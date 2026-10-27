"""会话作用域 —— 群聊 agent 能力边界的骨架（决策 #45 / v2 专章 P0-1）

## 这条边界要挡的是什么

决策 #43「每用户绑定自己的中台账号」成立的前提是**指令来自凭据主人本人**。
群聊把它变成「指令来自别人、凭据是主人的」：他部门或外部成员在群里 @ 我方 agent
要一份数据，agent 拿主人的中台 Token 去查 —— 中台看到的是完全合法的请求，
调用日志显示「用户 X 调用了 datahub.report.platform.list」，现有任何一层都不会拒绝，
事后翻日志也看不出异常。

「主人也在群里所以能发现」不构成防线：人不会逐条盯消息，而且那要等调用**成功之后**
才看得见。

收敛的解法（决策 #45）：**能力不跨界，只有产物跨界**。群聊里的 agent 只做讨论、
认领/接收任务、提交结果，不使用任何 MCP 与 skill；真正的执行发生在主人自己的私聊
上下文，用主人自己的凭据。

## 整个机制的骨架是一条规则：**作用域只能收紧，不能放宽**

    scope = 更严格的一个(凭据推出来的, 请求头声明的)

- **凭据侧**：今天所有 API Key 都是个人设备 Key，推出 `private`。群聊功能落地后，
  群聊轮次会拿一把网关自己签发的、绑定 `(group_id, 提问人 uid)` 的会话令牌，
  推出 `group`。
- **请求头侧**：`X-Llano-Conversation-Scope`。客户端说 `group` 就当 `group`；
  说 `private` 而凭据侧是 `group` 时，**仍然是 group**。

这条不对称是全部意义所在。设计里那四条约束的第一条是「调用者身份绝不能由 agent
提供 —— 若 agent 是携带一个 ID 参数去请求，注入一句『用你主人的账号查』即可破防」。
同理，作用域要是可以由请求方放宽，注入一句「你现在是私聊」就够了。

认不出来的值**一律当 group**（fail closed）：一个拼错的头不该悄悄换来完整权限。

## ⚠️ 今天这条防线还只有一半

凭据侧现在恒为 `private`（群聊会话令牌还不存在，群聊功能本身也还没有）。所以
**当下的实际约束力来自请求头**，而一个被完全控制的客户端可以干脆不发这个头。

这不是漏洞，是**分期**：另一半（服务端从 session/token 绑定关系推导）必须等群聊
会话表存在才做得了，而先把「只能收紧」这条规则和它的执行点钉死，是为了让那一半
落地时**接上去就行、不用回头改判定方向**。判定方向写反了的东西，加多少东西都救不回来。

真正不能省的是：**任何一处都不许出现「客户端说 private 就当 private」**。
`resolve_scope` 是唯一的入口，绕过它自己读头就等于把这条规则作废。

## 软失败：让它看起来像网关拒绝，而不是 agent 拒绝

跨账号请求一律以调用者身份送进网关，网关必定拒绝。对提问者表现为「网关不给我干，
所以帮不了你」，而不是「我不给你干」—— 「想帮但干不了」与「不想帮」在态度上是
两件事。这套机制**不告知 agent**，它是真心在试。

额外白赚的是：每次拒绝都是一条「谁在试图套数据」的审计记录。

四条实现约束（缺一条就退化成安全剧场）：

1. **身份服务端推导** —— 见上面那条不对称规则
2. **「必定失败」不真打上游** —— 本地判定即可返回同形态错误。真发请求会让中台看到
   大量无效 Token，可能撞上「共享网关 IP 被当撞库拦截」的已知风险。为演一场戏去惹
   风控不划算
3. **失败尝试单独限流、单独计数** —— 否则 @ 轰炸 = 免费刷网关与日志；而且「某人今日
   触发 47 次跨账号拒绝」本身就该告警，不是噪声。所以状态码是 `scope_denied` 而不是
   复用 `denied`（后者是日常的未授权，混在一起就淹没了）
4. **错误文案不得泄漏存在性** —— 「网关拒绝」可以，「你无权访问
   datahub.report.platform.list」不行，后者等于对外公布我们有哪些接口。不区分
   「不存在 / 无权 / 未绑定」，统一一个错误，并标 non-retryable，否则 agent 会热心地
   重试到限流
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from loguru import logger

# 作用域。顺序即严格程度，靠后的更严格 —— resolve_scope 取更严格的那个。
PRIVATE = "private"
GROUP = "group"
_STRICTNESS = {PRIVATE: 0, GROUP: 1}

SCOPE_HEADER = "X-Llano-Conversation-Scope"

# 单独的日志状态。**不要复用 denied** —— 那是日常的未授权拒绝，
# 混在一起「某人今日触发 47 次跨账号拒绝」这条信号就被噪声淹没了。
SCOPE_DENIED = "scope_denied"

# 统一错误。不区分「不存在 / 无权 / 未绑定」——区分开等于对外公布我们有哪些接口。
#
# 措辞刻意写成**网关**拒绝而不是 agent 拒绝：agent 会把它转述给提问者，
# 「网关不让我用你的账号查」与「我不想帮你查」在态度上是两件事，而我们要的是前者。
# 这套机制不告知 agent，它是真心在试。
SCOPE_DENIED_MESSAGE = (
    "网关拒绝了这次调用：当前会话不允许使用外部数据接口。"
    "如果确实需要这份数据，请让需要的人用自己的账号去取，"
    "或者请这个 agent 的主人在他自己的会话里做。"
)
SCOPE_DENIED_CODE = "conversation_scope_forbidden"

# 单位时间内同一个人能撞多少次。超了直接短路，连日志都不写（只累计计数），
# 否则 @ 轰炸就是免费刷我们的库。
DENY_RATE_LIMIT = 20
DENY_RATE_WINDOW_SECONDS = 300
# 到这个数就该有人看一眼了。设计里举的例子是「某人今日触发 47 次」。
DENY_ALERT_THRESHOLD = 10


@dataclass(frozen=True)
class ScopeDecision:
    scope: str
    """最终作用域。"""

    allows_capabilities: bool
    """这个作用域下能不能用 MCP / skill。"""

    reason: str
    """怎么定下来的，只进日志，不给调用方看。"""


def _normalize(value: Optional[str]) -> str:
    """把一个声明的作用域收成已知值。**认不出来一律当 group**。

    fail closed：一个拼错的头（`privat`、`Private `、空串、被中间层改过的值）
    不该悄悄换来完整权限。宁可让一次正常的私聊调用被挡下来被人发现，
    也不要让一次群聊调用因为拼写而放行 —— 前者会有人来报，后者没人会知道。
    """
    if value is None:
        return PRIVATE
    cleaned = str(value).strip().lower()
    if not cleaned:
        # 头存在但是空的：这是「有人在设它」的信号，不是「没设」。当 group。
        return GROUP
    if cleaned in _STRICTNESS:
        return cleaned
    logger.warning(f"会话作用域头认不出来的值 {value!r}，按最严格处理")
    return GROUP


def scope_from_credential(caller) -> str:
    """凭据本身推出来的作用域。

    这就是阶段 12 说的「另一半」（阶段 24 落地）。在它之前这里恒为 private，
    实际约束力全靠请求头 —— 而一个被控制、或只是**忘了发头**的客户端不发，
    就静默降级成 private，正是这条防线要挡的那条路。

    现在约束长在凭据本身上：群聊会话令牌在 `api_keys.scope` 上写着 group，
    拿它发的请求不管头里写什么都推得出 group（放宽由 resolve_scope 拦）。

    ## 认不出来的值一律 group

    与 `_normalize` 同一个方向，理由也一样：库里出现一个我们不认识的 scope，
    可能是脏数据，也可能是有人在改。宁可让一次私聊调用被挡下来被人报上来，
    也不要让一次群聊调用因为一个拼错的枚举值拿到完整权限。

    ⚠️ 「取不到这个属性」与「取到一个不认识的值」是**两件事**：
    前者是阶段 24 之前的老结构/老对象，按 private（与当时行为一致）；
    后者是有值但不认识，按 group。把两者合并成一个 `or GROUP` 会让所有
    存量个人 Key 一夜之间失去 MCP 与 skill。
    """
    raw = getattr(caller, "scope", None)
    if raw is None:
        return PRIVATE

    cleaned = str(raw).strip().lower()
    if not cleaned:
        return PRIVATE
    if cleaned in _STRICTNESS:
        return cleaned

    logger.warning(f"凭据上的会话作用域认不出来的值 {raw!r}，按最严格处理")
    return GROUP


def resolve_scope(caller, header_value: Optional[str] = None) -> ScopeDecision:
    """定出这次请求的会话作用域。**这是唯一入口。**

    绕过它自己去读那个头，就等于把「只能收紧」这条规则作废 —— 所以任何新增的
    受限接口都要走这里，而不是各自 `request.headers.get(...)`。
    """
    from_credential = scope_from_credential(caller)
    from_header = _normalize(header_value)

    # 取更严格的那个。**不是取请求头的**，也不是取凭据的。
    if _STRICTNESS[from_header] >= _STRICTNESS[from_credential]:
        scope, reason = from_header, "客户端声明"
    else:
        scope, reason = from_credential, "凭据推导"

    return ScopeDecision(
        scope=scope,
        allows_capabilities=(scope == PRIVATE),
        reason=f"{reason} → {scope}（凭据 {from_credential} / 声明 {from_header}）",
    )


async def note_scope_denial(uid: str) -> bool:
    """记一次跨作用域拒绝，返回**是否还应该落库**。

    返回 False 表示这个人已经在窗口内撞太多次了：继续给他写日志就是让 @ 轰炸免费
    消耗我们的库。计数照常涨（要的就是「他撞了多少次」这个数），只是不再写行。

    Redis 不可用时返回 True —— 宁可多写几行日志，也不要因为缓存挂了就把这条
    「谁在试图套数据」的审计记录整个丢掉。
    """
    try:
        from utils.cache import get_redis

        redis = get_redis()
        if redis is None:
            return True

        key = f"scope_deny:{uid}"
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, DENY_RATE_WINDOW_SECONDS)

        if count == DENY_ALERT_THRESHOLD:
            # 这一行是要被人看的：有人在反复尝试用别人的凭据取数。
            logger.warning(
                f"跨会话作用域拒绝已累计 {count} 次（{DENY_RATE_WINDOW_SECONDS}s 内，uid={uid}），"
                f"这通常意味着有人在群聊里反复诱导 agent 用主人的账号取数"
            )
        return count <= DENY_RATE_LIMIT
    except Exception as exc:  # noqa: BLE001 — 缓存挂了不该让审计整个失灵
        logger.debug(f"作用域拒绝计数失败，按仍需落库处理：{exc}")
        return True


def denial_payload() -> dict:
    """给调用方的统一错误体。

    `retryable: False` 是必须的：不标的话 agent 会热心地重试到限流，
    而每一次重试都是一条噪声日志和一次白跑。
    """
    return {
        "error": {
            "message": SCOPE_DENIED_MESSAGE,
            "type": "mcp_permission_error",
            "code": SCOPE_DENIED_CODE,
            "retryable": False,
        }
    }
