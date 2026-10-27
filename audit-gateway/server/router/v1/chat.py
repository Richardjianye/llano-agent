"""模型转发入口

一次请求的完整链路：

    认证（JWT / API Key）
      → 按别名或模型名找模型
      → 白名单判定（默认拒绝）
      → 发送方向脱敏兜底
      → 转发上游（流式 / 非流式）
      → 写用量明细 + 日聚合 + 会话审计

**这里是审计、脱敏、用量三件事唯一的写入点**，加新逻辑请集中放在这条路径上，
不要散到别处，否则很容易出现「某个入口没记账」的黑洞。

响应格式与 OpenAI 保持一致（不套 {code,message,data} 信封），
否则客户端的 OpenAI SDK 会解析失败。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict

from db.selects.apikey import api_key_used_tokens, get_key_by_prefix
from db.updates.apikey import revoke_api_key
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from loguru import logger
from services.audit_envelope import AuditEnvelopeError, extract_audit_envelope
from services.forwarding import (UpstreamError, build_target, call_upstream,
                                 collect_delta, error_sse, extract_text,
                                 prepare_payload, stream_upstream)
from services.masking import mask_messages
from services.model_access import (check_model_permission,
                                   list_available_models, model_public_name)
from services.quota import applicable_quota_statuses, first_blocking_quota
from services.usage import (UsageBreakdown, UsageFacts, estimate_tokens,
                            extract_breakdown, extract_usage, record_usage)
from services.usage_query import ModelPrice
from utils.apikey import key_prefix_of, looks_like_api_key, verify_api_key
from utils.gateway_auth import Caller, require_caller
from utils.middleware import get_request_id

router = APIRouter()


def _openai_error(message: str, status_code: int = 400, code: str = "invalid_request"):
    """OpenAI 风格的错误体，客户端 SDK 能直接识别"""
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": code, "code": code}},
    )


@router.post("/device/revoke")
async def revoke_calling_device(request: Request):
    """Allow possession of a device API key to revoke that key itself.

    This deliberately does not require an active account. A disabled account
    or an expired JWT must still be able to finish an offline-delayed logout;
    the endpoint grants no capability other than destroying the presented key.
    """
    auth_header = request.headers.get("Authorization", "")
    plain = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""

    if not plain or not looks_like_api_key(plain):
        return _openai_error("缺少有效的设备 API Key", 401, "gateway_auth_failed")

    record = await get_key_by_prefix(key_prefix_of(plain))
    if record is None or not verify_api_key(plain, record.key_hash):
        return _openai_error("设备 API Key 无效", 401, "gateway_auth_failed")

    await revoke_api_key(record.id, reason="client_logout")
    logger.warning(
        f"设备 API Key 自助吊销: id={record.id} prefix={record.key_prefix} owner={record.user_uid}"
    )
    return {"revoked": True}


def _openai_model_item(m) -> Dict[str, Any]:
    """一个模型在 OpenAI 格式清单里的样子。

    ## 为什么多带一个 `context_length`

    OpenAI 的 model 对象里没有这个字段，但**不带它客户端就会把窗口算错**，而且
    错法是静默的：

    客户端拿不到窗口时会退回 `agent/model_metadata.py` 里一张硬编码表做**子串**
    模糊匹配。那张表里有 `deepseek-v4-flash → 1M`，也有兜底的 `deepseek → 128K`。
    后台配的模型 id 叫 `DeepSeek-V4.1-Flash`，中间那个 `.1` 让它匹配不上前者、
    落进后者 —— 于是 1M 窗口的模型被当成 128K 用。

    后果不是"少用了点上下文"。压缩触发线按窗口算（<512K 时抬到 75%），128K 的
    窗口把触发线压到 96K；而长会话里受保护的尾段本身就能到 130K，于是每次压缩
    都"压不动"，连续两次无效后防抖熔断落闸，该会话的自动压缩被彻底关掉，表现
    就是长时间无响应和 Session not found。2026-09-16 线上真发生了一次，
    排查链见 `docs/history/stages-8-28.md` 阶段 30。

    ## 为什么是 `context_length` 这个名字，不是 `max_context_tokens`

    客户端认的是 `_CONTEXT_LENGTH_KEYS`（`context_length` / `max_context_length`
    / `context_window` …），**不认**我们库里那个列名。实测：
    `_extract_context_length({"context_length": 1000000})` → 1000000；
    换成 `max_context_tokens` → None。所以这里必须按它认的名字发，
    改名等于把这个修复静默撤销。

    没配窗口的模型**不带这个键**（而不是发 null）：客户端见不到就走它自己的探测
    链，那比拿到一个 null 更好。
    """
    item: Dict[str, Any] = {
        "id": model_public_name(m),
        "object": "model",
        "owned_by": "llano-gateway",
        "created": 0,
    }
    if m.max_context_tokens:
        item["context_length"] = int(m.max_context_tokens)
    # 客户端靠它决定用户贴的图是直接给模型看，还是先让辅助模型写成文字描述。
    # 不带的话公司网关的模型一律被当成不能看图（models.dev 查不到它们），
    # DeepSeek V4.1 Flash 明明支持 image_url 也只能看到文字描述。见 agent/product_vision.py。
    item["supports_vision"] = bool(m.supports_multimodal)
    return item


@router.get("/models")
async def list_models(caller: Caller = Depends(require_caller)):
    """当前调用方可用的模型（OpenAI 格式）

    客户端被锁死只能用网关下发的模型（决策 #7），这里是唯一来源。
    """
    from services.image_generation import is_image_model
    models = [m for m in await list_available_models(caller.as_subject()) if not is_image_model(m)]
    return {
        "object": "list",
        "data": [_openai_model_item(m) for m in models],
    }


def _billing_dimensions(caller) -> Dict[str, str]:
    """这一笔算个人消耗还是协作消耗（阶段 27-E）。

    从**凭据**推：群聊会话令牌带着 scope=group + scope_project_id。请求头说了
    不算 —— 客户端能声明的话，「算个人还是算协作」就成了自选项，而报表要靠
    这一维把两种消耗拆开。
    """
    from services import conversation_scope as scope_service

    if scope_service.scope_from_credential(caller) == scope_service.GROUP:
        return {"call_kind": "chat", "trigger_source": "project"}
    return {"call_kind": "chat", "trigger_source": "private"}


async def _turn_request_id(request: Request, caller) -> str:
    """群聊 turn 里发生的模型调用，用**租约上的** trigger_request_id 记账。

    这样一条项目消息、它触发的每一笔 usage、以及路上的拒绝日志共用同一个 id，
    事后逐笔对得上账。id 不是从请求头里抄的：头里给的是 turn 号，服务端自己去
    查那把租约，确认它属于这个调用者、属于这个项目、而且还在跑。查不到就退回
    普通 request_id —— **不因为头里写了什么就换一个记账口径**。
    """
    from services import conversation_scope as scope_service

    # 测试里的 Request 替身可能没有 headers；取不到就当没带头。
    headers = getattr(request, "headers", None) or {}
    raw = (headers.get("X-Llano-Turn-Id") or "").strip()
    if not raw or scope_service.scope_from_credential(caller) != scope_service.GROUP:
        return ""

    try:
        turn_id = int(raw)
    except ValueError:
        return ""

    from db.inserts.project_turn import get_turn
    from services import project_runtime as rt

    turn = await get_turn(turn_id)
    if turn is None or turn.get("owner_uid") != caller.uid:
        return ""
    if turn.get("status") != rt.TURN_RUNNING:
        return ""
    if caller.scope_project_id is not None and int(turn["project_id"]) != int(caller.scope_project_id):
        # 拿项目 A 的群聊令牌去记项目 B 的账。
        return ""
    return str(turn.get("trigger_request_id") or "")


@router.post("/chat/completions")
async def chat_completions(request: Request, caller: Caller = Depends(require_caller)):
    """对话补全 —— 流式与非流式都走这里"""
    request_id = get_request_id(request)
    started = time.perf_counter()

    # 群聊 turn 内的调用改用租约上的 id 记账，好让项目消息与 usage 逐笔对得上。
    turn_rid = await _turn_request_id(request, caller)
    if turn_rid:
        request_id = turn_rid

    try:
        body: Dict[str, Any] = await request.json()
    except ValueError:
        return _openai_error("请求体不是合法 JSON")

    model_ref = body.get("model")
    if not model_ref:
        return _openai_error("缺少 model 参数")

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return _openai_error("缺少 messages 参数")

    # The private audit field must leave the model-bound body immediately
    # after authentication and structural validation.  From this point on,
    # `body` is safe to hand to payload builders and upstream code.
    try:
        audit = extract_audit_envelope(body)
    except AuditEnvelopeError as exc:
        return _openai_error(str(exc), 400, "invalid_audit_envelope")

    # Key 自身的累计额度与用户/部门日额度叠加生效。JWT 调用没有 Key 额度。
    if caller.api_key_id is not None and caller.api_key_token_limit is not None:
        key_used = await api_key_used_tokens(caller.api_key_id)
        if key_used >= caller.api_key_token_limit:
            return _openai_error(
                "当前 API Key 的独立额度已用完，请更换密钥或联系管理员调整额度",
                429,
                "api_key_quota_exceeded",
            )

    # ---- 白名单判定：默认拒绝，没配授权就是不能用 ----
    model = await check_model_permission(caller.as_subject(), str(model_ref))
    if model is None:
        logger.warning(
            f"模型访问被拒 rid={request_id} uid={caller.uid} "
            f"role={caller.role} dept={caller.dept_id} model={model_ref}"
        )
        return _openai_error(
            f"当前账号还没有开通模型「{model_ref}」，如需使用请联系管理员",
            403,
            "model_not_allowed",
        )

    from services.image_generation import is_image_model
    if is_image_model(model):
        return _openai_error("请通过生图功能调用该模型", 400, "image_model_requires_image_endpoint")

    # ---- 日 token 配额：按模型硬拒绝，用户可自行切换其他模型 ----
    blocking_quota = first_blocking_quota(
        await applicable_quota_statuses(caller.uid, caller.dept_id, model.id)
    )
    if blocking_quota is not None:
        logger.warning(
            f"模型配额已耗尽 rid={request_id} uid={caller.uid} dept={caller.dept_id} "
            f"model={model_ref} rule={blocking_quota.rule_id} "
            f"used={blocking_quota.used} limit={blocking_quota.limit}"
        )
        return _openai_error(
            f"模型「{model_public_name(model)}」今日额度已用完，请手动切换其他可用模型",
            429,
            "quota_exceeded",
        )

    from db.selects.catalog import get_provider

    provider = await get_provider(model.provider_id)
    if provider is None:
        return _openai_error("模型对应的上游渠道不存在", 503, "provider_missing")

    # ---- 发送方向脱敏兜底（客户端已做过一遍，这里防绕过）----
    masked_messages, masked = mask_messages(messages)
    body["messages"] = masked_messages

    try:
        target = build_target(provider, model, session_id=audit.session_id or "")
    except UpstreamError as exc:
        await _record_failure(caller, request_id, model, provider.id, exc, started,
                              audit, masked, bool(body.get("stream")))
        return _openai_error(str(exc), exc.status_code, exc.code)

    payload = prepare_payload(body, target)
    is_stream = bool(payload.get("stream"))

    if is_stream:
        return StreamingResponse(
            _stream_and_record(
                caller, request_id, model, target, payload, masked_messages,
                audit, masked, started
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                # 反向代理（nginx）默认会缓冲，不关掉流式会变成一次性吐出
                "X-Accel-Buffering": "no",
            },
        )

    return await _complete_and_record(
        caller, request_id, model, target, payload, masked_messages,
        audit, masked, started
    )


# ==================== 非流式 ====================


async def _complete_and_record(
    caller, request_id, model, target, payload, messages, audit, masked, started
):
    try:
        upstream = await call_upstream(target, payload)
    except UpstreamError as exc:
        await _record_failure(
            caller, request_id, model, target.provider_id, exc, started,
            audit, masked, False
        )
        return _openai_error(str(exc), exc.status_code, exc.code)

    text = extract_text(upstream)
    usage = extract_usage(upstream)
    # 细分与原文照单全收：现在报表用不上，但缺了以后就再也算不出缓存省了多少钱
    breakdown = extract_breakdown(upstream)
    estimated = usage is None

    if usage is None:
        # 上游没给 usage，退化成估算并打标记，报表要能区分
        usage = {
            "prompt_tokens": estimate_tokens(_messages_text(messages)),
            "completion_tokens": estimate_tokens(text),
            "total_tokens": 0,
        }
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]

    await record_usage(
        UsageFacts(
            **_billing_dimensions(caller),
            request_id=request_id,
            user_uid=caller.uid,
            api_key_id=getattr(caller, "api_key_id", None),
            dept_id=caller.dept_id,
            role=caller.role,
            provider_id=target.provider_id,
            model_id=model.id,
            model_name=model.model_name,
            stream=False,
            status="success",
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            total_tokens=usage["total_tokens"],
            cached_prompt_tokens=breakdown.cached_prompt_tokens,
            cache_write_tokens=breakdown.cache_write_tokens,
            reasoning_tokens=breakdown.reasoning_tokens,
            usage_raw=breakdown.raw,
            tokens_estimated=estimated,
            # 单价快照：取的是**这次调用路由到的那一行模型配置**。
            # 记账时回查数据库拿到的是「写库那一刻的价」，
            # 正赶上有人在后台改价，这笔就记到新价上去了
            price=ModelPrice.from_model(model),
            latency_ms=int((time.perf_counter() - started) * 1000),
            request_messages=audit.messages,
            session_id=audit.session_id,
            turn_kind=audit.turn_kind,
            response_text=text,
            masked=masked,
        )
    )

    # 把模型名换回对外别名，别让客户端看到上游真实型号
    upstream["model"] = model_public_name(model)
    return JSONResponse(content=upstream)


# ==================== 流式 ====================


# 后台记账任务的强引用池。
# asyncio 只持弱引用，不留一份的话任务可能在跑完前被 GC 回收，
# 表现是「偶尔少记一笔」——这种丢数据最难查。
_PENDING_RECORDS: set = set()


def _record_in_background(facts: UsageFacts) -> None:
    """把记账丢到事件循环上独立执行，不挂在请求的生命周期里

    **为什么不能直接 await。** 记账要放在生成器的 finally 里（见下），
    而 finally 可能是被 GeneratorExit 唤醒的 —— 在正在关闭的异步生成器里
    await 会直接抛 RuntimeError。用 create_task 只是「登记一个任务」，
    不涉及挂起，因此在关闭路径上也是安全的。
    """
    task = asyncio.create_task(record_usage(facts))
    _PENDING_RECORDS.add(task)
    task.add_done_callback(_PENDING_RECORDS.discard)


async def _stream_and_record(
    caller, request_id, model, target, payload, messages, audit, masked, started
):
    """透传 SSE 的同时旁路收集用量

    注意异常处理：响应头已经发出去了，这里再抛异常客户端只会看到连接断开，
    所以统一转成一个 error 事件 + [DONE]，让客户端能正常收尾。

    ⚠️ **记账必须在 finally 里，而且必须是后台任务。**

    这是实测抓出来的计费漏洞：发了 4 次请求，用量表只记了 3 次，
    漏的正是流式那次。原因是记账代码写在最后一个 `yield` 之后 ——
    客户端收到 `[DONE]` 就断开连接，Starlette 随即取消这个生成器，
    执行流根本走不到记账那几行，这次调用的 token 就白送了。

    真实场景比测试脚本更常见：用户点「停止生成」、关掉客户端、网络断开，
    每一次都是一笔不计费的调用。而且**越长的回答越容易被中途叫停**，
    也就是说漏掉的恰好是最贵的那些请求。

    放进 finally 之后，无论正常结束、上游报错、还是客户端提前断开，
    已经产生的 token 都会被记下来。
    """
    collected: list[str] = []
    usage: Dict[str, int] | None = None
    breakdown = UsageBreakdown()
    status = "success"
    error_code = None
    client_gone = False

    try:
        try:
            async for line, chunk in stream_upstream(target, payload):
                # 上游标准 SSE 自己会发 [DONE]。这里统一吞掉，循环结束后只由
                # 网关发送一次；否则客户端在上游那个 [DONE] 处正常收尾时，
                # GeneratorExit 会落进下面的「客户端中断」分支，把正常请求
                # 错记成 aborted=True。
                if line.strip() == "data: [DONE]":
                    continue

                # 原样透传，不重新序列化
                yield f"{line}\n\n"

                if chunk is None:
                    continue

                collected.append(collect_delta(chunk))

                # 带 include_usage 时，usage 在最后一个 chunk 里；
                # 细分字段也在同一个 chunk，顺手一起取，别漏
                chunk_usage = extract_usage(chunk)
                if chunk_usage:
                    usage = chunk_usage
                    breakdown = extract_breakdown(chunk)

        except UpstreamError as exc:
            status = "failed"
            error_code = exc.code
            logger.warning(f"流式转发失败 rid={request_id}: {exc}")
            yield error_sse(str(exc))
        except (GeneratorExit, asyncio.CancelledError):
            # 客户端提前断开（点了「停止生成」/ 关窗口 / 网络断）。
            # 不是错误：已经生成的内容照样要记账，只是标记出来，
            # 便于日后分析「有多少 token 花在了没人看的回答上」
            client_gone = True
            raise
        except Exception as exc:
            status = "failed"
            error_code = "gateway_error"
            logger.exception(f"流式转发异常 rid={request_id}: {exc}")
            yield error_sse("网关内部错误")

        yield "data: [DONE]\n\n"

    finally:
        if client_gone:
            # ⚠️ 不要把它写进 status/error_code。中断和成功/失败是**正交**的：
            # 这次调用真的发生了、token 真的花了、内容也真的产出了，
            # 只是用户没看完。写成 failed 会让报表里的「失败率」混进一堆
            # 其实成功了的调用，而那个指标是用来判断上游稳不稳的。
            logger.info(f"客户端中途断开，按已生成内容记账 rid={request_id}")

        text = "".join(collected)
        estimated = usage is None
        if usage is None:
            # 中途断开时上游那个带 usage 的尾 chunk 多半没收到，
            # 只能按字符估算 —— 打上 tokens_estimated 标记，报表要能区分
            prompt = estimate_tokens(_messages_text(messages))
            completion = estimate_tokens(text)
            usage = {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
            }

        _record_in_background(
            UsageFacts(
                **_billing_dimensions(caller),
                request_id=request_id,
                user_uid=caller.uid,
                api_key_id=getattr(caller, "api_key_id", None),
                dept_id=caller.dept_id,
                role=caller.role,
                provider_id=target.provider_id,
                model_id=model.id,
                model_name=model.model_name,
                stream=True,
                status=status,
                error_code=error_code,
                aborted=client_gone,
                prompt_tokens=usage["prompt_tokens"],
                completion_tokens=usage["completion_tokens"],
                total_tokens=usage["total_tokens"],
                cached_prompt_tokens=breakdown.cached_prompt_tokens,
                cache_write_tokens=breakdown.cache_write_tokens,
                reasoning_tokens=breakdown.reasoning_tokens,
                usage_raw=breakdown.raw,
                tokens_estimated=estimated,
                # 与非流式同源：按调用时的单价定价，不受事后调价影响
                price=ModelPrice.from_model(model),
                latency_ms=int((time.perf_counter() - started) * 1000),
                request_messages=audit.messages,
            session_id=audit.session_id,
            turn_kind=audit.turn_kind,
                response_text=text,
                masked=masked,
            )
        )


# ==================== 公共 ====================


def _messages_text(messages) -> str:
    """把 messages 拼成纯文本，仅用于 token 估算"""
    parts = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append(part["text"])
    return "\n".join(parts)


async def _record_failure(
    caller, request_id, model, provider_id, exc, started, audit, masked, stream
):
    """失败也要记一笔：上游抖动的频率是判断合作厂商稳定性的依据"""
    await record_usage(
        UsageFacts(
            **_billing_dimensions(caller),
            request_id=request_id,
            user_uid=caller.uid,
            api_key_id=getattr(caller, "api_key_id", None),
            dept_id=caller.dept_id,
            role=caller.role,
            provider_id=provider_id,
            model_id=model.id if model else None,
            model_name=model.model_name if model else None,
            stream=stream,
            status="failed",
            error_code=getattr(exc, "code", "unknown"),
            # 失败的调用也要带单价快照：不带的话这一笔会被算成「没有快照」，
            # 整行日聚合就退回按当前单价回算，把当天正常的那些笔一起拖下水
            price=ModelPrice.from_model(model) if model else None,
            latency_ms=int((time.perf_counter() - started) * 1000),
            request_messages=audit.messages,
            session_id=audit.session_id,
            turn_kind=audit.turn_kind,
            response_text="",
            masked=masked,
        )
    )
