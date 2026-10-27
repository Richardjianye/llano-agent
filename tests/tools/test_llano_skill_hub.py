"""公司自有 skill hub 的来源适配器（阶段 11 · S11-E）

这一层的职责很窄：把网关的 JSON 翻译成上游的 SkillMeta / SkillBundle，
并且**在任何一步走不通的时候安静地让路**。所以用例分两组：

1. **翻译对不对** —— 尤其是编码信封的还原（utf-8 出 str、base64 出 bytes），
   这是「分发出去的技能装上之后是不是发布者那份」的全部保证
2. **走不通的时候不炸** —— 没配网关、没登录、连不上、返回体变形，
   一律返回空/None 让下游来源接着找

⚠️ 没有一条用例真的发 HTTP。`_request` 是被替换掉的注入点，测的是它前后的翻译
与降级；真发请求那一段由 scripts/e2e/skill_hub_chain.py 对着真网关验。
"""

from __future__ import annotations

import base64

import pytest

from tools import llano_skill_hub as hub
from tools.llano_skill_hub import LlanoHubSource, SkillHubUnavailable, decode_files

SKILL_MD = "---\nname: demo\n---\n\n# Demo\n"


@pytest.fixture
def _online(monkeypatch):
    """把 hub 的 HTTP 调用换成一个可编排的假货，返回记录下来的调用列表。"""
    calls = []
    responses = {}

    def fake_request(path, *, method="GET", body=None):
        calls.append((method, path, body))
        if path in responses:
            value = responses[path]
            if isinstance(value, Exception):
                raise value
            return value
        raise SkillHubUnavailable(f"没有为 {path} 编排响应")

    monkeypatch.setattr(hub, "_request", fake_request)
    monkeypatch.setattr(hub, "hub_origin", lambda: "https://gw.example.internal")
    return calls, responses


# ── 编码信封的还原 ────────────────────────────────────────────────────────

def test_text_files_come_back_as_str_and_binaries_as_bytes():
    """SkillBundle.files 声明的是 Dict[str, Union[str, bytes]]，下游按类型分别写盘。

    全还原成 str 的话，一张 png 会被当成文本写出去 —— 文件在、大小对不上、
    打不开，而且没有任何一步会报错。
    """
    blob = bytes(range(256))
    out = decode_files([
        {"path": "SKILL.md", "encoding": "utf-8", "content": SKILL_MD},
        {"path": "assets/logo.png", "encoding": "base64",
         "content": base64.b64encode(blob).decode("ascii")},
    ])

    assert out["SKILL.md"] == SKILL_MD
    assert isinstance(out["SKILL.md"], str)
    assert out["assets/logo.png"] == blob
    assert isinstance(out["assets/logo.png"], bytes)


def test_a_file_with_broken_base64_is_skipped_not_fatal():
    """坏掉一个 asset 就丢掉整个技能包，是把小故障放大成大故障。

    缺一张图的技能多半还能用；一个都装不上肯定不能用。
    """
    out = decode_files([
        {"path": "SKILL.md", "encoding": "utf-8", "content": SKILL_MD},
        {"path": "assets/x.bin", "encoding": "base64", "content": "!!!"},
    ])

    assert set(out) == {"SKILL.md"}


def test_encoding_defaults_to_text_when_omitted():
    assert decode_files([{"path": "a.md", "content": "hi"}]) == {"a.md": "hi"}


def test_entries_without_a_usable_path_or_content_are_dropped():
    """网关不该发出这种东西，但客户端不能因为对方发错就崩。"""
    out = decode_files([
        {"path": "", "content": "x"},
        {"path": "ok.md", "content": "y"},
        {"path": "no-content.md"},
        "不是对象",
    ])

    assert out == {"ok.md": "y"}


# ── 翻译 ─────────────────────────────────────────────────────────────────

def test_search_translates_gateway_json_into_skill_meta(_online):
    calls, responses = _online
    responses["/v1/skill-hub/skills?q=report&limit=10"] = {
        "skills": [{
            "slug": "lingxing-daily-report",
            "name": "领星日报",
            "description": "拉当日订单",
            "version": 3,
            "owner_username": "小王",
            "owner_dept_code": "ops",
            "install_count": 12,
        }]
    }

    metas = LlanoHubSource().search("report", limit=10)

    assert len(metas) == 1
    meta = metas[0]
    assert meta.identifier == "llano-hub/lingxing-daily-report"
    assert meta.source == "llano-hub"
    assert meta.trust_level == "trusted"
    assert meta.extra["owner"] == "小王"
    assert meta.extra["version"] == 3



def test_the_publisher_and_latest_publish_time_reach_the_desktop_payload(_online):
    """技能中心每行显示「谁、什么时候发布的」：owner 与 published_at 一路透传到界面载荷。"""
    from hermes_cli.web_server import _skill_meta_to_payload

    calls, responses = _online
    responses["/v1/skill-hub/skills?q=report&limit=10"] = {
        "skills": [{"slug": "a", "name": "甲", "owner_username": "小王", "published_at": "2026-09-23T15:39:06"},
                   {"slug": "b", "name": "乙"}]
    }

    new, old = (_skill_meta_to_payload(m) for m in LlanoHubSource().search("report", limit=10))

    assert new["owner"] == "小王" and new["published_at"] == "2026-09-23T15:39:06"
    # 老网关不回 published_at：空串，界面就不显示那半句
    assert old["owner"] == "" and old["published_at"] == ""

def test_results_carry_the_provider_label_upstream_source_filtering_reads(_online):
    """`hermes skills search --source LLANO Agent` 要能筛出公司自己的技能。

    上游 _filter_results_by_provider 读的就是 extra.provider 这个键。
    不填的话这条过滤对 hub 的技能永远返回空，而且不报错。
    """
    calls, responses = _online
    responses["/v1/skill-hub/skills?q=&limit=10"] = {
        "skills": [{"slug": "a", "name": "A", "description": ""}]
    }

    assert LlanoHubSource().search("").pop().extra["provider"] == "LLANO Agent"


def test_fetch_builds_a_bundle_with_the_right_trust_level(_online):
    """trust_level 是 trusted 不是 community。

    看 skills_guard.INSTALL_POLICY：community 的 caution 就 block，而技能里出现
    网络调用、写文件本来就正常，正是 caution 的主要来源。标 community 等于让员工
    自己写的技能天天被拦 —— 赔进去的是他这个月的技能指标。
    trusted 放开的只有 caution 这一档，dangerous 照样硬拦且 --force 越不过。
    """
    calls, responses = _online
    responses["/v1/skill-hub/skills/demo/bundle"] = {
        "slug": "demo",
        "name": "演示",
        "version": 2,
        "bundle_hash": "h" * 64,
        "owner_username": "小王",
        "files": [{"path": "SKILL.md", "encoding": "utf-8", "content": SKILL_MD}],
    }

    bundle = LlanoHubSource().fetch("demo")

    assert bundle is not None
    assert bundle.trust_level == "trusted"
    assert bundle.source == "llano-hub"
    assert bundle.files == {"SKILL.md": SKILL_MD}
    assert bundle.metadata["version"] == 2


def test_the_bundle_carries_a_source_url_for_provenance(_online):
    """source_url_for_bundle 优先读 metadata["source_url"]，做的是溯源展示。

    不填的话它退回去展示裸 identifier（"demo"），员工看到的溯源信息是一个
    没有上下文的词。
    """
    calls, responses = _online
    responses["/v1/skill-hub/skills/demo/bundle"] = {
        "slug": "demo",
        "files": [{"path": "SKILL.md", "encoding": "utf-8", "content": SKILL_MD}],
    }

    from tools.skills_hub import source_url_for_bundle

    url = source_url_for_bundle(LlanoHubSource().fetch("demo"))

    assert url == "https://gw.example.internal/v1/skill-hub/skills/demo"


def test_identifiers_are_url_escaped(_online):
    """slug 已经在网关侧被限死在 [a-z0-9-]，但这一层不能依赖那个假设。

    把没转义的字符串拼进 URL，`../` 这类输入会变成打到别的端点上的请求。
    """
    calls, responses = _online
    responses["/v1/skill-hub/skills/..%2F..%2Fadmin"] = {"slug": "x"}

    LlanoHubSource().inspect("../../admin")

    assert calls[0][1] == "/v1/skill-hub/skills/..%2F..%2Fadmin"


# ── 走不通的时候让路 ──────────────────────────────────────────────────────

def test_every_read_path_degrades_quietly_when_the_hub_is_unreachable(monkeypatch):
    """没配网关 / 没登录 / 连不上 —— 一律返回空，让下游来源接着找。

    这是上游 HermesIndexSource 已经立的规矩。一次网络抖动不该让
    `hermes skills search` 整个报错，员工也不该因为 VPN 没连就以为技能库空了。
    """
    def boom(*args, **kwargs):
        raise SkillHubUnavailable("连不上网关")

    monkeypatch.setattr(hub, "_request", boom)
    source = LlanoHubSource()

    assert source.search("anything") == []
    assert source.inspect("demo") is None
    assert source.fetch("demo") is None


def test_a_malformed_response_does_not_crash_the_search(_online):
    """网关返回体变形（版本不匹配、被中间层改过）时也不能炸。"""
    calls, responses = _online
    for bad in [{}, {"skills": None}, {"skills": "not a list"}, []]:
        responses["/v1/skill-hub/skills?q=&limit=10"] = bad
        assert LlanoHubSource().search("") == []


def test_a_bundle_without_files_is_treated_as_missing(_online):
    """空包不是「装了个空技能」，是「没这个东西」。"""
    calls, responses = _online
    for bad in [{"slug": "demo"}, {"slug": "demo", "files": []},
                {"slug": "demo", "files": [{"path": "", "content": "x"}]}]:
        responses["/v1/skill-hub/skills/demo/bundle"] = bad
        assert LlanoHubSource().fetch("demo") is None


# ── hub 地址的解析 ────────────────────────────────────────────────────────

def test_hub_falls_back_to_the_gateway_origin(monkeypatch):
    """skill_hub_url 是留给「hub 独立部署」的。在那之前它和网关是同一个服务。

    逼着运维再填一遍同样的地址只会多一个能填错的地方 —— 而填错的表现是
    「搜不到公司技能」，没有任何一处会报错。
    """
    # hub_origin 是在函数体里 import 的，所以要打在 product_policy 上，
    # 打在 hub 模块的名字空间里没有用（那里根本没有这个名字）。
    import hermes_cli.product_policy as pp

    monkeypatch.setattr(pp, "product_skill_hub_url", lambda: "")
    # hub_origin 读的是三态的 load_product_policy_status：托管配置损坏时要安全停用，
    # 不能和「普通 Hermes」混为一谈。
    policy = type("P", (), {"gateway_origin": "https://gw.example.internal/"})()
    monkeypatch.setattr(
        pp, "load_product_policy_status",
        lambda: pp.ProductPolicyStatus(pp.ProductPolicyState.ACTIVE, policy=policy),
    )

    assert hub.hub_origin() == "https://gw.example.internal"


def test_an_explicit_hub_url_wins(monkeypatch):
    import hermes_cli.product_policy as pp

    monkeypatch.setattr(pp, "product_skill_hub_url", lambda: "https://hub.example.internal/")
    monkeypatch.setattr(
        pp, "load_product_policy",
        lambda: type("P", (), {"gateway_origin": "https://gw.example.internal"})(),
    )

    assert hub.hub_origin() == "https://hub.example.internal"


# ── 界面分组（2026-09-03）────────────────────────────────────────────────

def test_a_company_pick_groups_under_its_own_heading(_online):
    """晋级过的技能归「公司精选」，不归发布它的部门。

    可见性放开之后（技能库对所有人可见），晋级只剩下「排前面 + 单独成组」这一个
    作用。它要是仍然落在原部门组里，主管点那一下就完全看不出效果了。
    """
    calls, responses = _online
    responses["/v1/skill-hub/skills?q=&limit=10"] = {
        "skills": [{
            "slug": "a", "name": "A", "description": "",
            "scope": "company", "scope_dept_name": "视觉设计部",
        }]
    }

    assert LlanoHubSource().search("").pop().extra["group"] == "公司精选"


def test_a_department_skill_groups_under_the_department_name(_online):
    calls, responses = _online
    responses["/v1/skill-hub/skills?q=&limit=10"] = {
        "skills": [{
            "slug": "a", "name": "A", "description": "",
            "scope": "dept", "scope_dept_name": "视觉设计部", "owner_dept_code": "visual-design",
        }]
    }

    assert LlanoHubSource().search("").pop().extra["group"] == "视觉设计部"


def test_a_missing_department_name_falls_back_to_the_code_then_to_ungrouped(_online):
    """网关查不到部门名时退到 code，再退到「未分组」。

    ⚠️ 不能编一个「未知部门(31)」这类看起来像模像样的名字：那样分不清是数据
    没配全，还是这个技能本来就没有归属，而两者的处理方式完全不同。
    """
    calls, responses = _online
    responses["/v1/skill-hub/skills?q=&limit=10"] = {
        "skills": [
            {"slug": "a", "name": "A", "description": "", "scope": "dept",
             "owner_dept_code": "visual-design"},
            {"slug": "b", "name": "B", "description": "", "scope": "dept"},
        ]
    }

    groups = [m.extra["group"] for m in LlanoHubSource().search("")]

    assert groups == ["visual-design", "未分组"]
