"""会话审计查询接口（决策 #8 的最后一块）

写入侧在 services/usage.py + services/attachments.py，挂在 `/v1/chat/completions`
那条转发链路上；这里只负责把已经落库的会话切出来给管理部门看。

## 接口分层

| 接口 | 用途 | 下发全文？ | 记哪种查阅日志 |
| ---- | ---- | ---------- | -------------- |
| `/list` | 列表检索 | ✗ 只给摘要 | `list`（记条件与命中数） |
| `/detail/{id}` | 详情抽屉 | ✓ | `detail`（精确到某一条） |
| `/attachment/{id}` | 附件预览/下载 | ✓ 文件流 | `attachment` |
| `/filters` | 下拉框候选项 | —— | 不记（不含任何内容） |
| `/export` | 导出摘要 CSV | ✗ 只导摘要 | `export` |
| `/access-logs` | 查阅日志本身 | —— | 不记（见下） |
| `/access-logs/export` | 查阅日志导出 | —— | 不记（不含会话内容） |

## 三条不能破的规矩

1. **权限判定只有 `audit:view` 一条路。** 它是**按部门**判定的（总裁无条件，
   其他人须「在管理部门」且「级别 ≥ 审计员」），与用量报表那套 `resolve_usage_scope`
   完全独立。别把 scope 那套逻辑复制过来。开发者有全部系统配置权限，
   但不在管理部门就一条会话都看不到 —— 这是决策 #17 明写的，不给角色开后门。

2. **列表接口绝不下发全文。** 这是「列表 + 详情抽屉」方案的全部意义所在：
   全文只能通过 `/detail/{id}` 拿，于是每一次「看了谁的哪一条」都有据可查。
   哪天有人想「顺手在列表里带上全文省一次请求」，请先回来读这一段。

3. **查阅日志与查询结果同生共死。** 每个会返回内容的接口都必须调
   `log_access`。写日志失败只告警不阻断（审计员不该因日志表故障而查不了），
   但漏写这个调用就是功能缺陷，不是性能优化。
   **取不到东西（404）也要记**：查阅日志回答的是「谁试图看什么」，
   不是「谁成功看到了什么」——挨个试 ID 的行为恰恰全都落在失败那一侧。

## 为什么 /access-logs 自己不记日志

否则打开这个页面就产生新记录，翻两页就把真正的查阅行为淹没了。
访问它同样需要 `audit:view`，够了。
"""

from __future__ import annotations

import os
import re
from datetime import date
from typing import Optional

from db.selects.audit import (fetch_access_logs, fetch_attachment,
                              fetch_attachments, fetch_audit_filter_options,
                              fetch_conversation, fetch_conversations,
                              fetch_dept_labels, fetch_session_titles,
                              fetch_sessions, fetch_user_labels)
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from fastapi.responses import Response as RawResponse
from model.database import ALL_ROLES, LEGACY_ROLES
from model.response import Response, UserInfo
from services.attachments import is_safe_inline_mime, resolve_storage_path
from services.audit_access import (ACTION_ATTACHMENT, ACTION_DETAIL,
                                   ACTION_EXPORT, ACTION_LIST, ACTION_LABELS,
                                   ALL_ACTIONS, AccessContext, log_access)
from services.audit_query import (CONVERSATION_STATUSES, AuditQueryError,
                                  access_logs_to_csv, extract_markers,
                                  format_size, make_preview,
                                  normalize_choice, normalize_keyword,
                                  normalize_page, normalize_page_size,
                                  parse_tool_skeleton, summarize_tools,
                                  to_csv, turn_tool_delta)
from services.usage_query import (GRANULARITY_DAY, UsageQueryError,
                                  resolve_range)
from utils.dependencies import require_permission
from utils.errors import NotFoundError, ValidationError_
from utils.middleware import get_client_ip, get_request_id

router = APIRouter(prefix="/audit", tags=["会话审计"])

# 口径声明。**只跟着导出的 CSV 走，不再下发到页面上** ——
# 界面里逐条解释系统行为会把真正要看的内容挤下去，那些说明归说明书
# （docs/internal/会话审计说明.md）。
# 但 CSV 必须带：表格转手几次之后，没人记得这份数据是怎么来的。
AUDIT_NOTE = (
    "会话内容为发送方向脱敏后的版本，模型回复原样留存；"
    "超长内容会被截断并标记；图片与文件为附件记录，正文中以占位符表示。"
)

# 导出上限。审计是逐条看的活，不是导数据的活；
# 而且导出的每一行都是聊天摘要，数量放开等于把会话库打包带走
EXPORT_MAX_ROWS = 2000

# 可筛选的角色取值。带上遗留角色（admin/operator）：
# 历史会话里存的就是那些值，白名单卡掉它们等于查不了老数据
KNOWN_ROLES = ALL_ROLES + LEGACY_ROLES


def _check_filters(status: Optional[str], role: Optional[str]) -> tuple:
    """枚举型筛选值的白名单校验

    写错的筛选条件必须报错。放行的话，`status=succes` 这类笔误会安静地
    查出 0 条，而审计员读到的是「这个人这段时间没聊过」—— 一个完全错误、
    却很有说服力的结论。「查了但没有记录」和「查询条件本身写错了」
    在审计场景里是天差地别的两件事。
    """
    try:
        return (
            normalize_choice(status, CONVERSATION_STATUSES, "状态"),
            normalize_choice(role, KNOWN_ROLES, "角色"),
        )
    except AuditQueryError as exc:
        raise ValidationError_(str(exc))


def row_preview(row) -> str:
    """列表页那一行的摘要。

    新记录读入库时就落定的 `last_user_preview`（最后一条用户消息）。
    老记录没有这一列，退回「取正文开头再拍平」—— 对决策 #52 之前的记录效果
    很差，那时正文开头永远是那段每条都一样的系统提示，但总比空白强，
    而且不会随时间恶化：新写的记录都有摘要。
    """
    preview = (getattr(row, "last_user_preview", None) or "").strip()
    if preview:
        return preview

    return make_preview(re.sub(r"\s+", " ", str(getattr(row, "request_head", "") or "")).strip())


def _ctx(request: Request, user: UserInfo) -> AccessContext:
    """从当前请求里攒出查阅现场信息"""
    return AccessContext(
        actor_uid=user.uid,
        actor_role=user.permission,
        actor_dept_id=user.dept_id,
        request_id=get_request_id(request),
        client_ip=get_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
    )


def _resolve_days(start: Optional[date], end: Optional[date]) -> tuple[date, date]:
    """复用用量那边的区间解析：默认近 7 天，跨度上限 366 天"""
    try:
        return resolve_range(start, end, GRANULARITY_DAY)
    except UsageQueryError as exc:
        raise ValidationError_(str(exc))


@router.get("/list", response_model=Response)
async def audit_list(
    request: Request,
    start: Optional[date] = None,
    end: Optional[date] = None,
    dept_id: Optional[int] = None,
    user_uid: Optional[str] = None,
    role: Optional[str] = None,
    model_id: Optional[int] = None,
    status: Optional[str] = None,
    has_attachment: Optional[bool] = None,
    keyword: Optional[str] = Query(None, description="在请求与回复中检索"),
    request_id: Optional[str] = Query(None, description="精确定位某次调用，来自用量明细"),
    session_id: Optional[str] = Query(None, description="只看某个会话内的轮次"),
    include_machine_turns: bool = Query(
        False, description="连标题生成、命令审批扫描一起显示"
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1),
    user: UserInfo = Depends(require_permission("audit:view")),
):
    """单次调用列表 —— **只返回摘要，不返回全文**

    摘要由数据库侧 LEFT() 截回来的片段生成，一页 20 条也就几十 KB。
    要看全文请点开详情抽屉，那会留下一条精确到单条的查阅日志。
    """
    start, end = _resolve_days(start, end)
    status, role = _check_filters(status, role)

    try:
        page_size = normalize_page_size(page_size)
        page = normalize_page(page, page_size)
        keyword = normalize_keyword(keyword)
    except AuditQueryError as exc:
        raise ValidationError_(str(exc))

    rows, total = await fetch_conversations(
        start, end,
        limit=page_size, offset=(page - 1) * page_size,
        with_tools=True,
        dept_id=dept_id, user_uid=user_uid, role=role, model_id=model_id,
        status=status, has_attachment=has_attachment, keyword=keyword,
        request_id=request_id, session_id=session_id,
        include_machine_turns=include_machine_turns,
    )

    user_labels = await fetch_user_labels(list({r.user_uid for r in rows}))
    dept_labels = await fetch_dept_labels(
        list({r.dept_id for r in rows if r.dept_id is not None})
    )

    # 会话内每一轮的「本次用户输入」是**同一句话**：Hermes 每轮把完整历史
    # 重发一遍，所以最后一条 user 消息从头到尾没变过。列表按 id 倒序返回，
    # 数组里的下一项就是时间上更早的那一轮 —— 和它相同就打标，前端折起来。
    #
    # 放服务端算而不是前端：它是这一页数据的纯函数，放这儿能被 pytest 覆盖。
    # ⚠️ 跨页比不了：每页第一行拿不到上一页最后一行，只能按「不重复」处理。
    # 展开会话时一次取 100 行，超过的部分由前端「加载更多」逐页续拉，
    # 页边界那一对的重复标记由前端补算
    previews = [row_preview(r) for r in rows]
    repeats = [
        bool(previews[i] and i + 1 < len(previews) and previews[i] == previews[i + 1])
        for i in range(len(previews))
    ]

    # 工具骨架记的是「这次请求的上下文里有哪些工具结果」，而 Hermes 每轮重发
    # 完整历史 —— 直接展示的话每一行都写着 `terminal × 43`，读起来像每轮都调了
    # 43 次。所以这里算**相对上一轮的新增**，详见 turn_tool_delta 的说明
    skeletons = [parse_tool_skeleton(getattr(r, "tool_skeleton", None)) for r in rows]
    deltas = [
        turn_tool_delta(
            skeletons[i], skeletons[i + 1] if i + 1 < len(skeletons) else None
        )
        for i in range(len(skeletons))
    ]

    items = [
        {
            "id": r.id,
            "request_id": r.request_id,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "user_uid": r.user_uid,
            "username": user_labels.get(r.user_uid, r.user_uid),
            "dept_id": r.dept_id,
            "dept_name": dept_labels.get(r.dept_id) if r.dept_id else None,
            "role": r.role,
            "model_name": r.model_name,
            "status": r.status,
            "masked": bool(r.masked),
            "truncated": bool(r.truncated),
            "stream": bool(r.stream),
            "aborted": bool(r.aborted),
            "attachment_count": r.attachment_count or 0,
            "session_id": r.session_id,
            "turn_kind": r.turn_kind or "chat",
            "preview": preview,
            # 和上一轮问的是同一句 —— 会话内这几乎总是 true
            "repeats_question": repeat,
            "response_preview": make_preview(r.response_head or ""),
            # 这一轮**新增**的工具调用（不是上下文里的累计）。
            # null 表示算不出（会话第一轮、跨页、上下文被压缩过），
            # 和 `[]`「这一轮没调工具」是两回事，界面上要长得不一样
            "tools": None if delta is None else summarize_tools(delta),
            # 上下文里累计有多少条工具结果 —— 「这次请求往模型里灌了多少」
            "tools_in_context": len(skeleton),
        }
        for r, preview, repeat, delta, skeleton in zip(
            rows, previews, repeats, deltas, skeletons
        )
    ]

    # 记条件与命中数，不记具体看到了哪些人 —— 列表本来就只给了摘要
    await log_access(
        _ctx(request, user), ACTION_LIST,
        query={
            "start": start, "end": end, "dept_id": dept_id, "user_uid": user_uid,
            "role": role, "model_id": model_id, "status": status,
            "has_attachment": has_attachment, "keyword": keyword,
            "request_id": request_id, "session_id": session_id,
            "include_machine_turns": include_machine_turns, "page": page,
        },
        result_count=len(items),
    )

    return Response(data={
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "range": {"start": start.isoformat(), "end": end.isoformat()},
    })


@router.get("/sessions", response_model=Response)
async def audit_sessions(
    request: Request,
    start: Optional[date] = None,
    end: Optional[date] = None,
    dept_id: Optional[int] = None,
    user_uid: Optional[str] = None,
    role: Optional[str] = None,
    model_id: Optional[int] = None,
    status: Optional[str] = None,
    has_attachment: Optional[bool] = None,
    keyword: Optional[str] = Query(None, description="在请求与回复中检索"),
    include_machine_turns: bool = Query(
        False, description="连标题生成、命令审批扫描一起统计"
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1),
    user: UserInfo = Depends(require_permission("audit:view")),
):
    """**会话列表** —— 一行一个会话，不是一行一次调用（决策 #52 · C1）

    一次提问在底层会散成十几条记录：标题生成 1 条、主对话 N 个 agent 轮次、
    命令审批扫描若干条。平铺出来彼此看不出关系，而真正想看的「这个人问了什么」
    被淹在里面。这里按 Hermes 自己的会话 ID 折叠。

    展开某一行去看里面每一轮，走的还是 `/list`，带上 `session_id` 即可 ——
    不为它单开一个接口：那会变成两套摘要逻辑，迟早只改其中一套。

    ⚠️ **没有 session_id 的记录照常出现**，退化成一行一条。stock Hermes 和
    绕过客户端的直连永远没有会话 ID，「分不了组就不显示」正好把最该看的藏了。
    """
    start, end = _resolve_days(start, end)
    status, role = _check_filters(status, role)

    try:
        page_size = normalize_page_size(page_size)
        page = normalize_page(page, page_size)
        keyword = normalize_keyword(keyword)
    except AuditQueryError as exc:
        raise ValidationError_(str(exc))

    rows, total = await fetch_sessions(
        start, end,
        limit=page_size, offset=(page - 1) * page_size,
        dept_id=dept_id, user_uid=user_uid, role=role, model_id=model_id,
        status=status, has_attachment=has_attachment, keyword=keyword,
        include_machine_turns=include_machine_turns,
    )

    titles = await fetch_session_titles([r.first_id for r in rows])
    user_labels = await fetch_user_labels(list({r.user_uid for r in rows}))
    dept_labels = await fetch_dept_labels(
        list({r.dept_id for r in rows if r.dept_id is not None})
    )

    items = [
        {
            # 没有 session_id 的行用 `#<id>` 当键 —— 前端只拿它做 v-for 的 key
            # 和展开状态，不要拿去当会话 ID 反查
            "session_key": r.session_key,
            "session_id": r.session_id,
            "linked": r.session_id is not None,
            "user_uid": r.user_uid,
            "username": user_labels.get(r.user_uid, r.user_uid),
            "dept_id": r.dept_id,
            "dept_name": dept_labels.get(r.dept_id) if r.dept_id else None,
            "role": r.role,
            "model_name": r.model_name,
            "turns": int(r.turns or 0),
            "first_id": r.first_id,
            "last_id": r.last_id,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "ended_at": r.ended_at.isoformat() if r.ended_at else None,
            "total_tokens": int(r.total_tokens or 0),
            "attachment_count": int(r.attachment_count or 0),
            "has_failure": bool(r.has_failure),
            "has_masked": bool(r.has_masked),
            "has_truncated": bool(r.has_truncated),
            "title": titles.get(r.first_id, ""),
        }
        for r in rows
    ]

    await log_access(
        _ctx(request, user), ACTION_LIST,
        query={
            "start": start, "end": end, "dept_id": dept_id, "user_uid": user_uid,
            "role": role, "model_id": model_id, "status": status,
            "has_attachment": has_attachment, "keyword": keyword,
            "include_machine_turns": include_machine_turns,
            "page": page, "grouped": True,
        },
        result_count=len(items),
    )

    return Response(data={
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "range": {"start": start.isoformat(), "end": end.isoformat()},
    })


@router.get("/detail/{conversation_id}", response_model=Response)
async def audit_detail(
    request: Request,
    conversation_id: int,
    user: UserInfo = Depends(require_permission("audit:view")),
):
    """会话详情 —— 唯一会下发全文的接口

    对应的 `detail` 查阅日志精确到「谁、何时、看了谁的哪一条」，
    这正是选「列表 + 详情抽屉」而不是「表格直接展开」的原因。
    """
    log = await fetch_conversation(conversation_id)
    if log is None:
        # 同附件接口：没查到也留痕，否则「挨个试 ID」这类行为完全不可见
        await log_access(
            _ctx(request, user), ACTION_DETAIL,
            conversation_id=conversation_id, result_count=0,
        )
        raise NotFoundError("会话记录不存在或已过留存期")

    attachments = await fetch_attachments(conversation_id)
    user_labels = await fetch_user_labels([log.user_uid])
    dept_labels = await fetch_dept_labels([log.dept_id] if log.dept_id else [])

    tools = parse_tool_skeleton(log.tool_skeleton)

    await log_access(
        _ctx(request, user), ACTION_DETAIL,
        conversation_id=conversation_id,
        target_user_uid=log.user_uid,
        result_count=1,
    )

    return Response(data={
        "id": log.id,
        "request_id": log.request_id,
        "created_at": log.created_at.isoformat() if log.created_at else None,
        "user_uid": log.user_uid,
        "username": user_labels.get(log.user_uid, log.user_uid),
        "dept_id": log.dept_id,
        "dept_name": dept_labels.get(log.dept_id) if log.dept_id else None,
        "role": log.role,
        "model_name": log.model_name,
        "status": log.status,
        "masked": bool(log.masked),
        "truncated": bool(log.truncated),
        "stream": bool(log.stream),
        "aborted": bool(log.aborted),
        # 会话串联（决策 #52）。session_id 是客户端给的、可伪造，
        # 只用于展示分组，绝不参与鉴权
        "session_id": log.session_id,
        "turn_kind": log.turn_kind or "chat",
        # ---- 内容：只留「问了什么 / 答了什么」（决策 #52）----
        "user_input": log.user_input or "",
        "user_input_markers": extract_markers(log.user_input or ""),
        # 接收方向只存不改（决策 #9）：这就是模型真实说过的话
        "response_text": log.response_text or "",
        "response_markers": extract_markers(log.response_text or ""),
        # 系统提示词只有指纹。前端拿它做的事只有一件：把同一版提示词的记录
        # 认出来（前 12 位就够肉眼比对），**不要**假装还能看到正文
        "system_prompt_sha256": log.system_prompt_sha256,
        "system_prompt_chars": int(log.system_prompt_chars or 0),
        # 本地工具链只有骨架：哪个工具、各多长。正文一个字不存 —— 知情取舍，
        # 见 services/audit_content.py 模块注释
        # 逐次明细 + 按工具名的汇总。默认展示汇总 —— 一次 agent 会话能有
        # 63 次工具调用，全摊开是一面读不出东西的墙（2026-08-23 实测）
        "tools": tools,
        "tool_summary": summarize_tools(tools),
        "tool_chars_total": sum(int(t.get("chars") or 0) for t in tools),
        "attachments": [
            {
                "id": a.id,
                "marker": a.marker,
                "kind": a.kind,
                "mime_type": a.mime_type,
                "filename": a.filename,
                "size_bytes": a.size_bytes or 0,
                "size_text": format_size(a.size_bytes),
                "sha256": a.sha256,
                # 远程链接原样给出但不代下载（避免网关变成 SSRF 跳板），
                # 前端只展示不做成可点链接
                "origin_url": a.origin_url,
                "stored": bool(a.stored),
                "store_error": a.store_error,
            }
            for a in attachments
        ],
    })


@router.get("/attachment/{attachment_id}")
async def audit_attachment(
    request: Request,
    attachment_id: int,
    user: UserInfo = Depends(require_permission("audit:view")),
):
    """附件原件 —— 返回文件流，不套 {code,message,data} 信封

    只服务已落盘的附件。远程 URL 形态的附件网关从未抓取过（见
    services/attachments.py 的第 2 条边界），这里也不会代为下载。

    ⚠️ **日志记在取文件之前，取不到也照记。**
    原先是「先判断可不可用，不可用直接 404」，于是所有失败的访问都不留痕 ——
    一个人挨个试 ID 想找出别人的附件，只要没试中就完全查不到这段行为，
    而这恰恰是最该被记下来的那一类。查阅日志要回答的是「谁试图看什么」，
    不是「谁成功看到了什么」。result_count 用 0/1 区分成没成。
    """
    attachment = await fetch_attachment(attachment_id)
    ctx = _ctx(request, user)

    if attachment is None:
        # 记录不存在也留一条：挨个试 ID 的行为只会表现成一串这样的记录
        await log_access(
            ctx, ACTION_ATTACHMENT, attachment_id=attachment_id, result_count=0
        )
        raise NotFoundError("附件记录不存在")

    async def _log(count: int) -> None:
        await log_access(
            ctx, ACTION_ATTACHMENT,
            conversation_id=attachment.conversation_id,
            attachment_id=attachment.id,
            target_user_uid=attachment.user_uid,
            result_count=count,
        )

    if not attachment.stored or not attachment.storage_path:
        reason = attachment.store_error or (
            "该附件为远程链接，网关按设计未抓取内容"
            if attachment.origin_url else "附件本体未留存"
        )
        await _log(0)
        raise NotFoundError(f"附件内容不可用：{reason}")

    from config import get_settings

    abs_path = resolve_storage_path(
        get_settings().audit_storage_path, attachment.storage_path
    )
    if abs_path is None:
        await _log(0)
        raise NotFoundError("附件文件已丢失，请检查存储目录与留存策略")

    # 附件是最敏感的部分，单独记一条
    await _log(1)

    inline = is_safe_inline_mime(attachment.mime_type)
    disposition = "inline" if inline else "attachment"

    return FileResponse(
        abs_path,
        # 用户提供的 MIME 不可信。只有安全栅格图保留原类型供预览，
        # 其他内容统一二进制下载，避免 HTML/SVG 在管理后台同源执行。
        media_type=(attachment.mime_type if inline else "application/octet-stream"),
        headers={
            "Content-Disposition": (
                f"{disposition}; filename*=UTF-8''"
                + _quote(attachment.filename or os.path.basename(abs_path))
            ),
            # 内容敏感，别让浏览器和中间层缓存
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
        },
    )


def _quote(name: str) -> str:
    from urllib.parse import quote

    return quote(name, safe="")


@router.get("/filters", response_model=Response)
async def audit_filters(user: UserInfo = Depends(require_permission("audit:view"))):
    """筛选下拉框候选项

    列的是全部部门与模型（而非「有记录的」）：审计员常要确认
    「某个部门有没有人用过 AI」，下拉框里没这个部门就问不出这个问题。

    不记查阅日志 —— 这里不含任何会话内容。
    """
    return Response(data=await fetch_audit_filter_options())


@router.get("/export")
async def audit_export(
    request: Request,
    start: Optional[date] = None,
    end: Optional[date] = None,
    dept_id: Optional[int] = None,
    user_uid: Optional[str] = None,
    role: Optional[str] = None,
    model_id: Optional[int] = None,
    status: Optional[str] = None,
    has_attachment: Optional[bool] = None,
    keyword: Optional[str] = None,
    request_id: Optional[str] = None,
    user: UserInfo = Depends(require_permission("audit:view")),
):
    """导出检索结果为 CSV —— **只导摘要，不导全文**

    刻意的取舍：全文导出等于把会话库打包带走，一旦落到本地磁盘就
    彻底脱离了留存期限与访问控制的管辖（第 5 节合规动作）。
    要看全文请逐条点开，每次都会留下一条 detail 日志。
    """
    start, end = _resolve_days(start, end)
    status, role = _check_filters(status, role)

    try:
        keyword = normalize_keyword(keyword)
    except AuditQueryError as exc:
        raise ValidationError_(str(exc))

    rows, total = await fetch_conversations(
        start, end,
        limit=EXPORT_MAX_ROWS, offset=0,
        dept_id=dept_id, user_uid=user_uid, role=role, model_id=model_id,
        status=status, has_attachment=has_attachment, keyword=keyword,
        request_id=request_id, session_id=session_id,
        include_machine_turns=include_machine_turns,
    )

    user_labels = await fetch_user_labels(list({r.user_uid for r in rows}))
    dept_labels = await fetch_dept_labels(
        list({r.dept_id for r in rows if r.dept_id is not None})
    )

    # 角色列直接导编码不导中文名：中文名的真源是前端 constants/roles.ts
    # （权限表的镜像），在后端再复制一份只会多一处要同步的地方
    payload = [
        {
            "created_at": r.created_at.isoformat(sep=" ", timespec="seconds") if r.created_at else "",
            "username": user_labels.get(r.user_uid, r.user_uid),
            "dept_name": dept_labels.get(r.dept_id) if r.dept_id else "",
            "role": r.role,
            "model_name": r.model_name,
            "status": r.status,
            "stream": bool(r.stream),
            "aborted": bool(r.aborted),
            "attachment_count": r.attachment_count or 0,
            "truncated": bool(r.truncated),
            "masked": bool(r.masked),
            "preview": row_preview(r),
            "response_preview": make_preview(r.response_head or ""),
        }
        for r in rows
    ]

    note = f"{AUDIT_NOTE} 本次导出 {len(payload)} 条（命中 {total} 条，上限 {EXPORT_MAX_ROWS}），区间 {start} ~ {end}，服务器本地时区。"
    csv_text = to_csv(payload, note=note)

    await log_access(
        _ctx(request, user), ACTION_EXPORT,
        query={
            "start": start, "end": end, "dept_id": dept_id, "user_uid": user_uid,
            "role": role, "model_id": model_id, "status": status,
            "has_attachment": has_attachment, "keyword": keyword,
        },
        result_count=len(payload),
    )

    filename = f"audit-{start}-{end}.csv"
    return RawResponse(
        # Excel 打开无 BOM 的 UTF-8 CSV 会把中文显示成乱码，必须加
        content=("﻿" + csv_text).encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


async def _access_log_items(rows) -> list:
    """查阅日志行 → 响应/CSV 共用的字典

    列表和导出必须走同一个转换：两边各拼一遍的话，日后加一列
    只会加在其中一边，而对不上的恰好是拿去交材料的那份。
    """
    uids = {r.actor_uid for r in rows} | {
        r.target_user_uid for r in rows if r.target_user_uid
    }
    labels = await fetch_user_labels(list(uids))

    return [
        {
            "id": r.id,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "actor_uid": r.actor_uid,
            "actor_name": labels.get(r.actor_uid, r.actor_uid),
            "actor_role": r.actor_role,
            "action": r.action,
            "action_label": ACTION_LABELS.get(r.action, r.action),
            "target_conversation_id": r.target_conversation_id,
            "target_attachment_id": r.target_attachment_id,
            "target_user_uid": r.target_user_uid,
            "target_name": (
                labels.get(r.target_user_uid) if r.target_user_uid else None
            ),
            "query_json": r.query_json,
            "result_count": r.result_count,
            "client_ip": r.client_ip,
        }
        for r in rows
    ]


def _check_action(action: Optional[str]) -> Optional[str]:
    if action and action not in ALL_ACTIONS:
        raise ValidationError_(f"动作非法，可选值：{', '.join(ALL_ACTIONS)}")
    return action or None


@router.get("/access-logs", response_model=Response)
async def audit_access_logs(
    start: Optional[date] = None,
    end: Optional[date] = None,
    actor_uid: Optional[str] = None,
    action: Optional[str] = None,
    target_user_uid: Optional[str] = Query(
        None, description="按被查阅人筛选：谁看过某个人的会话"
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1),
    user: UserInfo = Depends(require_permission("audit:view")),
):
    """查阅日志 —— 「看的人也被看」

    这张表本身要能被看到，否则「记了日志」和「没记」在体感上没区别。
    访问它同样需要 audit:view，但**不再为此记一条日志**：
    否则打开这个页面就产生新记录，翻两页就把真正的查阅行为淹没了。

    `target_user_uid` 是这张表真正的用法：日志按人翻只能看出「他很勤快」，
    按**被查阅人**翻才回答得了「有没有人在专门盯着某个人」——
    那才是查阅日志存在的意义。
    """
    start, end = _resolve_days(start, end)
    action = _check_action(action)

    try:
        page_size = normalize_page_size(page_size)
        page = normalize_page(page, page_size)
    except AuditQueryError as exc:
        raise ValidationError_(str(exc))

    rows, total = await fetch_access_logs(
        start, end,
        limit=page_size, offset=(page - 1) * page_size,
        actor_uid=actor_uid, action=action, target_user_uid=target_user_uid,
    )

    return Response(data={
        "items": await _access_log_items(rows),
        "total": total,
        "page": page,
        "page_size": page_size,
        "range": {"start": start.isoformat(), "end": end.isoformat()},
    })


@router.get("/access-logs/export")
async def audit_access_logs_export(
    start: Optional[date] = None,
    end: Optional[date] = None,
    actor_uid: Optional[str] = None,
    action: Optional[str] = None,
    target_user_uid: Optional[str] = None,
    user: UserInfo = Depends(require_permission("audit:view")),
):
    """导出查阅日志为 CSV —— 给合规检查/内部核查交材料用

    与会话导出的取舍不同，这份可以放心导：**它一个字的聊天内容都没有**，
    只有「谁在什么时候看了谁的什么」。会话导出之所以只出摘要，
    是因为全文落到本地磁盘就脱离了留存期限管辖；查阅日志没有这个问题。

    同样不为这次导出记一条查阅日志 —— 理由和 /access-logs 一样：
    这里没碰到任何会话内容，记了只会淹没真正的查阅行为。
    """
    start, end = _resolve_days(start, end)
    action = _check_action(action)

    rows, total = await fetch_access_logs(
        start, end,
        limit=EXPORT_MAX_ROWS, offset=0,
        actor_uid=actor_uid, action=action, target_user_uid=target_user_uid,
    )

    items = await _access_log_items(rows)
    note = (
        f"会话查阅日志，本次导出 {len(items)} 条（命中 {total} 条，"
        f"上限 {EXPORT_MAX_ROWS}），区间 {start} ~ {end}，服务器本地时区。"
        "本表不含任何会话内容。"
    )
    csv_text = access_logs_to_csv(items, note=note)

    filename = f"audit-access-logs-{start}-{end}.csv"
    return RawResponse(
        content=("﻿" + csv_text).encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )
