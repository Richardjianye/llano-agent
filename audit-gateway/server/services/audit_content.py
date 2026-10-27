"""审计正文的写入侧整形 —— 决策 #52 的内容模型

## 只留「问了什么 / 答了什么」

审计要回答的问题就两个：**员工问了什么、模型答了什么**。其余的都降级：

| 内容 | 存成什么 |
| ---- | -------- |
| 本次用户输入 | **全文**（独占额度，不和任何东西抢） |
| 模型回复 | 全文（`response_text`，接收方向只存不改） |
| 系统提示词 | **只存 sha256 + 字数** |
| 本地工具链 | **只存骨架**：哪个工具、各多长 |
| 思考链 | 不存（计费照算，正文一个字不留） |

系统提示词每条记录一模一样，员工也改不了，全文入库纯占体积；一个指纹就够
回答「这些记录用的是不是同一版提示词」。工具用了哪些、跑了哪些模型，对
「这个人在拿公司数据干什么」这个问题没有信息量。

⚠️ **知情取舍（负责人 2026-08-21 拍板）**：本地工具的结果是以 `tool` 消息进
模型上下文的 —— 员工的 agent 读了本机文件、抓了内网页面，那些数据确实出了网。
停存正文后，「什么公司数据被发给了模型」在审计里**只剩体量不剩内容**。
骨架保住的是「这次对话往模型里灌了 42KB 本地数据」这个信号。
**MCP 调用不受影响**：`mcp_call_logs` 单独记 capability、参数、状态、耗时。

## 上一版留下的教训：别切 JSON 字符串

改成这个模型之前，这里存的是整个 messages 数组，`json.dumps(...)[:N]` 那一刀
正好落在系统提示中间，存进库的是**半截 JSON**。读取侧 `json.loads` 必然失败，
退回「整坨当成一条 role=unknown」的兜底分支，连锁反应有三个，当时全都没人
看出来是同一个 bug：详情页的「本次用户输入」永远不出现、详情页只剩一大坨
系统提示、列表页每一行摘要都长得一模一样。

新模型下这个坑物理上不存在了 —— 用户输入是独立一列纯文本，没有结构可切。
**但 `tool_skeleton` 是 JSON**，那一列只装名字和字数、天然短，
不会走到需要截断的地步；真要给它加限制时记得：**限条数，不要切字符串**。

## 为什么用户输入要保头也保尾

`> ## Documentation Index …（8000 字文档）… 这个工作流为什么报错？`

这是真实记录的形状：**粘贴的上下文在前，要问的那句话在最后一行**。只截头的话
入库的是 8000 字文档加半个「这」字，问题本身一个字都没落库；列表摘要取开头
120 字，于是每条记录都长成 `> ## Documentation Index`，看不出任何区别。

所以用户输入一律**头尾都留**，中间省略并标出省了多少字。模型回复仍然只截头：
回复的信息在开头。正常提问根本够不着上限 —— 用户输入不再和 13943 字的系统
提示抢预算了，`elide_middle` 只在超长粘贴时才真的动手。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

# 单条会话内容的存储上限，超出截断并标记。
# 不设上限的话，一次长文档粘贴就能把行撑到几 MB，备份和查询都会难受。
# 现在这个额度**用户输入独占**，不和系统提示抢 —— 正常提问根本够不着
CONTENT_MAX_CHARS = 20000

# 工具骨架最多记多少条。超出只留前 N 条并在末尾补一行「还有 M 条」。
# ⚠️ 限的是**条数**不是字符：那一列是 JSON，切字符串会切出半截数组，
# 正是上面模块注释里那个 bug 的形状
TOOL_SKELETON_MAX_ENTRIES = 200

TRUNCATION_SUFFIX = "…（已截断）"

# 头尾都保留时，尾部拿走的比例。用户消息的形状通常是「一大段粘贴 + 最后一句问题」，
# 问题短、上下文长，所以尾部不需要对半分；四成足够装下一整段追问
TAIL_KEEP_RATIO = 0.4

# 头尾保留的最小可用额度。低于这个数，头尾各剩十几个字加一个省略标记，
# 读起来比单纯截头更糟，那就退回截头
MIN_ELIDE_CHARS = 80

_ELISION_TEMPLATE = "…（中间省略 {omitted} 字）…"


def _content_text(message: Any) -> str:
    """取一条消息的纯文本，兼容 str 与 OpenAI 的多模态 parts 数组。"""
    if not isinstance(message, dict):
        return ""

    content = message.get("content")

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        return "\n".join(
            part["text"]
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )

    return "" if content is None else str(content)


def last_user_text(messages: Any) -> str:
    """本次调用里用户真正问的那句话。

    Hermes 每轮都把完整历史重发一遍，协议约定当前输入是**最后**一条
    role=user。倒序找，不做字符串截尾 —— 避免把末尾的工具结果或附件占位符
    误当成用户的问题。

    与读取侧 `audit_query.latest_user_message` 是同一条规则，区别只是这里吃
    原始 dict、那边吃解析后的对象。两边都改的时候记得对齐。
    """
    if not isinstance(messages, list):
        return ""

    for message in reversed(messages):
        if isinstance(message, dict) and str(message.get("role") or "") == "user":
            return _content_text(message)

    return ""


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text

    return text[: max(0, limit - len(TRUNCATION_SUFFIX))] + TRUNCATION_SUFFIX


def elide_middle(text: str, limit: int, *, tail_ratio: float = TAIL_KEEP_RATIO) -> str:
    """保头也保尾地压到 `limit` 字以内，中间省略并标出省了多少字。

    省略标记本身要占额度，而它的长度取决于省略字数，省略字数又取决于标记长度 ——
    互相依赖，所以迭代到不动点。

    不动点上两件事同时成立：留下的字数 `keep` 恰好等于 `limit - 标记长度`，
    且标记里写的省略字数恰好是 `len(text) - keep`。于是结果长度**精确等于**
    `limit`，不需要事后再补一刀（补过，是够不着的死代码）。

    收敛性：`keep → limit - 标记长度(len(text) - keep)` 随 keep 单调不减且
    首轮必然下降，序列单调不增且有下界；标记长度只随省略字数的十进制位数变化，
    而每轮 keep 只挪动几个字，位数至多变一两次。四轮足够，且由
    `test_elide_middle_绝不超限` 逐个额度扫过来钉死。
    """
    if limit <= 0:
        return ""

    if len(text) <= limit:
        return text

    keep = limit
    for _ in range(4):
        next_keep = limit - len(_ELISION_TEMPLATE.format(omitted=len(text) - keep))
        if next_keep == keep:
            break
        keep = next_keep

    if keep < MIN_ELIDE_CHARS:
        return _cut(text, limit)

    tail = max(1, int(keep * tail_ratio))
    head = keep - tail

    return text[:head] + _ELISION_TEMPLATE.format(omitted=len(text) - keep) + text[-tail:]


def make_user_preview(text: str, limit: int) -> str:
    """列表页那一行的摘要 —— 和正文一样保头也保尾。

    和 `audit_query.make_preview` 分开是刻意的：那个用在**模型回复**的摘要上，
    回复的信息在开头，截头就够。用户输入不是这个形状（见模块注释），两者共用
    一个函数只会逼出一个 `keep_tail=` 开关，然后总有一个调用点忘记传。
    """
    if not text:
        return ""

    flat = re.sub(r"\s+", " ", text).strip()

    return elide_middle(flat, limit)


def truncate_text(text: str, *, max_chars: int = CONTENT_MAX_CHARS) -> Tuple[str, bool]:
    """回复正文的截断 —— 那一列是纯文本，直接切就行，没有结构要保。"""
    if text is None:
        return text, False

    if len(text) <= max_chars:
        return text, False

    return text[:max_chars] + TRUNCATION_SUFFIX, True


# ==================== 决策 #52 的内容模型 ====================


@dataclass(frozen=True)
class ShapedAudit:
    """一次调用里要落库的审计内容"""

    user_input: str
    system_prompt_sha256: Optional[str]
    system_prompt_chars: int
    # JSON 数组文本 `[{"name": …, "chars": …}]`，没有工具消息时为 None。
    # 存 None 而不是 "[]"：查询时 `IS NULL` 一眼看出「这次没用工具」，
    # 而 "[]" 要解析完才知道，且和「解析失败」不好区分
    tool_skeleton: Optional[str]
    truncated: bool


def system_prompt_text(messages: Any) -> str:
    """把所有 role=system 消息拼起来。

    拼而不是只取第一条：Hermes 正常只发一条，但 provider profile、
    工具说明有时会追加第二条 system。只按第一条算指纹的话，
    第二条变了指纹却不变 —— 那这个指纹就答不出「是不是同一版提示词」了。
    """
    if not isinstance(messages, list):
        return ""

    return "\n".join(
        _content_text(message)
        for message in messages
        if isinstance(message, dict) and str(message.get("role") or "") == "system"
    )


def _tool_name(message: dict, by_call_id: Dict[str, str]) -> str:
    """这条 tool 结果是哪个工具产生的。

    OpenAI 协议里 tool 消息**不一定带 name**，规范的做法是拿
    `tool_call_id` 回查上一条 assistant 的 `tool_calls`。两条路都试，
    都取不到才落 `unknown` —— 骨架里最有价值的就是这个名字，
    宁可多查一次也别整列写满 unknown。
    """
    name = str(message.get("name") or "").strip()
    if name:
        return name

    call_id = str(message.get("tool_call_id") or "").strip()

    return by_call_id.get(call_id, "unknown")


def tool_skeleton(messages: Any) -> Optional[str]:
    """本地工具链只存骨架：哪个工具、各多长。

    ⚠️ 正文一个字不存 —— 这是知情取舍，见模块注释。这里保住的是
    「这次对话往模型里灌了多少本地数据」这个信号。
    """
    if not isinstance(messages, list):
        return None

    by_call_id: Dict[str, str] = {}
    entries: List[Dict[str, Any]] = []

    for message in messages:
        if not isinstance(message, dict):
            continue

        role = str(message.get("role") or "")

        if role == "assistant":
            # 先扫 assistant 的 tool_calls 建索引，下面的 tool 消息才认得出名字。
            # 顺序天然成立：调用一定排在结果前面
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                call_id = str(call.get("id") or "").strip()
                function = call.get("function")
                if call_id and isinstance(function, dict):
                    by_call_id[call_id] = str(function.get("name") or "").strip() or "unknown"
            continue

        if role != "tool":
            continue

        entries.append(
            {"name": _tool_name(message, by_call_id), "chars": len(_content_text(message))}
        )

    if not entries:
        return None

    if len(entries) > TOOL_SKELETON_MAX_ENTRIES:
        omitted = len(entries) - TOOL_SKELETON_MAX_ENTRIES
        entries = entries[:TOOL_SKELETON_MAX_ENTRIES]
        entries.append({"name": f"（还有 {omitted} 次工具调用未记录）", "chars": 0})

    return json.dumps(entries, ensure_ascii=False)


def shape_audit_content(
    messages: Any, *, max_chars: int = CONTENT_MAX_CHARS
) -> ShapedAudit:
    """把一次调用的 messages 整形成要落库的四样东西。

    纯函数，不碰数据库 —— 内容模型是「错了也不报错、只是审计悄悄看不到东西」
    的地方，必须能脱离 MySQL 单测。
    """
    prompt = system_prompt_text(messages)
    raw_input = last_user_text(messages)
    shaped_input = elide_middle(raw_input, max_chars)

    return ShapedAudit(
        user_input=shaped_input,
        # 空提示词存 None 而不是空串的 sha256：后者是个看起来很正常的
        # 64 位十六进制，一眼分不出「没有系统提示」和「系统提示是空的」
        system_prompt_sha256=(
            hashlib.sha256(prompt.encode("utf-8")).hexdigest() if prompt else None
        ),
        system_prompt_chars=len(prompt),
        tool_skeleton=tool_skeleton(messages),
        truncated=len(shaped_input) < len(raw_input),
    )
