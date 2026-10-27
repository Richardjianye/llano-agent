"""会话审计的读取侧 —— 纯逻辑部分

写入侧在 services/usage.py + services/attachments.py（挂在转发链路上），
这里只管「怎么把已经落库的会话切出来给人看」。

放在 services 层而不是路由里，理由和 usage_query.py 一样：摘要提取、
占位符还原、CSV 组装都是容易写错又不容易发现的地方，得能脱离 MySQL 单测。

## 三条必须守住的口径

1. **列表不下发全文。** 只给 `preview` 摘要。这不只是省带宽 ——
   查阅日志的颗粒度直接取决于此：列表给了全文，`detail` 那条日志就永远
   只能写成「看了第 3 页」，事后追责基本没用（决策 #8）。
2. **接收方向只存不改（决策 #9）。** 模型回复原样呈现，不做任何二次脱敏。
   审计员看到的必须是模型真实说过的话。
3. **截断要显式标出来。** 超过 CONTENT_MAX_CHARS 的内容入库时就被切了，
   详情页必须告诉审计员「你看到的不是全部」，否则会得出错误结论。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# 列表页摘要长度。够看出「在聊什么」，又不至于让列表接口变相下发全文
PREVIEW_MAX_CHARS = 120

# 一次列表查询最多返回多少条。审计是逐条看的活，不是导数据的活；
# 真要批量拿走请走 export（那会留下一条 export 审计日志）
MAX_PAGE_SIZE = 100

# 关键词的最短长度。允许单字符会退化成全表 LIKE '%a%'，
# 在几百万行的会话表上就是一次事故
MIN_KEYWORD_LEN = 2

# 翻页深度上限（page × page_size）。深翻页的 OFFSET 是数据库逐行数过去的，
# 第 500 页比第 1 页慢两个数量级；而且真要看到第 500 页，
# 该做的是缩小时间范围或加筛选条件，不是一直点下一页
MAX_PAGE_DEPTH = 10000

# 会话记录的状态取值。与 usage_records 共用同一套（success/failed）——
# 「中断」不在其中，它是正交的 aborted 列（决策 #28）
CONVERSATION_STATUSES = ("success", "failed")

# 占位符形如 [[attachment:ab12cd34ef56 图.png image/png 240KB]]
_MARKER_RE = re.compile(r"\[\[attachment:([0-9a-f]{6,32})([^\]]*)\]\]")


class AuditQueryError(ValueError):
    """查询参数不合法 —— 由路由层转成 400"""


def normalize_keyword(keyword: Optional[str]) -> Optional[str]:
    """校验并规范化关键词

    LIKE 的通配符要转义掉：用户搜「100%」不该变成匹配任意内容。
    """
    if keyword is None:
        return None

    keyword = keyword.strip()
    if not keyword:
        return None

    if len(keyword) < MIN_KEYWORD_LEN:
        raise AuditQueryError(f"关键词至少 {MIN_KEYWORD_LEN} 个字符，太短会拖垮全表扫描")

    return keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def normalize_page_size(page_size: int) -> int:
    if page_size < 1:
        raise AuditQueryError("每页条数必须为正整数")
    if page_size > MAX_PAGE_SIZE:
        raise AuditQueryError(
            f"每页最多 {MAX_PAGE_SIZE} 条 —— 需要批量取数请用导出功能（会记审计日志）"
        )
    return page_size


def normalize_page(page: int, page_size: int) -> int:
    """校验翻页深度

    OFFSET 是数据库一行行数过去的，翻到几百页时单次查询能慢上两个数量级，
    而会话表还在随调用量线性增长。挡在这里比让人「点着点着页面就转圈」清楚得多。
    """
    if page < 1:
        raise AuditQueryError("页码必须为正整数")
    if page * page_size > MAX_PAGE_DEPTH:
        raise AuditQueryError(
            f"翻页深度超过 {MAX_PAGE_DEPTH} 条 —— 请缩小时间范围或增加筛选条件"
        )
    return page


def normalize_choice(
    value: Optional[str], allowed: tuple, label: str
) -> Optional[str]:
    """把枚举型筛选值卡在白名单内

    不校验的话，`status=succes`（少个 s）这类笔误会安安静静地查出 0 条，
    而审计员看到的是「这个人这段时间没用过 AI」—— 一个完全错误却很有说服力的结论。
    查询条件写错必须报错，不能表现成「查无此人」。
    """
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if value not in allowed:
        raise AuditQueryError(f"{label}非法，可选值：{', '.join(allowed)}")
    return value


# ==================== 正文解析 ====================


def make_preview(text: str, limit: int = PREVIEW_MAX_CHARS) -> str:
    """列表页摘要

    换行压成空格：列表是单行显示的，带换行会让 CSV 导出也跟着错行。
    """
    if not text:
        return ""
    flat = re.sub(r"\s+", " ", text).strip()
    if len(flat) <= limit:
        return flat
    return flat[:limit] + "…"


def extract_markers(text: str) -> List[str]:
    """取出正文里的附件短码，按出现顺序去重"""
    seen: List[str] = []
    for match in _MARKER_RE.finditer(text or ""):
        marker = match.group(1)
        if marker not in seen:
            seen.append(marker)
    return seen


def format_size(size_bytes: Optional[int]) -> str:
    """人类可读的体积。远程附件没有字节数，显示「未知」而不是 0 B"""
    if not size_bytes:
        return "未知"
    units = ("B", "KB", "MB", "GB")
    value = float(size_bytes)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


# ==================== 导出 ====================

CSV_HEADERS = (
    "时间", "用户", "部门", "角色", "模型", "状态",
    "附件数", "是否截断", "服务端脱敏", "请求摘要", "回复摘要",
)


def to_csv(rows: List[Dict[str, Any]], note: str = "") -> str:
    """导出 CSV —— **只导摘要，不导全文**

    这是个刻意的取舍：全文导出等于把整个会话库打包带走，
    一旦落到本地磁盘就彻底脱离了审计与留存期限的管辖。
    要看全文请逐条点开（那会各留下一条 detail 日志）。

    BOM 由路由层加在响应体最前面，这里只出纯文本。
    """
    import csv
    import io

    from utils.csv_export import safe_csv_cell

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_HEADERS)

    for row in rows:
        writer.writerow([safe_csv_cell(value) for value in [
            row.get("created_at") or "",
            row.get("username") or row.get("user_uid") or "",
            row.get("dept_name") or "",
            row.get("role") or "",
            row.get("model_name") or "",
            row.get("status") or "",
            row.get("attachment_count") or 0,
            "是" if row.get("truncated") else "否",
            "是" if row.get("masked") else "否",
            row.get("preview") or "",
            row.get("response_preview") or "",
        ]])

    if note:
        writer.writerow([])
        writer.writerow([note])

    return buffer.getvalue()


ACCESS_LOG_CSV_HEADERS = (
    "时间", "查阅人", "查阅时角色", "动作",
    "被查阅人", "会话ID", "附件ID", "结果条数", "来源IP", "查询条件",
)


def access_logs_to_csv(rows: List[Dict[str, Any]], note: str = "") -> str:
    """查阅日志导出 CSV

    这份是拿去交材料的（合规检查、内部纪律核查），所以和会话导出不同：
    它**不含任何聊天内容**，只有「谁在什么时候看了谁的什么」，
    因此可以放心落到本地磁盘，没有会话正文那层留存期限的顾虑。
    """
    import csv
    import io

    from utils.csv_export import safe_csv_cell

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(ACCESS_LOG_CSV_HEADERS)

    for row in rows:
        writer.writerow([safe_csv_cell(value) for value in [
            row.get("created_at") or "",
            row.get("actor_name") or row.get("actor_uid") or "",
            row.get("actor_role") or "",
            row.get("action_label") or row.get("action") or "",
            row.get("target_name") or row.get("target_user_uid") or "",
            row.get("target_conversation_id") or "",
            row.get("target_attachment_id") or "",
            row.get("result_count") or 0,
            row.get("client_ip") or "",
            row.get("query_json") or "",
        ]])

    if note:
        writer.writerow([])
        writer.writerow([note])

    return buffer.getvalue()


def parse_tool_skeleton(raw: Optional[str]) -> List[Dict[str, Any]]:
    """把 `tool_skeleton` 那一列解析成 `[{"name","chars"}]`。

    坏数据一律当成「没有工具」而不是抛错：这一列只影响详情页多显示一段，
    让整个详情接口因为一行历史脏数据 500，代价和收益完全不成比例。

    ⚠️ 逐项校验而不是信 `json.loads` 的结果：这列的内容最终来自客户端发来的
    消息数组，写入侧虽然只取名字和长度，读取侧也不该假设它一定是那个形状。
    """
    if not raw:
        return []

    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []

    if not isinstance(parsed, list):
        return []

    entries: List[Dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        try:
            chars = max(0, int(item.get("chars") or 0))
        except (TypeError, ValueError):
            chars = 0
        entries.append({"name": str(item.get("name") or "unknown"), "chars": chars})

    return entries


def summarize_tools(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把工具骨架按工具名汇总：各调了几次、各灌了多少字进模型上下文。

    **按字数降序，不是按次数。** 审计在这一层要回答的是「哪个工具把最多的
    公司数据送出去了」—— 调了 40 次每次 20 字的 terminal，远不如调了 3 次
    每次 18000 字的 read_file 重要，按次数排会把后者压到看不见的地方。

    放服务端而不是前端：这是有确定输入输出的纯逻辑，放这儿能被 pytest 覆盖；
    放前端的话，这个项目里它就是一段没有任何测试的代码。
    """
    summary: Dict[str, Dict[str, Any]] = {}

    for entry in entries:
        name = str(entry.get("name") or "unknown")
        item = summary.setdefault(name, {"name": name, "count": 0, "chars": 0})
        item["count"] += 1
        item["chars"] += int(entry.get("chars") or 0)

    # 字数相同时按名字排，结果才是稳定的 —— 否则同一条记录刷新两次
    # chip 顺序可能不一样，看的人会以为数据变了
    return sorted(summary.values(), key=lambda item: (-item["chars"], item["name"]))


def turn_tool_delta(
    current: List[Dict[str, Any]], previous: Optional[List[Dict[str, Any]]]
) -> Optional[List[Dict[str, Any]]]:
    """这一轮**新增**的工具调用（相对上一轮）。

    ## 为什么不能直接用骨架本身

    `tool_skeleton` 记的是**这次请求的上下文里有哪些工具结果**，而 Hermes
    每轮都把完整历史重发一遍 —— 所以第 58 轮的骨架里躺着前 57 轮的全部工具
    结果。直接展示的话，会话里每一行都写着 `terminal × 43`，读起来像是每轮都
    调了 43 次 terminal。**那是个看起来很正常的谎**：数字对得上，含义是错的。

    ## 怎么对齐

    历史是追加的，所以上一轮的骨架应当是这一轮的**前缀**。是前缀就取尾巴那段，
    那就是这一轮新做的事。

    对不齐（上下文压缩重写过历史、跨页取不到上一轮、会话第一轮）返回 `None`
    而不是空列表 —— `None` 是「算不出」，`[]` 是「这一轮没调工具」，
    两者在界面上该长得不一样。把算不出显示成「没调用」是又一个看起来很正常的谎。
    """
    if previous is None:
        return None
    # 只需要这一个判断。`current` 比 `previous` 短时，`current[:len(previous)]`
    # 就是 `current` 本身，长度不同必然不相等，一样落进这里 —— 曾经在上面
    # 多写了一个 `len(current) < len(previous)` 的早退，变异验证证明它**永远
    # 不会被走到**（拆掉它测试不报红），于是删掉。这和 2026-08-21 在
    # elide_middle 里删掉的那个事后守卫是同一个形状：两个守卫互相遮蔽，
    # 看起来都在防守，其实只有一个在工作
    if current[: len(previous)] != previous:
        return None

    return current[len(previous):]
