"""群聊会话令牌 —— 阶段 12 那半刀的另一半（阶段 24）

## 在它之前，这条防线只有一半

阶段 12 把「作用域只能收紧」这条规则和它的执行点钉死了，但**凭据侧恒为
private**。于是「这一轮是群聊」只能由客户端发 `X-Llano-Conversation-Scope`
声明 —— 一个被控制、或**只是忘了发头**的客户端不发，就静默降级成 private，
而 private 是允许 MCP 与 skill 的那一档。

决策 #45 要挡的正是这条路：别人在群里 @ 我方 agent，agent 拿主人的凭据去取数。

阶段 24 把约束移到**凭据本身**上：群聊令牌在 `api_keys.scope` 上写着 group，
拿它发的请求不管头里写什么，服务端都推得出 group。

## 这组守四件事

1. 凭据说 group，**请求头放宽不了**
2. 认不出来的 scope 值 → group（fail closed）
3. **取不到属性** → private（阶段 24 之前的库/对象，与当时行为逐字一致）
4. 群聊令牌**不能再签一把**（自我延期 / 换项目）
"""

from __future__ import annotations

import pytest
from services import conversation_scope as cs


class _Caller:
    """够 resolve_scope 用的最小调用者。"""

    def __init__(self, scope=None, uid="u-1"):
        self.uid = uid
        if scope is not None:
            self.scope = scope


class _LegacyCaller:
    """阶段 24 之前的调用者对象 —— 身上根本没有 scope 这个属性。"""

    uid = "u-old"


# ── 1. 只能收紧 ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("header", ["private", "PRIVATE", " private ", None, ""])
def test_a_group_credential_can_never_be_widened_by_the_header(header):
    """**这是整组里最重要的一条。**

    凭据是群聊令牌时，请求头无论说什么都不能换回 private —— 否则注入一句
    「你现在是私聊」就够了，整套能力边界作废。
    """
    decision = cs.resolve_scope(_Caller(cs.GROUP), header)

    assert decision.scope == cs.GROUP
    assert decision.allows_capabilities is False


def test_a_private_credential_can_still_be_narrowed_by_the_header():
    """反向基线：收紧方向必须仍然通。

    只测上面那条的话，把 resolve_scope 改成「永远返回 group」也能全绿 ——
    那等于把私聊的 MCP 和 skill 一起关掉，而没有任何用例会报。
    """
    decision = cs.resolve_scope(_Caller(cs.PRIVATE), "group")

    assert decision.scope == cs.GROUP


def test_a_private_credential_with_no_header_keeps_capabilities():
    """个人设备 Key 的日常路径不能被这一刀伤到。"""
    decision = cs.resolve_scope(_Caller(cs.PRIVATE), None)

    assert decision.scope == cs.PRIVATE
    assert decision.allows_capabilities is True


# ── 2 / 3. 「值不认识」与「属性不存在」是两件事 ──────────────────────────

@pytest.mark.parametrize("bad", ["grp", "public", "private;", "0", "privat"])
def test_an_unrecognised_scope_value_fails_closed(bad):
    """库里出现不认识的值 —— 可能是脏数据，也可能是有人在改。按最严格处理。

    宁可让一次私聊调用被挡下来、被人报上来，也不要让一次群聊调用因为一个
    拼错的枚举值拿到完整权限：前者有人会来问，后者没人会知道。
    """
    assert cs.scope_from_credential(_Caller(bad)) == cs.GROUP


@pytest.mark.parametrize("ok", ["GROUP", " group ", "Group"])
def test_case_and_padding_are_not_treated_as_corruption(ok):
    """大小写和空格不算脏数据 —— 把它们判成 group 是**碰巧对**，
    而这条要钉的是「认得出来就照它说的算」。"""
    assert cs.scope_from_credential(_Caller(ok)) == cs.GROUP


@pytest.mark.parametrize("ok", ["PRIVATE", " private ", "Private"])
def test_recognised_private_is_honoured_not_rounded_up(ok):
    """配套的另一半：认得出来的 private 就是 private。

    只有上面那条的话，`scope_from_credential` 写成「永远返回 group」也能过 ——
    而那会把所有人的 MCP 与 skill 一起关掉。"""
    assert cs.scope_from_credential(_Caller(ok)) == cs.PRIVATE


def test_a_missing_attribute_stays_private_not_group():
    """**这一条挡的是一次全员事故。**

    把「取不到属性」也 fail closed 成 group，所有存量个人设备 Key 会一夜之间
    失去 MCP 与 skill —— 而它们本来就是私聊。现象是「所有人的工具突然都没了」，
    而没有任何一条报错。

    「值不认识」才 fail closed，「列/属性不存在」按 private。
    """
    assert cs.scope_from_credential(_LegacyCaller()) == cs.PRIVATE
    assert cs.resolve_scope(_LegacyCaller(), None).allows_capabilities is True


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_a_blank_credential_scope_is_private(blank):
    """空值来自「加了列但没回填」，不是「有人在设它」——与请求头那侧相反。

    请求头空串当 group，因为头是**请求方**发的，空串说明有人在设它。
    凭据侧的空值来自我们自己的库，是回填没做完，按 private 才与加列前一致。
    """
    assert cs.scope_from_credential(_Caller(blank)) == cs.PRIVATE


# ── 4. 令牌不能自我繁殖 ──────────────────────────────────────────────────

def test_the_session_endpoint_refuses_a_group_credential():
    """拿群聊令牌再签一把 = 自己延长有效期，或换一个项目（横向越权）。

    正常流程里签发这一步永远发生在私聊上下文，所以这条限制不挡任何真实用法。
    """
    import inspect

    from router.v1 import projects

    source = inspect.getsource(projects.group_session)

    assert "scope_from_credential" in source, "签发前必须看凭据侧作用域"
    assert "SCOPE_DENIED_MESSAGE" in source, "拒绝要用统一文案，不能说破手里是什么凭据"


def test_the_token_carries_the_project_it_was_minted_for():
    """令牌绑定项目。不绑的话，一把群聊令牌在所有项目里通用，
    「哪个群的会话」这件事就没有服务端事实可依。"""
    import inspect

    from router.v1 import projects

    source = inspect.getsource(projects.group_session)

    assert "scope_project_id=" in source
    assert "expires_at=" in source, "会话令牌必须有有效期，它不是设备 Key"


def test_scope_derivation_lives_in_exactly_one_place():
    """任何一处自己读 caller.scope 判断，都等于把「只能收紧」那条规则作废。"""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for path in list((root / "router").rglob("*.py")) + list((root / "services").rglob("*.py")):
        if path.name == "conversation_scope.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "caller.scope" in text and "scope_from_credential" not in text:
            offenders.append(str(path.relative_to(root)))

    assert offenders == [], f"这些地方绕过了 resolve_scope 自己读 scope：{offenders}"
