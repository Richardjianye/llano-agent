"""技能来源闸门 —— **当前是熄火状态的应急开关，不是生效中的策略**。

2026-08-26 负责人定：所有技能来源不封禁。产品方向是让员工随便下载
（自有 skill hub 是为了给各部门下技能指标、把工作经验转成公司资产），
安全靠**安装时与定期扫描 + 命中即禁用并上传样本**来兜，而不是靠堵来源。
所以 `third_party_skill_sources` 已经从 product-environments.json 的
disabledCapabilities 里移除了 —— 品牌构建现在拿到全部 9 个上游来源、
公共 SkillHub 与阶段 11 落地的公司自有 hub，共 11 个。

机制本身留着没删：它是一条测过的、改一行配置就能拉闸的开关。真出事的时候
（比如 ClawHub 再来一次 ClawHavoc）不用现写代码。**这些用例都显式传
third_party_allowed，测的是机制，不是当前策略。**「当前策略是全开」由
apps/desktop/scripts/product-environment.contract.mjs 的
EXPECTED_DISABLED_CAPABILITIES 钉住。
"""
import pytest

from hermes_cli import product_policy
from tools import skills_hub


class _FakeSource:
    def __init__(self, source_id: str) -> None:
        self._id = source_id

    def source_id(self) -> str:
        return self._id


UPSTREAM_SOURCE_IDS = [
    "official",
    "hermes-index",
    "skills-sh",
    "well-known",
    "url",
    "github",
    "clawhub",
    "lobehub",
    "browse-sh",
]


def _sources(ids):
    return [_FakeSource(i) for i in ids]


def _ids(sources):
    return [s.source_id() for s in sources]


def test_upstream_builds_keep_every_source():
    """基线：不设策略时一个都不少。

    先立这条，否则下面的「都被挡住了」可能只是因为裁剪函数把什么都丢掉了。
    """
    kept = skills_hub.apply_product_skill_source_policy(
        _sources(UPSTREAM_SOURCE_IDS), third_party_allowed=True
    )

    assert _ids(kept) == UPSTREAM_SOURCE_IDS


def test_branded_builds_drop_every_public_source():
    kept = skills_hub.apply_product_skill_source_policy(
        _sources(UPSTREAM_SOURCE_IDS), third_party_allowed=False
    )

    assert _ids(kept) == []


def test_official_source_is_not_treated_as_local():
    """`official` 名字像本地源，行为是网络。

    OptionalSkillSource 带 live-repo fallback：本地 optional-skills/ 里找不到就去
    GitHub 拉 NousResearch/hermes-agent（skills_hub.py `_fetch_from_live_repo`）。
    而我们决定不把 optional-skills/ 进包，于是它的每一次查找都落到那条回退上。
    """
    kept = skills_hub.apply_product_skill_source_policy(
        _sources(["official"]), third_party_allowed=False
    )

    assert _ids(kept) == []


def test_a_source_upstream_adds_later_is_closed_by_default():
    """白名单跟黑名单的差别只在这一条上。

    上游哪天加个 `awesome-skills`，黑名单会默认放行、等人发现；白名单默认关掉。
    """
    kept = skills_hub.apply_product_skill_source_policy(
        _sources([*UPSTREAM_SOURCE_IDS, "some-new-hub-upstream-adds"]),
        third_party_allowed=False,
    )

    assert _ids(kept) == []


def test_our_own_hub_gets_through_once_it_is_registered():
    """自有 hub 的预留接口：source_id 进白名单就生效。"""
    kept = skills_hub.apply_product_skill_source_policy(
        _sources([*UPSTREAM_SOURCE_IDS, "llano-hub"]),
        allowed=frozenset({"llano-hub"}),
        third_party_allowed=False,
    )

    assert _ids(kept) == ["llano-hub"]


def test_the_whitelist_is_no_longer_empty():
    """紧急拉闸时仍保留公司 Hub 与受支持的公共 SkillHub。

    这条钉的是**拉闸之后剩下什么**。空集合的年代，拉闸 = 一个远程技能源都没有，
    员工连公司自己的技能都装不了；现在拉闸 = 只剩公司自己的，这才是应急开关
    该有的样子。

    落在这里而不是靠上面那条参数化用例，是因为那条显式传 allowed=...，
    测的是裁剪函数；这条测的是**模块里那个真常量**。
    """
    assert skills_hub.PRODUCT_SKILL_SOURCE_IDS == frozenset(
        {"llano-hub", "skillhub"}
    )


@pytest.fixture
def _managed(tmp_path, monkeypatch):
    from hermes_cli import managed_scope

    managed_scope.invalidate_managed_cache()
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(tmp_path))
    yield tmp_path
    managed_scope.invalidate_managed_cache()


def _write_policy(path, *, disabled, skill_hub_url=""):
    (path / "config.yaml").write_text(
        "product:\n"
        "  enabled: true\n"
        "  brand_id: llano-ai-workbench\n"
        "  provider_id: llano-gateway\n"
        f"  skill_hub_url: {skill_hub_url}\n"
        "  disabled_capabilities:\n" + "".join(f"    - {d}\n" for d in disabled),
        encoding="utf-8",
    )


def test_capability_flows_from_the_managed_config(_managed):
    """闸门读的是真的品牌配置，不是测试里手搓的布尔。"""
    _write_policy(_managed, disabled=["third_party_skill_sources"])

    assert product_policy.capability_allowed(product_policy.THIRD_PARTY_SKILL_SOURCES) is False
    assert _ids(skills_hub.apply_product_skill_source_policy(_sources(UPSTREAM_SOURCE_IDS))) == []


def test_invalid_declared_managed_config_keeps_public_sources_closed(_managed):
    (_managed / "config.yaml").write_text("product: [", encoding="utf-8")
    from hermes_cli import managed_scope

    managed_scope.invalidate_managed_cache()

    assert product_policy.capability_allowed(
        product_policy.THIRD_PARTY_SKILL_SOURCES
    ) is False
    assert _ids(
        skills_hub.apply_product_skill_source_policy(_sources(UPSTREAM_SOURCE_IDS))
    ) == []


def test_a_policy_without_the_capability_leaves_sources_alone(_managed):
    """反向基线：品牌构建但没关这个能力位时，来源不该被裁。"""
    _write_policy(_managed, disabled=["nous_services"])

    assert product_policy.capability_allowed(product_policy.THIRD_PARTY_SKILL_SOURCES) is True
    assert _ids(skills_hub.apply_product_skill_source_policy(_sources(UPSTREAM_SOURCE_IDS))) == UPSTREAM_SOURCE_IDS


def test_skill_hub_url_is_carried_through_for_when_the_hub_exists(_managed):
    _write_policy(_managed, disabled=["third_party_skill_sources"], skill_hub_url="https://hub.example.internal")

    assert product_policy.product_skill_hub_url() == "https://hub.example.internal"


def test_skill_hub_url_is_empty_until_configured(_managed):
    _write_policy(_managed, disabled=["third_party_skill_sources"])

    assert product_policy.product_skill_hub_url() == ""


def test_create_source_router_actually_applies_the_policy(_managed):
    """裁剪函数写对了不等于接上了。

    变异验证抓到过：把 `create_source_router` 末尾那句策略调用去掉，上面所有
    用例照样全绿 —— 它们测的是裁剪函数，没有一条测装配点。

    ⚠️ 这条曾经断言 `== []`。阶段 11 之前白名单是空的，拉闸之后确实一个源都不剩；
    hub 落地之后剩下的是它。改的是事实，不是把用例放宽。
    """
    _write_policy(_managed, disabled=["third_party_skill_sources"])

    assert _ids(skills_hub.create_source_router()) == ["llano-hub", "skillhub"]


def test_create_source_router_is_untouched_without_the_policy(_managed):
    """不设策略时：上游 9 个一个不少，外加公共与公司 hub。"""
    _write_policy(_managed, disabled=["nous_services"])

    assert _ids(skills_hub.create_source_router()) == [
        "llano-hub",
        "skillhub",
        *UPSTREAM_SOURCE_IDS,
    ]


def test_our_own_hub_is_searched_first(_managed):
    """顺序不是随手排的。

    上游 `parallel_search_sources` 会按来源顺序做去重与保底名额，排在前面的先占位。
    同名技能优先装公司自己那份，而不是公网上碰巧同名的 —— 后者是个真实的攻击面
    （在 clawhub 上发一个同名技能，赌员工装错）。
    """
    _write_policy(_managed, disabled=["nous_services"])

    assert _ids(skills_hub.create_source_router())[0] == "llano-hub"
