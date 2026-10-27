"""技能正文里的业务数据样本扫描 —— P0-4 的第二条约束

## 要挡的是什么

「**skill 正文不得包含业务数据样本。** agent 总结『上月平台销量怎么查』时极可能把
示例响应连数值一起写进去 —— 移交给管理员等于把数据换个地方继续留存，还脱离了
会话审计的留存期管辖。」

最后半句是重点：会话记录受留存期制度管辖，会到期清理；而一条 skill 是**长期资产**，
没有到期这回事。把订单号、金额、店铺名写进 skill，等于给这些数据开了一条绕过留存期
的旁路，而且没有任何一处会报错。

## 这是**提示**，不是拦截

规则原话是「在 skill 生成的提示词层就约束，移交前再扫一遍」—— 两道都是软的。
理由与阶段 7 那套一致：靠启发式判断「这串数字是不是业务数据」既会漏也会误杀，
而误杀一条 skill 赔进去的是员工的技能指标。所以这里只产出 findings，
由调用方决定怎么呈现，**不阻断任何写入**。

## 占位符要能认出来，否则整套规则会被无视

规则要求「示例值一律占位符」。如果扫描器把 `<订单号>`、`{order_id}`、`xxx` 也报出来，
那么正确写法和错误写法一样吵 —— 人很快就会把这类提示全部忽略，包括真的那些。
所以占位符必须**先**被排掉。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List

# 占位符长什么样。先排掉这些，否则「按规则写的」和「没按规则写的」一样吵。
_PLACEHOLDER = re.compile(
    r"<[^>\n]{1,40}>"           # <订单号>
    r"|\{\{?[^}\n]{1,40}\}?\}"   # {order_id} / {{order_id}}
    r"|\$\{[^}\n]{1,40}\}"       # ${ORDER_ID}
    r"|\b[xX]{3,}\b"             # xxx / XXXX
    r"|\*{3,}"                   # ***
    r"|\bYOUR_[A-Z0-9_]+\b"
    r"|\b(?:示例|样例|占位|例如)\b"
)

# 长数字串：订单号、手机号、时间戳。8 位是个折中 —— 再短会把年份、页码、
# HTTP 状态码全捞进来，那些是正常的接口说明内容。
_LONG_DIGITS = re.compile(r"(?<!\d)\d{8,}(?!\d)")
# 金额。带货币符号或明确的小数金额。
_MONEY = re.compile(r"[¥$€£]\s?\d[\d,]*(?:\.\d{1,2})?|\b\d[\d,]*\.\d{2}\s?(?:元|美元|USD|CNY)\b")
# ASIN。对这家公司信号极强 —— 领星的接口返回里到处是它。
_ASIN = re.compile(r"\bB0[A-Z0-9]{8}\b")
# 一行 markdown 表格里有两个及以上纯数字单元格 —— 多半是贴回来的结果表。
_TABLE_ROW = re.compile(r"^\s*\|(?:[^|\n]*\|){2,}\s*$")
_TABLE_NUMERIC_CELL = re.compile(r"^\s*[\d,]+(?:\.\d+)?\s*$")

# 围栏代码块里的 JSON，值不是占位符 —— 典型的「把响应原样贴进来」。
_JSON_VALUE = re.compile(r'"[\w_]+"\s*:\s*(?:"(?P<s>[^"\n]{2,})"|(?P<n>-?\d[\d.]*))')


@dataclass(frozen=True)
class DataFinding:
    line: int
    kind: str
    excerpt: str
    hint: str


def _clean(line: str) -> str:
    """把占位符抠掉再判断。"""
    return _PLACEHOLDER.sub(" ", line)


def _excerpt(text: str, limit: int = 80) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"


def scan_text(text: str) -> List[DataFinding]:
    """扫一段 skill 正文，返回可能是业务数据样本的地方。

    只报**位置和类型**，不试图判断严重程度 —— 严重程度要看这串数字到底是什么，
    而那正是启发式做不到的事。让人看一眼比给一个假的置信度有用。
    """
    findings: List[DataFinding] = []
    in_fence = False

    for i, raw in enumerate(text.splitlines(), 1):
        if raw.lstrip().startswith("```"):
            in_fence = not in_fence
            continue

        line = _clean(raw)

        if _ASIN.search(line):
            findings.append(DataFinding(i, "asin", _excerpt(raw),
                                        "像是真实 ASIN，换成占位符"))
        if _MONEY.search(line):
            findings.append(DataFinding(i, "money", _excerpt(raw),
                                        "像是真实金额，换成占位符"))
        if _LONG_DIGITS.search(line):
            findings.append(DataFinding(i, "long_digits", _excerpt(raw),
                                        "像是订单号/ID/时间戳，换成占位符"))

        if _TABLE_ROW.match(raw):
            cells = [c for c in raw.strip().strip("|").split("|")]
            numeric = sum(1 for c in cells if _TABLE_NUMERIC_CELL.match(_clean(c)))
            if numeric >= 2:
                findings.append(DataFinding(i, "result_table", _excerpt(raw),
                                            "像是贴回来的结果表，只留表头与字段说明"))

        if in_fence:
            for match in _JSON_VALUE.finditer(line):
                value = match.group("s") or match.group("n") or ""
                # 字段名、类型名、枚举值这类短字符串是接口说明的正常内容。
                if match.group("s") and len(value) <= 3:
                    continue
                findings.append(DataFinding(i, "response_sample", _excerpt(raw),
                                            "像是原样贴回来的响应，值换成占位符"))
                break

    return findings


def summarize(findings: List[DataFinding]) -> Dict[str, Any]:
    """给调用方一份可以直接展示的账目。"""
    by_kind: Dict[str, int] = {}
    for f in findings:
        by_kind[f.kind] = by_kind.get(f.kind, 0) + 1
    return {
        "count": len(findings),
        "by_kind": by_kind,
        "findings": [
            {"line": f.line, "kind": f.kind, "excerpt": f.excerpt, "hint": f.hint}
            for f in findings
        ],
    }


# 给模型看的那句话。规则要求「在 skill 生成的提示词层就约束」——
# 扫描是事后补救，提示词才是让它一开始就别写进去的地方。
PROMPT_RULE = (
    "Never paste real business data into a skill: no real order IDs, ASINs, "
    "amounts, store names, or captured API responses. Skills are long-lived "
    "assets and are NOT covered by the conversation retention policy, so data "
    "written into one outlives the audit trail it came from. "
    "Write interfaces, parameters and steps only; replace every example value "
    "with a placeholder like <order_id>."
)
