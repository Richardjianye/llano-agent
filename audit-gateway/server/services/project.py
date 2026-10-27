"""协作（项目群聊）的判定与文本处理 —— 阶段 13

设计决议见 TODO「协作（群聊）功能设计决议」整章。这一层负责三件事：
**成员判定**、**服务端脱敏**、**@ 解析**。

## 服务端脱敏（P0-2）：为什么不能复用客户端那条

现有脱敏（决策 #9/#37）是**下发策略、客户端执行**的，覆盖的是「发往模型上游」
这一个方向。群聊里发给其他部门 / 其他公司的人同样是内容外流，但方向完全不同：

- 发往模型：发送方的客户端替换，替换失败最坏是把关键词交给模型厂商
- 发往同事：**接收方是人**，而发送方的客户端不可信 —— 它跑在员工自己的机器上

指望发送方替换等于把一条防线交给被防的那一侧。所以群聊这条必须在**服务端**做一遍，
投递出去的永远是服务端替换过的那份。

⚠️ 两边必须用**同一份关键词表和同一套语义**（大小写不敏感、长词优先）。各自实现
   会各自漂移，而漂移的表现是「某个词在私聊里被遮了、在群聊里没遮」，没有任何
   一处会报错。所以这里直接调 redaction_policy.normalized_keywords，不另起一份。

## @ 解析放在服务端，不听客户端

"@ 时必须回复" 是强制触发条件。让客户端提交一份 mentions 列表的话，注入一句
「就说你被 @ 了」就能让 agent 强制回复 —— 与决策 #45 那条「身份绝不能由 agent
提供」是同一类问题。所以 mentions 从正文里按**成员名单**解析，客户端说什么不算。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

# 成员种类
KIND_USER = "user"
KIND_AGENT = "agent"
KIND_SYSTEM = "system"

# 成员角色
ROLE_OWNER = "owner"
ROLE_MEMBER = "member"
ROLE_VIEWER = "viewer"
# viewer 只读。**发言也算写** —— 一个能在群里说话的人已经能影响项目走向了，
# 把「只读」理解成「不能改任务但能说话」会让这个角色形同虚设。
WRITE_ROLES = frozenset({ROLE_OWNER, ROLE_MEMBER})

ACTIVE = "active"
LEFT = "left"
ARCHIVED = "archived"

# 项目标识。与 skill hub 的 slug 同一套字符集，理由也一样：它会进 URL、
# 也可能变成 Gitea 仓库名，容错到「自动改写」只会让人搜不到自己刚建的项目。
CODE_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")

MAX_MESSAGE_CHARS = 8000


class ProjectRejected(ValueError):
    """请求不合法或没资格。调用方转成 4xx。"""


def normalize_code(value: Any) -> str:
    raw = value if isinstance(value, str) else ""
    code = raw.strip().lower()
    if not code:
        raise ProjectRejected("项目标识不能为空")
    if not CODE_RE.match(code):
        raise ProjectRejected(
            f"项目标识只能用小写字母、数字和中划线，且首尾必须是字母或数字：{code!r}"
        )
    return code


# ==================== 服务端脱敏（P0-2）====================


@dataclass(frozen=True)
class Redacted:
    text: str
    hits: int

    @property
    def changed(self) -> bool:
        return self.hits > 0


def _keyword_pattern(keywords: List[str]) -> Optional[re.Pattern]:
    if not keywords:
        return None
    # normalized_keywords 已经按长度降序排好 —— 长词优先是必须的：
    # 「示例公司科技」和「示例公司」都在表里时，短词先匹配会把长词切成
    # 「[公司]科技」，等于漏掉了后半截。
    return re.compile("|".join(re.escape(k) for k in keywords), re.IGNORECASE)


def redact(text: str, *, keywords: Optional[List[str]] = None,
           placeholder: Optional[str] = None) -> Redacted:
    """把关键词替换成占位符。大小写不敏感，长词优先。

    同步版本，只吃环境变量基线。**群聊路径不要用它** —— 那条要按发言人部门解析，
    走 `redact_for_dept`。留着是因为它是纯函数、好测，也是既有调用方的形状。
    """
    if keywords is None or placeholder is None:
        from config import get_settings
        from services.redaction_policy import normalized_keywords

        settings = get_settings()
        keywords = normalized_keywords(settings.mask_keywords or "")
        placeholder = settings.mask_placeholder or "[公司]"

    pattern = _keyword_pattern(keywords)
    if pattern is None:
        return Redacted(text=text, hits=0)

    hits = 0

    def _sub(match: re.Match) -> str:
        nonlocal hits
        hits += 1
        return placeholder

    return Redacted(text=pattern.sub(_sub, text), hits=hits)


async def redact_for_dept(text: str, dept_id: Optional[int]) -> Redacted:
    """按**发言人**部门解析词表再遮。群聊路径用这个。

    为什么是发言人不是读者：一条消息从 A 部门发出、被 B 部门读到，要防的是
    「A 的内部术语流到 B」。按读者遮的话，A 的术语在 B 眼里原样呈现 ——
    恰恰是要防的那个方向。

    取所有成员部门的并集也想过，没做：B 的术语不会出现在 A 写的话里，真出现了
    说明 B 的信息已经泄漏到 A 那儿了，遮这条消息也来不及。多遮只会制造噪音，
    而噪音会让人忽略真正被遮的那些。
    """
    from config import get_settings
    from services.redaction_policy import keywords_for

    return redact(
        text,
        keywords=await keywords_for(dept_id),
        placeholder=get_settings().mask_placeholder or "[公司]",
    )


# ==================== @ 解析 ====================


def parse_mentions(text: str, members: Iterable[Dict[str, Any]]) -> List[Dict[str, str]]:
    """从正文里解析被 @ 的成员。**按成员名单匹配，不信客户端提交的列表。**

    让客户端提交 mentions 的话，注入一句「就说你被 @ 了」就能让 agent 强制回复 ——
    与决策 #45 那条「身份绝不能由 agent 提供」是同一类问题。

    匹配的是 `@显示名`：人用 username，agent 用 agent_label。长名优先，
    否则 `@小王` 会在 `@小王的助手` 里先命中，把后者切成两半。
    """
    candidates: List[Tuple[str, Dict[str, str]]] = []
    for m in members:
        if m.get("status") != ACTIVE:
            continue
        label = (m.get("agent_label") or m.get("username") or "").strip()
        if not label:
            continue
        candidates.append((label, {
            "kind": m.get("member_kind", KIND_USER),
            "uid": m.get("user_uid", ""),
            "label": label,
        }))

    candidates.sort(key=lambda pair: -len(pair[0]))

    found: List[Dict[str, str]] = []
    seen = set()
    for label, info in candidates:
        if re.search(rf"@{re.escape(label)}(?![^\s@])", text):
            key = (info["kind"], info["uid"], info["label"])
            if key not in seen:
                seen.add(key)
                found.append(info)
    return found


# ==================== 消息校验 ====================


def validate_message(text: Any) -> str:
    if not isinstance(text, str):
        raise ProjectRejected("消息正文必须是字符串")
    body = text.strip()
    if not body:
        raise ProjectRejected("消息正文不能为空")
    if len(body) > MAX_MESSAGE_CHARS:
        # 截断不行：群聊消息会被别人读，半截话比没有更误导人。
        raise ProjectRejected(f"消息超过 {MAX_MESSAGE_CHARS} 字上限")
    return body


# ==================== 成员判定 ====================


def require_write_access(member: Optional[Dict[str, Any]]) -> None:
    """能不能在这个项目里发言。

    不是成员和只读成员都拒，但**错误文案不区分** —— 区分开等于告诉外人
    「有这么个项目，只是你进不去」。与 skill hub 下架后统一回 404 同一条理由。
    """
    if member is None or member.get("status") != ACTIVE:
        raise ProjectRejected("项目不存在，或你不是它的成员")
    if member.get("role") not in WRITE_ROLES:
        raise ProjectRejected("你在这个项目里是只读成员，不能发言")


def require_owner(member: Optional[Dict[str, Any]], *, is_admin: bool = False) -> None:
    """改成员、归档项目这类动作。管理员可越过。"""
    if is_admin:
        return
    if member is None or member.get("status") != ACTIVE or member.get("role") != ROLE_OWNER:
        raise ProjectRejected("只有项目负责人能做这个操作")


# ==================== 任务与认领（P0-6）====================
#
# 这一段存在的首要理由不是「管理任务」，是**挡住一条绕道**：
#
#   群里发一个夹带注入的任务描述 → agent 认领 → 回到私聊上下文执行
#   → 此时它持有主人的全部 MCP 凭据
#
# 执行不发生在群聊，所以阶段 12 那道「群聊禁用能力」的闸门完全落空。
#
# 落地对策是两条，缺一条都不成立：
#
# 1. **不可信标记跟着任务描述一路进入私聊上下文**，不因为「这是主人自己的会话」
#    就恢复完全信任
# 2. **认领 ≠ 自动开工** —— 认领后需主人显式确认才进入执行，确认时把任务原文
#    **原样**呈给他看。人在环这一下比任何自动检测都可靠，成本也极低

TASK_OPEN = "open"          # 未认领
TASK_CLAIMED = "claimed"    # 已认领，**还没开工**
TASK_ACTIVE = "active"      # 主人确认过，开工中
TASK_DONE = "done"
TASK_CANCELLED = "cancelled"

SOURCE_CHAT = "chat"
SOURCE_MANUAL = "manual"

MAX_TASK_TITLE = 255
MAX_TASK_BODY = 4000


def default_untrusted(source_kind: str) -> bool:
    """这段文字该不该带不可信标记。

    **默认为真**，只有一条路能让它为假：项目成员在项目页里自己手写的任务 ——
    那种情况下他本来就在读自己写的东西。

    默认值写在「不可信」这一侧，是因为反过来（默认可信、遇到群聊来源才标脏）
    漏一处就等于这条防线不存在，而漏了不会有任何报错。
    """
    return source_kind != SOURCE_MANUAL


def validate_task(title: Any, body: Any) -> Tuple[str, str]:
    if not isinstance(title, str) or not title.strip():
        raise ProjectRejected("任务标题不能为空")
    text = "" if body is None else body
    if not isinstance(text, str):
        raise ProjectRejected("任务描述必须是字符串")
    if len(text) > MAX_TASK_BODY:
        # 与群聊消息同理：截断之后呈给认领人看的是半截话，
        # 而「原样呈给他看」正是这条防线的全部内容。
        raise ProjectRejected(f"任务描述超过 {MAX_TASK_BODY} 字上限")
    return title.strip()[:MAX_TASK_TITLE], text.strip()


def can_claim(task: Dict[str, Any]) -> None:
    if task.get("status") != TASK_OPEN:
        raise ProjectRejected("这个任务已经被认领或已结束")


def can_confirm(task: Dict[str, Any], uid: str) -> None:
    """确认开工。**只有认领人本人能确认。**

    别人替你确认，等于替你决定「用你的凭据去执行这段来路不明的文字」——
    而整条 P0-6 防线就架在「认领人自己看过原文」这一下上。
    """
    if task.get("status") != TASK_CLAIMED:
        raise ProjectRejected("这个任务不在待确认状态")
    if task.get("claimed_by_uid") != uid:
        raise ProjectRejected("只有认领人本人能确认开工")


def confirmation_notice(task: Dict[str, Any]) -> Dict[str, Any]:
    """确认开工前要给认领人看的东西。

    **原文原样呈现，不做任何摘要或高亮。** 摘要会把注入的那句话摘掉 ——
    而它恰恰是要让人看见的那一句。高亮则会训练人只看高亮部分。

    `untrusted` 为真时附一句提醒。措辞刻意不说「这可能是攻击」：绝大多数任务
    都是正常的，狼来了喊多了就没人看了。说的是「这段文字来自群聊，不是你写的」
    —— 陈述事实，让人自己判断。
    """
    notice: Dict[str, Any] = {
        "title": task.get("title", ""),
        # 原样。呈给人看的就是库里那份。
        "body": task.get("body", ""),
        "source_kind": task.get("source_kind", SOURCE_CHAT),
        "untrusted": bool(task.get("untrusted", True)),
    }
    if notice["untrusted"]:
        notice["reminder"] = (
            "这段任务描述来自群聊，不是你自己写的。开工之后你的 agent 会在你的"
            "私聊上下文里读它，那里它持有你的全部凭据。请先通读上面的原文再确认。"
        )
    return notice
