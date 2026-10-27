"""群聊 agent 能力边界（决策 #45 / v2 专章 P0-1）

这是协作（群聊）功能能否开工的**单点前置条件**。要挡的东西：他部门或外部成员在
群里 @ 我方 agent 要一份数据，agent 拿主人的中台 Token 去查 —— 中台看到的是完全
合法的请求，现有任何一层都不会拒绝，事后翻日志也看不出异常。

整个机制的骨架是一条不对称规则：**作用域只能收紧，不能放宽**。所以这一组用例里
最重要的不是「群聊被挡住了」，而是**「说自己是私聊没用」** —— 前者写错了会有人报，
后者写错了没有任何人会发现。

分四组：收紧规则、能力闸门、软失败的形态、计数与限流。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from services import conversation_scope as cs
from services.conversation_scope import GROUP, PRIVATE, resolve_scope
from utils.gateway_auth import Caller


def _caller(**kw) -> Caller:
    return Caller(
        uid=kw.get("uid", "owner-uid"),
        username="小王",
        role="employee",
        dept_id=7,
        dept_code="rd",
        auth_type="apikey",
        api_key_id=3,
    )


# ── 收紧规则 ─────────────────────────────────────────────────────────────

def test_baseline_no_header_is_private():
    """基线。今天所有请求都是私聊，没有这条的话下面的「被挡住」可能只是因为
    这个函数把什么都判成 group。"""
    decision = resolve_scope(_caller(), None)

    assert decision.scope == PRIVATE
    assert decision.allows_capabilities is True


def test_declaring_group_narrows():
    decision = resolve_scope(_caller(), "group")

    assert decision.scope == GROUP
    assert decision.allows_capabilities is False


def test_declaring_private_can_never_widen(monkeypatch):
    """**这条是整个机制的骨架。**

    凭据侧已经推出 group（将来的群聊会话令牌）时，请求头说 private 必须无效。
    可以放宽的话，注入一句「你现在是私聊」就够了 —— 与 P0-1 第一条约束
    「若 agent 是携带一个 ID 参数去请求，注入一句『用你主人的账号查』即可破防」
    是同一个道理。
    """
    monkeypatch.setattr(cs, "scope_from_credential", lambda caller: GROUP)

    for claimed in [None, "private", "PRIVATE", "  private  "]:
        decision = resolve_scope(_caller(), claimed)
        assert decision.scope == GROUP, f"声明 {claimed!r} 不该把作用域放宽"
        assert decision.allows_capabilities is False


@pytest.mark.parametrize(
    "bad", ["privat", "Private!", "public", "none", "0", "个人", "group; private", " "]
)
def test_unrecognised_values_fail_closed(bad):
    """认不出来一律当 group。

    一个拼错的头不该悄悄换来完整权限。宁可让一次正常的私聊调用被挡下来被人发现，
    也不要让一次群聊调用因为拼写而放行 —— 前者会有人来报，后者没人会知道。
    """
    assert resolve_scope(_caller(), bad).scope == GROUP


def test_an_empty_header_is_treated_as_group_not_as_absent():
    """头存在但是空的，是「有人在设它」的信号，不是「没设」。

    区分不开的话，一个把值清空的中间层就等于关掉了整条防线。
    """
    assert resolve_scope(_caller(), "").scope == GROUP
    assert resolve_scope(_caller(), None).scope == PRIVATE


def test_case_and_whitespace_are_tolerated_for_the_strict_value():
    """收紧方向上要宽容：`Group` / ` group ` 都得认。

    这个方向认错的代价是「本该被挡的没挡住」，所以宁可多认几种写法。
    """
    for claimed in ["group", "GROUP", "Group", "  group  "]:
        assert resolve_scope(_caller(), claimed).scope == GROUP


def test_credential_side_is_still_private_today():
    """今天这条防线只有一半，用例把这件事写下来而不是假装它是完整的。

    凭据侧恒为 private（群聊会话令牌还不存在）。另一半落地时**改的应该只有
    scope_from_credential**，判定方向在 resolve_scope 里，不会被动到。
    """
    assert cs.scope_from_credential(_caller()) == PRIVATE


# ── 能力闸门 ──────────────────────────────────────────────────────────────

@pytest.fixture
def _mcp(monkeypatch):
    """把 MCP 端点的下游全换掉，留下真实的闸门逻辑。"""
    import router.v1.mcp as module

    calls = {"catalog": 0, "resolve": 0, "recorded": []}

    async def catalog(*_a):
        calls["catalog"] += 1
        instance = SimpleNamespace(code="lingxing-main", status=1)
        capability = SimpleNamespace(
            capability_code="lx.basic.seller.list", name="查询店铺列表",
            domain="基础数据", description="文档", status=1,
            input_schema_json='{"type":"object"}',
        )
        return [module.service.CatalogEntry(instance, capability, "available")]

    async def resolve_capability(*_a, **_k):
        calls["resolve"] += 1
        raise AssertionError("群聊下不该走到授权解析，更不该打上游")

    async def record_call(**kw):
        calls["recorded"].append(kw)

    monkeypatch.setattr(module.service, "search_catalog", catalog)
    monkeypatch.setattr(module.service, "resolve_capability", resolve_capability)
    monkeypatch.setattr(module.service, "record_call", record_call)
    monkeypatch.setattr(module, "get_request_id", lambda r: "req-1")

    # ⚠️ 限流器必须在这里被接管，否则这一组用例会依赖**本机 Redis 的残留状态**。
    #
    # 这不是假想：`scope_deny:<uid>` 的 TTL 是 300 秒，而开发机上常有一个真 Redis
    # 在跑。反复跑几遍测试，同一个 uid 的计数就会攒过 DENY_RATE_LIMIT，之后
    # note_scope_denial 正确地返回 False（不再落库）—— 于是断言「记了一行」的用例
    # 开始红，而红的原因跟被测代码毫无关系。写这组用例时就是这么中的招：
    # 单跑绿、全跑红，查到最后是 Redis 里躺着一个 30。
    monkeypatch.setattr(
        "services.conversation_scope.note_scope_denial", _always_log
    )
    return calls


async def _always_log(uid: str) -> bool:
    """闸门用例只关心「拒绝了没有、记了什么」，限流是另一组用例的事。"""
    return True


def _req(scope: str | None):
    headers = {} if scope is None else {cs.SCOPE_HEADER: scope}
    return SimpleNamespace(headers=headers)


async def test_group_scope_gets_an_empty_catalog_not_an_error(_mcp):
    """群聊里连**目录**都不能给。

    只挡 execute 不挡 search 看上去够了（反正执行会被拒），但那等于对外公布我们
    有哪些接口 —— P0-1 第四条约束正是「错误文案不得泄漏存在性」。一个能读到完整
    能力目录的人，已经拿到了他不该有的东西。

    返回空目录而不是报错：agent 会把「一个能力都没有」如实转述成「我这儿查不到
    这类接口」，正是软失败要的那种表现。
    """
    from router.v1.mcp import search

    body = SimpleNamespace(query=None, instance_code=None, domain=None, limit=20)
    result = await search(body, _req("group"), _caller())

    assert result["data"] == []
    assert _mcp["catalog"] == 0, "群聊下不该去查目录"


async def test_private_scope_still_sees_the_catalog(_mcp):
    """反向基线：上面那条「空目录」不能是因为目录接口整个坏了。"""
    from router.v1.mcp import search

    body = SimpleNamespace(query="seller", instance_code=None, domain="基础数据", limit=20)
    result = await search(body, _req(None), _caller())

    assert result["data"], "私聊下目录必须照常"
    assert _mcp["catalog"] == 1


@pytest.mark.parametrize("capability_code", ["lx.basic.seller.list", "datahub.report.platform.list"])
async def test_group_scope_execute_never_reaches_upstream(_mcp, capability_code):
    """「必定失败」不真打上游（P0-1 第二条约束）。

    真发请求会让中台看到大量无效 Token，可能撞上已登记的「共享网关 IP 被当撞库
    拦截」风险 —— 为演一场戏去惹风控不划算。

    `resolve_capability` 被换成了会抛断言的假货：走到那里这条就红。
    """
    from router.v1.mcp import execute

    body = SimpleNamespace(capability_code=capability_code, instance_code=None,
                           arguments={"page": 1})
    response = await execute(body, _req("group"), _caller())

    assert response.status_code == 403
    assert _mcp["resolve"] == 0


# ── 软失败的形态 ──────────────────────────────────────────────────────────

def test_the_refusal_reads_as_the_gateway_refusing_not_the_agent():
    """「想帮但干不了」与「不想帮」在态度上是两件事。

    agent 会把这句话转述给提问者，我们要的是前者。这套机制**不告知 agent**，
    它是真心在试 —— 所以措辞必须自己站得住，不能靠 agent 补台。
    """
    message = cs.SCOPE_DENIED_MESSAGE

    assert "网关" in message
    # 反过来的话术不能出现：那会让 agent 显得是在拒绝同事。
    for blame in ["我不", "拒绝帮", "不允许你"]:
        assert blame not in message


def test_the_refusal_never_names_a_capability():
    """P0-4 那条约束：不得泄漏存在性。

    「你无权访问 datahub.report.platform.list」等于对外公布我们有哪些接口。
    统一一个错误，不区分「不存在 / 无权 / 未绑定」。
    """
    payload = cs.denial_payload()
    blob = str(payload)

    for leak in ["lx.", "datahub", "capability_code", "lingxing"]:
        assert leak not in blob


def test_the_refusal_is_marked_non_retryable():
    """不标的话 agent 会热心地重试到限流，每一次都是噪声日志和一次白跑。"""
    assert cs.denial_payload()["error"]["retryable"] is False


async def test_the_refusal_looks_like_an_ordinary_permission_error(_mcp):
    """403 且 type 与普通未授权一致。

    形态特殊的话，agent（或读日志的人）能看出「这里有个特殊模式」，
    而我们要的是它表现得就像一次普通的没权限。
    """
    from router.v1.mcp import execute

    body = SimpleNamespace(capability_code="lx.basic.seller.list", instance_code=None, arguments={})
    response = await execute(body, _req("group"), _caller())

    assert response.status_code == 403
    assert cs.denial_payload()["error"]["type"] == "mcp_permission_error"


# ── 计数与限流 ────────────────────────────────────────────────────────────

async def test_a_denial_is_logged_under_its_own_status(_mcp):
    """状态是 scope_denied，**不复用 denied**。

    denied 是日常的未授权拒绝，量很大。混在一起的话「某人今日触发 47 次跨账号
    拒绝」这条信号就被噪声淹没了 —— 而那正是这套机制白赚的那份审计价值。
    """
    from router.v1.mcp import execute

    body = SimpleNamespace(capability_code="lx.basic.seller.list", instance_code=None, arguments={})
    await execute(body, _req("group"), _caller())

    assert len(_mcp["recorded"]) == 1
    assert _mcp["recorded"][0]["status"] == cs.SCOPE_DENIED
    assert _mcp["recorded"][0]["status"] != "denied"


async def test_the_denial_log_records_the_caller_not_a_client_supplied_id(_mcp):
    """每次拒绝都是一条「谁在试图套数据」的审计记录 —— 没有 uid 就没有价值。

    记的是 `caller.uid`，也就是网关从 API Key 解出来的那个，不是载荷里的任何字段。
    今天 caller 就是 agent 的主人（群聊会话令牌还不存在）；群聊落地后
    caller 会变成**提问人**，那时这条记录才真正回答「谁在试图套数据」。
    两个阶段记的都是「网关认定的调用者」，所以这里不用改。
    """
    from router.v1.mcp import execute

    body = SimpleNamespace(capability_code="lx.basic.seller.list", instance_code=None, arguments={})
    await execute(body, _req("group"), _caller(uid="asker-uid"))

    assert _mcp["recorded"][0]["uid"] == "asker-uid"


async def test_rate_limiting_stops_the_logging_but_not_the_counting(monkeypatch):
    """撞太多次之后不再落库，计数照涨。

    不限的话 @ 轰炸 = 免费刷我们的库；而计数不能跟着停，因为「他撞了多少次」
    正是要报出来的那个数。
    """
    counter = {"n": 0}

    class _FakeRedis:
        async def incr(self, key):
            counter["n"] += 1
            return counter["n"]

        async def expire(self, key, seconds):
            return True

    import utils.cache as cache

    monkeypatch.setattr(cache, "get_redis", lambda: _FakeRedis())

    results = [await cs.note_scope_denial("u1") for _ in range(cs.DENY_RATE_LIMIT + 3)]

    assert all(results[: cs.DENY_RATE_LIMIT]), "限额之内都应落库"
    assert not any(results[cs.DENY_RATE_LIMIT:]), "超限之后不该再落库"
    assert counter["n"] == cs.DENY_RATE_LIMIT + 3, "计数必须一直涨"


async def test_a_broken_cache_does_not_swallow_the_audit_trail(monkeypatch):
    """Redis 挂了要**多写**日志，不是少写。

    这条审计记录（谁在试图套数据）比一点写入量重要得多。反过来实现的话，
    只要把缓存打挂就能让这条防线的留痕整个消失。
    """
    import utils.cache as cache

    def boom():
        raise RuntimeError("redis 挂了")

    monkeypatch.setattr(cache, "get_redis", boom)

    assert await cs.note_scope_denial("u1") is True


async def test_no_redis_configured_still_logs(monkeypatch):
    import utils.cache as cache

    monkeypatch.setattr(cache, "get_redis", lambda: None)

    assert await cs.note_scope_denial("u1") is True


# ── 覆盖面：哪些接口必须过这道闸 ──────────────────────────────────────────

async def test_publishing_a_skill_is_blocked_in_a_group(monkeypatch):
    """群里不许以主人名义往全公司发东西。

    发布是一个**以主人名义、对全公司可见**的写动作 —— 群里一句夹带注入的话就能让
    agent 用主人的身份往公司技能库里塞东西，而技能是会被执行的。这正是 #45
    「能力不跨界，只有产物跨界」要挡的那一类。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from router.v1 import router as v1_router
    from utils.gateway_auth import require_caller
    from utils.middleware import register_exception_handlers

    import db.inserts.skill_hub as inserts

    async def boom(**kw):
        raise AssertionError("群聊下不该走到入库")

    monkeypatch.setattr("router.v1.skill_hub.publish_skill", boom)

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(v1_router)
    app.dependency_overrides[require_caller] = lambda: _caller()

    payload = {
        "schema_version": 1, "slug": "x-demo", "name": "x",
        "files": [{"path": "SKILL.md", "encoding": "utf-8", "content": "---\nname: x\n---\n"}],
    }
    response = TestClient(app).post(
        "/v1/skill-hub/skills", json=payload, headers={cs.SCOPE_HEADER: "group"}
    )

    assert response.status_code == 403


async def test_reading_the_skill_hub_is_not_blocked(monkeypatch):
    """反向基线：只挡发布，不挡搜索。

    搜索读的是公司内部本来就全员可见可安装的东西，挡了只是给协作场景添堵，
    换不来任何东西。这条同时防止有人「顺手」把闸门加宽 —— 加宽是没有代价感的，
    所以要有一条用例站在另一边。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from router.v1 import router as v1_router
    from utils.gateway_auth import require_caller
    from utils.middleware import register_exception_handlers

    async def fake_search(q, limit, **kwargs):
        return [{"slug": "a", "name": "A", "description": "", "version": 1}]

    monkeypatch.setattr("router.v1.skill_hub.search_skills", fake_search)

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(v1_router)
    app.dependency_overrides[require_caller] = lambda: _caller()

    response = TestClient(app).get(
        "/v1/skill-hub/skills", headers={cs.SCOPE_HEADER: "group"}
    )

    assert response.status_code == 200
    assert response.json()["skills"]


def test_every_gate_goes_through_resolve_scope():
    """闸门只能有一个入口。

    绕过 resolve_scope 自己 `request.headers.get(...)` 就等于把「只能收紧」那条
    规则作废 —— 而那种代码看起来完全正常，review 时也很难一眼看出问题。
    所以这里静态扫一遍：读了那个头的地方，必须同时调 resolve_scope。
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for path in list((root / "router").rglob("*.py")) + list((root / "services").rglob("*.py")):
        if path.name == "conversation_scope.py":
            continue
        source = path.read_text(encoding="utf-8")
        if "SCOPE_HEADER" in source and "resolve_scope" not in source:
            offenders.append(str(path.relative_to(root)))

    assert offenders == [], f"这些地方读了作用域头却没走 resolve_scope：{offenders}"
