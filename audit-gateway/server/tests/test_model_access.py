"""模型白名单判定逻辑测试

这套规则决定"谁能用哪个模型"，配错就是越权，因此覆盖到每条判定分支。
"""

from model.database import (ROLE_ADMIN, ROLE_EMPLOYEE, SCOPE_DEPT,
                            SCOPE_GLOBAL, SCOPE_ROLE, SCOPE_USER)
from services.model_access import (AccessSubject, WhitelistRule,
                                   filter_unambiguous_public_models,
                                   is_model_allowed,
                                   resolve_allowed_model_ids)

EMPLOYEE = AccessSubject(uid="u-emp", role=ROLE_EMPLOYEE, dept_id=2)
BOSS = AccessSubject(uid="u-boss", role=ROLE_ADMIN, dept_id=1)
NO_DEPT = AccessSubject(uid="u-new", role=ROLE_EMPLOYEE, dept_id=None)


def test_no_rules_means_no_access():
    """默认拒绝：没有任何规则时不应放行"""
    assert resolve_allowed_model_ids([], EMPLOYEE) == set()


def test_global_scope_applies_to_everyone():
    rules = [WhitelistRule(SCOPE_GLOBAL, "", 1)]
    assert resolve_allowed_model_ids(rules, EMPLOYEE) == {1}
    assert resolve_allowed_model_ids(rules, BOSS) == {1}
    assert resolve_allowed_model_ids(rules, NO_DEPT) == {1}


def test_dept_scope_only_matches_own_dept():
    rules = [WhitelistRule(SCOPE_DEPT, "2", 10), WhitelistRule(SCOPE_DEPT, "9", 11)]
    assert resolve_allowed_model_ids(rules, EMPLOYEE) == {10}


def test_dept_scope_skipped_when_user_has_no_dept():
    """无部门用户不应因 dept 规则拿到授权"""
    rules = [WhitelistRule(SCOPE_DEPT, "2", 10)]
    assert resolve_allowed_model_ids(rules, NO_DEPT) == set()


def test_role_scope():
    rules = [
        WhitelistRule(SCOPE_ROLE, ROLE_ADMIN, 20),
        WhitelistRule(SCOPE_ROLE, ROLE_EMPLOYEE, 21),
    ]
    assert resolve_allowed_model_ids(rules, BOSS) == {20}
    assert resolve_allowed_model_ids(rules, EMPLOYEE) == {21}


def test_user_scope():
    rules = [WhitelistRule(SCOPE_USER, "u-emp", 30), WhitelistRule(SCOPE_USER, "u-x", 31)]
    assert resolve_allowed_model_ids(rules, EMPLOYEE) == {30}


def test_scopes_are_unioned():
    rules = [
        WhitelistRule(SCOPE_GLOBAL, "", 1),
        WhitelistRule(SCOPE_DEPT, "2", 2),
        WhitelistRule(SCOPE_ROLE, ROLE_EMPLOYEE, 3),
        WhitelistRule(SCOPE_USER, "u-emp", 4),
    ]
    assert resolve_allowed_model_ids(rules, EMPLOYEE) == {1, 2, 3, 4}


def test_explicit_deny_beats_any_allow():
    """显式拒绝优先级最高 —— 部门被停用某模型时，全局授权不能把它救回来"""
    rules = [
        WhitelistRule(SCOPE_GLOBAL, "", 1),
        WhitelistRule(SCOPE_ROLE, ROLE_EMPLOYEE, 1),
        WhitelistRule(SCOPE_DEPT, "2", 1, allowed=False),
    ]
    assert resolve_allowed_model_ids(rules, EMPLOYEE) == set()
    assert is_model_allowed(rules, EMPLOYEE, 1) is False
    # 拒绝规则是部门级的，不影响其他部门的人
    assert is_model_allowed(rules, BOSS, 1) is True


def test_deny_does_not_leak_to_other_models():
    rules = [
        WhitelistRule(SCOPE_GLOBAL, "", 1),
        WhitelistRule(SCOPE_GLOBAL, "", 2),
        WhitelistRule(SCOPE_USER, "u-emp", 2, allowed=False),
    ]
    assert resolve_allowed_model_ids(rules, EMPLOYEE) == {1}


def test_unknown_scope_never_grants():
    """脏数据/未来新增作用域不应意外放行"""
    rules = [WhitelistRule("whatever", "", 1)]
    assert resolve_allowed_model_ids(rules, EMPLOYEE) == set()


def test_admin_role_has_no_implicit_model_access():
    """admin 在接口层是超级角色，但模型授权仍需显式配置 —— 避免审计口径含糊"""
    rules = [WhitelistRule(SCOPE_DEPT, "2", 10)]
    assert resolve_allowed_model_ids(rules, BOSS) == set()


class _Model:
    def __init__(self, model_id, model_name, alias=None):
        self.id = model_id
        self.model_name = model_name
        self.alias = alias


def test_同名渠道模型未配别名时全部拒绝下发():
    models = [_Model(1, "deepseek-chat"), _Model(2, "deepseek-chat")]
    assert filter_unambiguous_public_models(models) == []


def test_不同公开别名可以区分同一上游型号():
    models = [
        _Model(1, "deepseek-chat", "deepseek-direct"),
        _Model(2, "deepseek-chat", "partner-deepseek"),
    ]
    assert filter_unambiguous_public_models(models) == models


# ---------------------------------------------------------------------------
# /v1/models 的 context_length（2026-09-16）
# ---------------------------------------------------------------------------


class _CtxModel:
    """只带这个断言用得到的字段。"""

    def __init__(self, name, alias=None, max_context_tokens=None, supports_multimodal=False):
        self.model_name = name
        self.alias = alias
        self.max_context_tokens = max_context_tokens
        self.supports_multimodal = supports_multimodal


def test_v1_models_带上下文窗口():
    """OpenAI 格式清单必须带 context_length。

    不带的话客户端会退回硬编码表做子串匹配：`DeepSeek-V4.1-Flash` 里的 `.1`
    让它匹配不上 `deepseek-v4-flash`（1M），落进兜底的 `deepseek`（128K）。
    1M 的模型被当成 128K，压缩触发线随之压到 96K，长会话里受保护的尾段本身就
    超过它 → 压缩连续无效 → 防抖熔断关掉自动压缩。2026-09-16 线上真发生过。
    """
    from router.v1.chat import _openai_model_item

    item = _openai_model_item(_CtxModel("DeepSeek-V4.1-Flash", max_context_tokens=1_000_000))
    assert item["context_length"] == 1_000_000


def test_字段名必须是客户端认的那个():
    """客户端的 `_CONTEXT_LENGTH_KEYS` 认 `context_length`，**不认**库里那个列名
    `max_context_tokens`。改成列名等于把这个修复静默撤销，所以钉死。"""
    from router.v1.chat import _openai_model_item

    item = _openai_model_item(_CtxModel("m", max_context_tokens=256_000))
    assert "context_length" in item
    assert "max_context_tokens" not in item


def test_没配窗口的模型不带这个键():
    """发 null 比不发更糟：客户端会把它当成一个值，而不是走自己的探测链。"""
    from router.v1.chat import _openai_model_item

    assert "context_length" not in _openai_model_item(_CtxModel("m"))
    assert "context_length" not in _openai_model_item(_CtxModel("m", max_context_tokens=0))


def test_公开名仍然优先用别名():
    """加字段不能动 id 的口径 —— 别名存在就用别名，避免上游换型影响客户端。"""
    from router.v1.chat import _openai_model_item

    item = _openai_model_item(_CtxModel("internal-name", alias="public-name"))
    assert item["id"] == "public-name"
    assert item["object"] == "model"


def test_v1_models_声明能不能看图():
    """客户端靠 supports_vision 决定图片是直接给模型看，还是先转成文字描述。

    不发的话公司网关的模型在客户端一律算「不能看图」（models.dev 查不到），
    DeepSeek V4.1 Flash 后台勾了多模态也只能看到辅助模型写的描述。
    关掉的模型也要明确发 False，客户端才不会再去猜。
    """
    from router.v1.chat import _openai_model_item

    assert _openai_model_item(_CtxModel("deepseek-flash", supports_multimodal=True))["supports_vision"] is True
    assert _openai_model_item(_CtxModel("deepseek-v4-pro", supports_multimodal=False))["supports_vision"] is False


# ---------------------------------------------------------------------------
# 部门继承（2026-10-19）：源贺在设计创意部(30)，上级视觉中心(9) 开放了 GPT，
# 但 dept 规则只比本部门 ID，他用不了。
# ---------------------------------------------------------------------------

from services.model_access import (explain_model_access, inherited_dept_id,
                                   with_dept_chain)
from services.org_tree import expand_ancestors

# 视觉中心(9) → 设计创意部(30) → 创意一组(31)；营销中心(5) 是平级的另一棵树
_TREE = {9: None, 30: 9, 31: 30, 5: None, 51: 5}
DESIGNER = AccessSubject(uid="u-yh", role=ROLE_EMPLOYEE, dept_id=30,
                         dept_chain=expand_ancestors(30, _TREE))
GROUP_MEMBER = AccessSubject(uid="u-g", role=ROLE_EMPLOYEE, dept_id=31,
                             dept_chain=expand_ancestors(31, _TREE))
VISUAL_HEAD = AccessSubject(uid="u-head", role=ROLE_EMPLOYEE, dept_id=9,
                            dept_chain=expand_ancestors(9, _TREE))
MARKETING = AccessSubject(uid="u-mk", role=ROLE_EMPLOYEE, dept_id=51,
                          dept_chain=expand_ancestors(51, _TREE))


def test_上级部门开放_下级成员可用():
    rules = [WhitelistRule(SCOPE_DEPT, "9", 16)]
    assert resolve_allowed_model_ids(rules, DESIGNER) == {16}


def test_多级部门都能继承到顶级的授权():
    rules = [WhitelistRule(SCOPE_DEPT, "9", 16), WhitelistRule(SCOPE_DEPT, "30", 17)]
    assert resolve_allowed_model_ids(rules, GROUP_MEMBER) == {16, 17}
    assert resolve_allowed_model_ids(rules, DESIGNER) == {16, 17}


def test_继承只向上_下级的授权不会给上级():
    rules = [WhitelistRule(SCOPE_DEPT, "30", 17)]
    assert resolve_allowed_model_ids(rules, VISUAL_HEAD) == set()


def test_继承不横向_平级部门拿不到():
    rules = [WhitelistRule(SCOPE_DEPT, "9", 16)]
    assert resolve_allowed_model_ids(rules, MARKETING) == set()


def test_部门继承与角色和个人授权取并集():
    rules = [
        WhitelistRule(SCOPE_GLOBAL, "", 1),
        WhitelistRule(SCOPE_DEPT, "9", 2),
        WhitelistRule(SCOPE_ROLE, ROLE_EMPLOYEE, 3),
        WhitelistRule(SCOPE_USER, "u-g", 4),
    ]
    assert resolve_allowed_model_ids(rules, GROUP_MEMBER) == {1, 2, 3, 4}


def test_上级部门的显式拒绝向下生效_个人授权救不回来():
    rules = [
        WhitelistRule(SCOPE_DEPT, "9", 16, allowed=False),
        WhitelistRule(SCOPE_DEPT, "30", 16),
        WhitelistRule(SCOPE_USER, "u-g", 16),
    ]
    assert resolve_allowed_model_ids(rules, GROUP_MEMBER) == set()


def test_下级部门的拒绝不影响上级本级():
    rules = [
        WhitelistRule(SCOPE_DEPT, "9", 16),
        WhitelistRule(SCOPE_DEPT, "30", 16, allowed=False),
    ]
    assert resolve_allowed_model_ids(rules, VISUAL_HEAD) == {16}
    assert resolve_allowed_model_ids(rules, DESIGNER) == set()


def test_没带继承链时退回只匹配本部门():
    """调用方忘了补链只会少给，不会多给"""
    bare = AccessSubject(uid="u-yh", role=ROLE_EMPLOYEE, dept_id=30)
    rules = [WhitelistRule(SCOPE_DEPT, "9", 16), WhitelistRule(SCOPE_DEPT, "30", 17)]
    assert resolve_allowed_model_ids(rules, bare) == {17}


def test_反查结论与解析结论一致并标出继承来源():
    rules = [
        WhitelistRule(SCOPE_GLOBAL, "", 1),
        WhitelistRule(SCOPE_DEPT, "9", 16),
        WhitelistRule(SCOPE_DEPT, "30", 17),
        WhitelistRule(SCOPE_USER, "u-yh", 16),
        WhitelistRule(SCOPE_ROLE, ROLE_EMPLOYEE, 18, allowed=False),
        WhitelistRule(SCOPE_DEPT, "9", 18),
        WhitelistRule(SCOPE_DEPT, "5", 19),
    ]
    explained = explain_model_access(rules, DESIGNER)
    allowed = {mid for mid, e in explained.items() if e.allowed}
    assert allowed == resolve_allowed_model_ids(rules, DESIGNER)
    assert 19 not in explained
    assert explained[18].allowed is False and len(explained[18].deny_sources) == 1
    sources16 = explained[16].allow_sources
    assert {inherited_dept_id(r, DESIGNER) for r in sources16} == {9, None}
    assert inherited_dept_id(explained[17].allow_sources[0], DESIGNER) is None


async def test_with_dept_chain_从部门树补齐(monkeypatch):
    async def fake_ancestors(dept_id):
        return expand_ancestors(dept_id, _TREE)

    monkeypatch.setattr("services.org_tree.dept_ancestor_ids", fake_ancestors)
    bare = AccessSubject(uid="u-g", role=ROLE_EMPLOYEE, dept_id=31)
    assert (await with_dept_chain(bare)).dept_ids() == (9, 30, 31)
    assert (await with_dept_chain(NO_DEPT)).dept_ids() == ()


async def test_部门树成环时只按本部门匹配(monkeypatch):
    async def fake_ancestors(dept_id):
        return expand_ancestors(dept_id, {1: 2, 2: 1})

    monkeypatch.setattr("services.org_tree.dept_ancestor_ids", fake_ancestors)
    subject = await with_dept_chain(AccessSubject(uid="x", role=ROLE_EMPLOYEE, dept_id=1))
    rules = [WhitelistRule(SCOPE_DEPT, "2", 5), WhitelistRule(SCOPE_DEPT, "1", 6)]
    assert resolve_allowed_model_ids(rules, subject) == {6}
