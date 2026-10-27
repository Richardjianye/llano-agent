"""对账：一次调用之后，回头核对审计、附件、查阅日志、用量四条链路。

## 为什么单独成模块（阶段 4 · CP1）

阶段 4 要的「完整链路」是**客户端驱动**的：Playwright 开真实 Electron 登录、
切模型、发消息，再回头核对。而 `e2e_audit_check.py` 是**脚本自己发 HTTP**。
两者发请求的方式不同，但「发了几次就该记几笔、中断不算失败、明细不许带聊天内容」
这套口径必须**完全一致** —— 否则两条链路各自绿着，却在核对同一件事时用了两套标准，
那种「都通过了」比不测更危险。

所以口径只在这里定义一次。脚本直接 import；跨语言的调用方（Playwright）走
`scripts/e2e_reconcile.py` 这个 CLI，拿 JSON 结果。

## 与调用方的约定

这些函数**只读**：全部走管理端 GET 接口，不发对话、不产生上游费用、不改数据。
所以可以在任意时刻重复调用来"看一眼现在库里是什么样"，包括在客户端跑完之后。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from .gateway import Gateway
from .reporter import Reporter


async def check_audit(
    gw: Gateway,
    reporter: Reporter,
    audit_sentinel: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """② 会话审计：存进去了吗，读得出来吗"""
    reporter.section("② 会话审计存储与读取")

    listing = await gw.admin_get("/admin/audit/list", page=1, page_size=20)
    items = listing["items"]

    reporter.check(len(items) > 0, "审计列表有记录", f"共 {listing['total']} 条")
    if not items:
        return None

    # 列表不该下发全文 —— 这是「列表 + 详情抽屉」方案的核心前提
    leaked = [k for k in ("user_input", "response_text", "messages") if k in items[0]]
    reporter.check(not leaked, "列表接口未下发全文", f"泄漏字段：{leaked}" if leaked else "只有 preview 摘要")

    reporter.check(
        any(i["preview"] for i in items),
        "列表摘要非空",
        "摘要是空的说明 last_user_preview 没落上",
    )

    if audit_sentinel:
        sentinel_found = False
        for item in items:
            try:
                probe = await gw.admin_get(f"/admin/audit/detail/{item['id']}")
            except RuntimeError:
                continue
            if audit_sentinel in json.dumps(
                probe.get("messages") or [], ensure_ascii=False
            ):
                sentinel_found = True
                break
        reporter.check(
            sentinel_found,
            "审计详情保留双通道原文哨兵",
            "未找到说明私有审计字段未写入 conversation_logs",
        )

    with_attachment = [i for i in items if i["attachment_count"] > 0]
    reporter.check(
        len(with_attachment) > 0,
        "有请求被识别出附件",
        f"{len(with_attachment)} 条带附件",
    )

    # 图片 / 文档 / 音频三种 part 形态都要认得出来。
    # 豆包这三类走的是同一条 base64 内联通道，抽取逻辑漏掉哪一种都只会
    # 表现为「附件数少了一个」，不会报错
    kinds = set()
    for item in with_attachment:
        try:
            probe = await gw.admin_get(f"/admin/audit/detail/{item['id']}")
        except RuntimeError:
            continue
        kinds |= {a["kind"] for a in probe.get("attachments", [])}
    if kinds:
        reporter.info(f"识别出的附件类型：{', '.join(sorted(kinds))}")

    # 请求ID 检索：用量明细追到会话原文的入口
    if items:
        rid = items[0]["request_id"]
        located = await gw.admin_get("/admin/audit/list", request_id=rid)
        reporter.check(
            located["total"] >= 1,
            "能按请求ID精确定位会话",
            f"{rid} → {located['total']} 条",
        )

    # 详情：唯一会拿到全文的接口
    target = with_attachment[0] if with_attachment else items[0]
    detail = await gw.admin_get(f"/admin/audit/detail/{target['id']}")

    reporter.check(len(detail["messages"]) > 0, "详情能读出请求消息")
    reporter.check(
        "base64" not in json.dumps(detail["messages"], ensure_ascii=False),
        "正文里没有 base64 残留",
        "有残留说明附件抽取没生效，正文列迟早被撑爆",
    )

    if detail["attachments"]:
        for att in detail["attachments"]:
            reporter.info(
                f"附件：{att['filename']} {att['mime_type']} {att['size_text']} "
                f"stored={att['stored']} sha256={(att['sha256'] or '')[:12]}"
            )
        reporter.check(
            all(a["marker"] for a in detail["attachments"]),
            "附件都有短码，能和正文占位符对上",
        )

        # 占位符必须真的出现在正文里，否则详情页挂不上附件卡片
        body = json.dumps(detail["messages"], ensure_ascii=False)
        matched = [a for a in detail["attachments"] if a["marker"] in body]
        reporter.check(
            len(matched) == len(detail["attachments"]),
            "每个附件在正文里都有对应占位符",
            f"{len(matched)}/{len(detail['attachments'])}",
        )

    reporter.check(
        detail.get("response_text") is not None,
        "模型回复已存储（接收方向只存不改）",
    )

    return detail


async def check_attachment_download(
    gw: Gateway, reporter: Reporter, detail: Dict[str, Any]
) -> None:
    """③ 附件本体：真的落盘了，也真的取得回来"""
    reporter.section("③ 附件落盘与回读")

    attachments = detail.get("attachments") or []
    if not attachments:
        reporter.warn("这条会话没有附件，跳过", "上游可能拒绝了多模态请求")
        return

    stored = [a for a in attachments if a["stored"]]
    if not stored:
        reporter.warn(
            "附件都没落盘",
            "; ".join(a["store_error"] or "原因未记录" for a in attachments),
        )
        reporter.info("→ 检查 AUDIT_STORAGE_PATH 目录权限与 AUDIT_STORE_ATTACHMENT_BYTES 开关")
        return

    for att in stored:
        resp = await gw.raw_get(f"/admin/audit/attachment/{att['id']}")

        # ⚠️ 不能只看 status_code。管理端的错误也走 {code,message,data} 信封、
        # 同样返回 **HTTP 200**（本项目既定约定），只有成功时才是文件流。
        # 只判状态码的话，「附件文件已丢失」会被报成「✅ 能下载回来」，
        # 真正的原因被挤到后面「字节数不一致」里，诊断整个错位 ——
        # 而丢文件恰恰是最需要一眼看懂的那类事故（元数据还在，本体没了）。
        envelope = None
        if resp.headers.get("content-type", "").startswith("application/json"):
            try:
                envelope = resp.json()
            except ValueError:
                envelope = None

        if envelope is not None and envelope.get("code") not in (0, None):
            reporter.check(
                False,
                f"附件 #{att['id']} 能下载回来",
                f"网关返回错误信封 code={envelope.get('code')}：{envelope.get('message')}",
            )
            reporter.info(
                "→ 元数据在库里而本体取不到：查 AUDIT_STORAGE_PATH 目录是否被清理或换机后没迁移"
            )
            continue

        ok = resp.status_code == 200 and len(resp.content) > 0
        reporter.check(
            ok,
            f"附件 #{att['id']} 能下载回来",
            f"HTTP {resp.status_code}，{len(resp.content)} 字节",
        )

        if ok:
            reporter.check(
                len(resp.content) == att["size_bytes"],
                f"附件 #{att['id']} 字节数与记录一致",
                f"{len(resp.content)} vs {att['size_bytes']}",
            )
            # 哈希对得上才谈得上取证
            import hashlib
            digest = hashlib.sha256(resp.content).hexdigest()
            reporter.check(
                digest == att["sha256"],
                f"附件 #{att['id']} 内容哈希一致",
                "对不上说明存的和取的不是同一份",
            )


async def check_access_logs(
    gw: Gateway, reporter: Reporter, detail: Optional[Dict[str, Any]]
) -> None:
    """④ 查阅日志：看的人也被看（决策 #8）"""
    reporter.section("④ 查阅行为审计日志")

    data = await gw.admin_get("/admin/audit/access-logs", page=1, page_size=50)
    items = data["items"]

    reporter.check(len(items) > 0, "查阅日志有记录", f"共 {data['total']} 条")
    if not items:
        reporter.info("→ 一条都没有说明 log_access 压根没被调用，决策 #8 没落实")
        return

    actions = {i["action"] for i in items}
    for action, why in (
        ("list", "检索列表"),
        ("detail", "查看全文"),
    ):
        reporter.check(action in actions, f"记录了「{why}」动作")

    # detail 那条必须能精确到「看了谁的哪一条」——这正是选详情抽屉方案的理由
    details = [i for i in items if i["action"] == "detail"]
    if details and detail:
        latest = details[0]
        reporter.check(
            latest["target_conversation_id"] is not None,
            "查看全文的日志精确到单条会话",
            f"会话 #{latest['target_conversation_id']}",
        )
        reporter.check(
            bool(latest["target_user_uid"]),
            "查看全文的日志记下了内容归属人",
            f"{latest['target_name']}（{latest['target_user_uid']}）",
        )

    if any(i["action"] == "attachment" for i in items):
        reporter.check(True, "记录了「查看附件」动作")
    else:
        reporter.warn("没有「查看附件」记录", "上一步可能没有可下载的附件")

    # 列表检索要留下查询条件，日后才回答得了「他是不是在盯着某个人」
    lists = [i for i in items if i["action"] == "list"]
    if lists:
        reporter.check(
            any(i["query_json"] for i in lists),
            "检索日志留下了查询条件",
            (lists[0]["query_json"] or "")[:120],
        )

    for item in items[:5]:
        reporter.info(
            f"{item['created_at']} {item['actor_name']} "
            f"{item['action_label']} "
            f"{'会话 #' + str(item['target_conversation_id']) if item['target_conversation_id'] else '（列表/导出）'} "
            f"来自 {item['client_ip']}"
        )


async def check_usage(
    gw: Gateway, reporter: Reporter, expected_requests: int, baseline: int = 0
) -> None:
    """⑤ 用量与计费：token 进库了吗，细分留住了吗，**一笔都没漏吗**"""
    reporter.section("⑤ 用量统计与计费")

    summary = await gw.admin_get("/admin/usage/summary")
    total = summary["total"]

    reporter.check(total["requests"] > 0, "用量有记录", f"{total['requests']} 次请求")

    # 逐笔核对。第一版脚本只查「有没有记录」，于是流式那次漏记完全没被发现 ——
    # 报表上少一笔看起来和正常没有任何区别，这正是计费漏洞最危险的地方
    actual = total["requests"] - baseline
    reporter.check(
        actual >= expected_requests,
        f"发出去 {expected_requests} 次，记账 {actual} 次",
        "少记就是漏计费。重点查流式：客户端断开时记账代码可能根本没执行到",
    )
    reporter.check(
        total["total_tokens"] > 0,
        "token 已入库",
        f"输入 {total['prompt_tokens']} / 输出 {total['completion_tokens']}",
    )

    if total["estimated_requests"] > 0:
        reporter.warn(
            f"{total['estimated_requests']} 次请求的 token 是估算值",
            "上游没返回 usage —— 流式记得确认 include_usage 生效",
        )
    else:
        reporter.check(True, "全部 token 来自上游实测，无估算")

    # 费用为 None 是正确行为（未配单价），为 0 才是 bug（决策 #22）
    if total["cost"] is None:
        reporter.warn(
            "费用显示为「—」",
            "有模型未配置单价。这是正确行为：按 0 计入会让总额悄悄偏小",
        )
        reporter.info("→ 要看费用估算，请在后台「模型与白名单」为模型填单价")
    else:
        reporter.check(True, "费用可估算", f"{total['cost']} {total['currency']}")
        # 单位错一个数量级时金额仍然「看起来正常」，所以这里做个量级体检。
        # 按 1 元/M token 的典型价，几十次测试调用的费用应该远小于 1 元
        if total["total_tokens"] and total["cost"] > 10:
            reporter.warn(
                f"费用 {total['cost']} 元与 {total['total_tokens']} token 不太相称",
                "确认单价填的是「元 / 百万 token」而不是「元 / 千 token」",
            )

    # ---- 单价快照：这轮调用应当全部按「调用时单价」计 ----
    # basis 不是 snapshot 说明要么 price-snapshot 那份迁移没跑，要么写入侧
    # 没把单价带上。两种情况都不报错，只是费用会一直随日后调价漂移 ——
    # 正是这种「不报错的错」最需要脚本盯着
    basis = total.get("cost_basis")
    if basis == "snapshot":
        reporter.check(True, "费用按调用时单价计（有快照）")
    else:
        reporter.warn(
            f"费用口径为 {basis}，本轮调用没有全部用上单价快照",
            summary.get("cost_note")
            or "确认 2026-08-08-price-snapshot.sql 已跑，"
               "且区间内没有混进快照上线前的历史数据",
        )

    # 明细：token 细分与 usage 原文有没有真的落库（决策 #21）
    records = await gw.admin_get("/admin/usage/records", page=1, page_size=10)
    items = records["items"]
    reporter.check(len(items) > 0, "调用明细有记录")

    if items:
        latest = items[0]
        reporter.info(
            f"最近一次：{latest['model_name']} "
            f"{latest['total_tokens']} token / {latest['latency_ms']}ms "
            f"缓存命中 {latest['cached_prompt_tokens']} 推理 {latest['reasoning_tokens']}"
        )
        reporter.check(
            "prompt_tokens" in latest and "cached_prompt_tokens" in latest,
            "token 细分字段已下发",
            "缓存读写/推理 token 现在报表用不上，但不存就永远追不回来",
        )

        # ---- 流式 / 直接要分得清 ----
        streamed = [i for i in items if i["stream"]]
        direct = [i for i in items if not i["stream"]]
        reporter.check(
            bool(streamed) and bool(direct),
            "明细能区分流式与直接请求",
            f"流式 {len(streamed)} 条 / 直接 {len(direct)} 条",
        )

        # ---- 中断的那次：必须入库，且**不能被算成失败** ----
        aborted = [i for i in items if i.get("aborted")]
        reporter.check(
            bool(aborted),
            "中途断开的调用已入库",
            "一条都没有说明记账仍挂在请求生命周期上，用户点「停止生成」就丢账",
        )
        if aborted:
            case = aborted[0]
            reporter.check(
                case["status"] == "success",
                "中断的调用不算失败",
                f"status={case['status']} —— 算成失败会污染上游失败率这个指标",
            )
            reporter.check(
                case["total_tokens"] > 0,
                "中断前产生的 token 已计费",
                f"{case['total_tokens']} token",
            )

        # ---- 单价快照落到明细行上了吗 ----
        # 明细页要能回答「这笔怎么这么贵」，那就得看得到当时按什么价算的
        if latest.get("cost") is None:
            reporter.info(
                "最近一次调用的费用为「—」："
                "该模型当时未配单价（正确行为，绝不按 0 计入）"
            )
        else:
            reporter.check(
                latest.get("billing_mode") is not None,
                "明细带单价快照",
                f"{latest['cost']} {latest.get('cost_currency')} / "
                f"{latest.get('billing_mode')} "
                f"输入 {latest.get('input_price_per_1m')} 输出 {latest.get('output_price_per_1m')}",
            )

        # ---- 请求ID：从明细追到会话原文的钥匙 ----
        reporter.check(
            bool(latest.get("request_id")),
            "明细带请求ID（可据此定位会话原文）",
            latest.get("request_id", ""),
        )

        failed = [i for i in items if i["status"] != "success"]
        if failed:
            reporter.check(
                True,
                "失败调用也记了账",
                f"{len(failed)} 条失败：{failed[0]['error_code']}",
            )

    # 明细接口绝不能顺带下发聊天内容 —— 部门负责人也能看这个页面
    if items:
        leaked = [k for k in ("user_input", "response_text", "preview") if k in items[0]]
        reporter.check(not leaked, "用量明细不含聊天内容", f"泄漏字段：{leaked}" if leaked else "")


async def check_export(gw: Gateway, reporter: Reporter) -> None:
    """⑥ 导出：只出摘要，且留痕"""
    reporter.section("⑥ 导出")

    resp = await gw.raw_get("/admin/audit/export")
    ok = resp.status_code == 200
    reporter.check(ok, "审计 CSV 能导出", f"HTTP {resp.status_code}，{len(resp.content)} 字节")

    if ok:
        text = resp.content.decode("utf-8-sig", errors="replace")
        reporter.check("时间,用户,部门" in text, "CSV 表头正确")
        reporter.check(
            "base64" not in text,
            "导出内容不含附件原文",
        )

    logs = await gw.admin_get("/admin/audit/access-logs", page=1, page_size=10)
    reporter.check(
        any(i["action"] == "export" for i in logs["items"]),
        "导出行为被记进查阅日志",
        "数据离开系统的时刻最该留痕",
    )


# ==================== 主流程 ====================


