"""技能发布规范（2026-10-16）—— 「习得时就按发布标准写」与「整理并发布」共用的一份口径。

以前两头各说各的：Agent 习得技能时只被提醒补中文名/中文说明，而「整理并发布」的 AI
按另一套章节重排正文、再逐条找个人信息。于是几乎每条技能发布前都要整理一遍。
这里把规范定成一份，四处引用：

- `skill_manage` 的工具说明（Agent 创建/改写技能时看到的）；
- 后台技能复盘的提示词（品牌版才追加）；
- 技能检查器的品牌版规则（创建后给 Agent 的提醒、发布前的机器检查）；
- 「整理并发布」的 AI 整理提示词。

只放文本和纯函数，不 import 工具模块，谁都能引用而不产生循环依赖。
"""
from __future__ import annotations

import re
from typing import List

# 正文章节，顺序即推荐顺序。用二级标题 `## 适用场景` 这种写法。
SECTIONS = ("适用场景", "前置条件", "步骤", "注意事项", "怎么验证")

# 给 Agent 看的英文说明（工具说明和复盘提示词都是英文），章节名保留中文原文。
AUTHORING_SPEC_EN = (
    "Company publishing standard (this product) — write every skill so it can be shared "
    "with colleagues as-is: frontmatter has a Chinese `display_name` (<=20 chars) and a "
    "one-sentence Chinese `description` (trigger first); the body is in Chinese with the "
    "sections " + ", ".join(f"`## {s}`" for s in SECTIONS) + "; keep commands and code "
    "blocks verbatim; never write personal or business data (phone numbers, ID/bank card "
    "numbers, emails, keys/passwords, local user paths like /Users/<name>, internal IPs, "
    "real customer names, amounts, order IDs) — use placeholders like {客户名} or <order_id>."
)

# 给「整理并发布」AI 用的中文正文结构要求。
REFINE_BODY_RULE_ZH = "正文用中文，按「" + "、".join(SECTIONS) + "」组织，每节用二级标题（## 标题）"

_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.M)


def missing_sections(body: str) -> List[str]:
    """正文里缺哪些规范章节（按标题文字包含判断，`## 步骤（详细）` 也算有）。"""
    headings = [h.strip() for h in _HEADING.findall(re.sub(r"```.*?```", "", body or "", flags=re.S))]
    return [s for s in SECTIONS if not any(s in h for h in headings)]
