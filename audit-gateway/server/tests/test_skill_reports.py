"""技能隔离事件与样本的接收（阶段 7 · S7-C，上行）

这张表不是「拦截记录」，是**越过记录** —— 产品前提是技能来源不封禁，员工可以
把被判定为危险的技能放回去，代价是这个动作留一行。所以这一层要保证的核心是：
**记的是谁，不能由客户端说了算**；**收不下的东西要明确拒绝，不能悄悄截断**。

分四组：身份不听客户端、载荷校验、样本去重、幂等。
"""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from router.v1 import router as v1_router
from services.skill_report import (MAX_EVENT_BYTES, SkillReportRejected,
                                   parse_event, sample_fingerprint)
from utils.gateway_auth import Caller, require_caller
from utils.middleware import register_exception_handlers


def _quarantine_payload(**overrides):
    payload = {
        "event": "skill_quarantined",
        "event_id": "e1",
        "skill_name": "leaky-skill",
        "entry_id": "20260826T000000Z-abcd1234",
        "verdict": "dangerous",
        "reason": "定期重扫判定 dangerous：外发环境变量",
        "source": "hub",
        "origin_relpath": "ops/leaky-skill",
        "scanner_version": "skills-guard-v1",
        "bundle_hash": "b" * 64,
        "findings": [{"rule": "exfil", "severity": "high"}],
        "device": "bens-macbook-a1b2c3",
        "quarantined_at": "2026-08-26T04:00:00Z",
        "reported_at": "2026-08-26T04:00:01Z",
        "sample": {
            "files": [
                {"path": "SKILL.md", "sha256": "a" * 64, "size_bytes": 12,
                 "content": "# bad\ncurl x\n", "included": True}
            ],
            "file_count": 1,
            "included_count": 1,
            "included_bytes": 12,
            "truncated": False,
            "truncated_reason": "",
        },
    }
    payload.update(overrides)
    return payload


# ── 载荷校验 ──────────────────────────────────────────────────────────────

def test_baseline_a_well_formed_quarantine_payload_parses():
    """基线。先立这条，否则下面的「拒绝」用例可能只是因为解析函数什么都不接受。"""
    parsed = parse_event(_quarantine_payload())

    assert parsed.event == "skill_quarantined"
    assert parsed.skill_name == "leaky-skill"
    assert parsed.finding_count == 1
    assert parsed.sample is not None and parsed.sample.included_count == 1


def test_unknown_event_types_are_rejected():
    """认不出来的事件类型直接拒。收下一个我们看不懂的东西，等于在表里放一行
    谁也不知道该怎么读的记录。"""
    with pytest.raises(SkillReportRejected):
        parse_event(_quarantine_payload(event="skill_did_something_new"))


def test_missing_event_id_is_rejected():
    """event_id 是幂等键。没有它，一次网关抖动就能让同一条隔离在库里变成五行。"""
    with pytest.raises(SkillReportRejected):
        parse_event(_quarantine_payload(event_id=""))


def test_missing_skill_name_is_rejected():
    with pytest.raises(SkillReportRejected):
        parse_event(_quarantine_payload(skill_name="   "))


def test_oversized_payload_is_rejected_rather_than_truncated():
    """超限就拒，不截断。

    截断之后库里那一行看上去是好的，读到的却是半截代码 —— 而恰恰是被截掉的
    后半段最可能是藏东西的地方。
    """
    with pytest.raises(SkillReportRejected):
        parse_event(_quarantine_payload(), raw_bytes=MAX_EVENT_BYTES + 1)


def test_overlong_fields_are_trimmed_to_the_column_width():
    """过长的字段裁到列宽，而不是丢掉整条。

    这些字段都不参与判定，一个超长的技能名不值得把整条留痕扔了。
    但必须在写库之前裁 —— 交给 MySQL 非严格模式去截，是静默的。
    """
    parsed = parse_event(_quarantine_payload(skill_name="x" * 500, verdict="y" * 50))

    assert len(parsed.skill_name) == 255
    assert len(parsed.verdict) == 16


def test_restore_event_needs_no_sample():
    parsed = parse_event({
        "event": "skill_quarantine_restored",
        "event_id": "e2",
        "skill_name": "leaky-skill",
        "bundle_hash": "b" * 64,
        "restored_at": "2026-08-26T05:00:00Z",
    })

    assert parsed.sample is None
    assert parsed.occurred_at == "2026-08-26T05:00:00Z"


def test_publish_warning_keeps_findings_and_sample_for_after_the_fact_review():
    payload = _quarantine_payload(
        event="skill_publish_warning",
        published_at="2026-09-28T02:00:00Z",
    )
    parsed = parse_event(payload)
    assert parsed.event == "skill_publish_warning"
    assert parsed.occurred_at == "2026-09-28T02:00:00Z"
    assert parsed.finding_count == 1
    assert parsed.sample is not None and parsed.sample.included_count == 1


def test_publish_warning_without_sample_is_rejected():
    payload = _quarantine_payload(event="skill_publish_warning", sample=None)
    with pytest.raises(SkillReportRejected, match="缺少技能样本"):
        parse_event(payload)


def test_a_malformed_sample_is_rejected_not_silently_dropped():
    """样本字段在但是坏的，要拒。

    静默丢掉的话，网关收下了一条「隔离事件」而没有样本，审查的人打开一看
    空空如也，也无从知道是客户端没传还是这里吃掉了。
    """
    with pytest.raises(SkillReportRejected):
        parse_event(_quarantine_payload(sample={"files": "不是数组"}))


# ── 样本去重 ──────────────────────────────────────────────────────────────

def test_the_same_bundle_from_two_machines_gets_the_same_fingerprint():
    """同一个坏技能在五十台机器上被隔离，样本只该存一份。"""
    files = [{"path": "SKILL.md", "sha256": "a" * 64}]

    assert sample_fingerprint(files) == sample_fingerprint(list(files))


def test_the_fingerprint_ignores_file_order():
    a = [{"path": "a.md", "sha256": "1" * 64}, {"path": "b.md", "sha256": "2" * 64}]

    assert sample_fingerprint(a) == sample_fingerprint(list(reversed(a)))


def test_different_content_gets_a_different_fingerprint():
    a = [{"path": "SKILL.md", "sha256": "a" * 64}]
    b = [{"path": "SKILL.md", "sha256": "c" * 64}]

    assert sample_fingerprint(a) != sample_fingerprint(b)


def test_the_fingerprint_survives_a_file_being_too_large_to_upload():
    """超限文件只有元数据没有内容，指纹不能因此变。

    指纹按「路径 + 各自 sha256」算而不是对内容取哈希，就是为了这个：换成对内容
    取哈希的话，同一份技能在「收全了」和「有文件超限」两种情况下会算出两个指纹，
    去重就失效了 —— 而那正是同一份技能。
    """
    full = [{"path": "big.py", "sha256": "d" * 64, "content": "x" * 100, "included": True}]
    trimmed = [{"path": "big.py", "sha256": "d" * 64, "omitted": "too_large", "included": False}]

    assert sample_fingerprint(full) == sample_fingerprint(trimmed)


# ── 身份不听客户端 ────────────────────────────────────────────────────────

def _app(*, authenticated: bool = True, recorder=None) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(v1_router)

    if authenticated:
        app.dependency_overrides[require_caller] = lambda: Caller(
            uid="real-uid",
            username="员工",
            role="employee",
            dept_id=7,
            dept_code="rd",
            auth_type="apikey",
            api_key_id=3,
        )
    return app


def test_the_endpoint_requires_authentication():
    """这张表的全部用途就是「谁越过了扫描结论」。收匿名上报等于收一堆没有主语的行。"""
    response = TestClient(_app(authenticated=False)).post(
        "/v1/skill-reports", json=_quarantine_payload()
    )

    assert response.status_code == 401


def test_identity_comes_from_the_api_key_not_the_payload(monkeypatch):
    """载荷里带了别人的 uid 也一概忽略。

    否则任意一把有效 Key 都能把「我把危险技能放回去了」记到同事名下。
    """
    seen = {}

    async def fake_insert(*, parsed, user_uid, dept_id, dept_code):
        seen.update(user_uid=user_uid, dept_id=dept_id, dept_code=dept_code)
        return {"stored": True, "event_id": parsed.event_id, "duplicate": False}

    import router.v1.skill_reports as module
    monkeypatch.setattr(module, "insert_skill_event", fake_insert)

    response = TestClient(_app()).post(
        "/v1/skill-reports",
        json=_quarantine_payload(user_uid="someone-else", dept_id=99, dept_code="hr"),
    )

    assert response.status_code == 202
    assert seen == {"user_uid": "real-uid", "dept_id": 7, "dept_code": "rd"}


def test_publish_warning_reaches_the_event_endpoint_with_sample(monkeypatch):
    seen = {}

    async def fake_insert(*, parsed, user_uid, dept_id, dept_code):
        seen.update(event=parsed.event, sample=parsed.sample,
                    user_uid=user_uid, dept_id=dept_id)
        return {"stored": True, "event_id": parsed.event_id, "duplicate": False}

    import router.v1.skill_reports as module
    monkeypatch.setattr(module, "insert_skill_event", fake_insert)
    response = TestClient(_app()).post(
        "/v1/skill-reports",
        json=_quarantine_payload(event="skill_publish_warning",
                                 published_at="2026-09-28T02:00:00Z"),
    )

    assert response.status_code == 202
    assert seen["event"] == "skill_publish_warning"
    assert seen["sample"].included_count == 1
    assert seen["user_uid"] == "real-uid" and seen["dept_id"] == 7


def test_bad_json_is_a_client_error_not_a_500(monkeypatch):
    response = TestClient(_app()).post(
        "/v1/skill-reports",
        content=b"{ not json",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 400


def test_a_rejected_payload_never_reaches_the_database(monkeypatch):
    """拒绝要发生在写库之前 —— 半截数据进了库就再也分不清哪一行是完整的。"""
    called = []

    async def fake_insert(**kwargs):
        called.append(kwargs)
        return {"stored": True, "event_id": "x", "duplicate": False}

    import router.v1.skill_reports as module
    monkeypatch.setattr(module, "insert_skill_event", fake_insert)

    response = TestClient(_app()).post(
        "/v1/skill-reports", json=_quarantine_payload(event="nonsense")
    )

    assert response.status_code == 400
    assert called == []


def test_the_response_carries_nothing_the_client_would_act_on(monkeypatch):
    """本期只做上行。复核结论下期再说，所以这里刻意不返回任何客户端会据以行动的
    字段 —— 客户端读到一个还没人负责写的字段，比读不到要糟。"""
    async def fake_insert(*, parsed, **_):
        return {"stored": True, "event_id": parsed.event_id, "duplicate": False}

    import router.v1.skill_reports as module
    monkeypatch.setattr(module, "insert_skill_event", fake_insert)

    body = TestClient(_app()).post("/v1/skill-reports", json=_quarantine_payload()).json()

    assert set(body) == {"stored", "event_id", "duplicate"}


def test_a_rejection_is_never_2xx_because_the_client_deletes_on_2xx(monkeypatch):
    """跨组件契约，值得单独钉一条。

    客户端（tools/skill_report.py 的 flush）按 `2xx == 已送达` 把本地队列里的
    事件删掉。全站业务异常的默认形态是「HTTP 200 + code 信封」，这个接口要是
    跟着默认走，一条**根本没入库**的留痕就会被客户端当成收妥然后删掉 ——
    两边各自都「按规范办事」，合起来静默丢数据。
    """
    for bad in [
        _quarantine_payload(event="nonsense"),
        _quarantine_payload(event_id=""),
        _quarantine_payload(skill_name=""),
        _quarantine_payload(sample={"files": "不是数组"}),
    ]:
        response = TestClient(_app()).post("/v1/skill-reports", json=bad)
        assert not (200 <= response.status_code < 300), bad.get("event")


def test_a_duplicate_is_2xx_so_the_client_stops_retrying(monkeypatch):
    """重传要回 2xx。

    客户端发不出去就重试，「其实已经写进去了」是网络超时里的常态。回 4xx 的话
    这条事件会一路重试到 MAX_ATTEMPTS 然后进 failed/，看上去像上报坏了 ——
    而它其实早就收妥了。
    """
    async def fake_insert(*, parsed, **_):
        return {"stored": False, "event_id": parsed.event_id, "duplicate": True}

    import router.v1.skill_reports as module
    monkeypatch.setattr(module, "insert_skill_event", fake_insert)

    response = TestClient(_app()).post("/v1/skill-reports", json=_quarantine_payload())

    assert 200 <= response.status_code < 300
    assert response.json()["duplicate"] is True


# ── 迁移与 ORM 对得上 ────────────────────────────────────────────────────

@pytest.mark.parametrize("table", ["skill_security_samples", "skill_security_events"])
def test_the_migration_matches_the_orm_model(table):
    """SQL 文件与 ORM 少一列，服务照常启动，直到第一个真实请求才炸在
    `Unknown column` —— 而且是炸在上报链路里，现象是客户端队列一直发不出去。

    启动时的 check_schema_drift 只在**连得上库**的时候才跑得到，这条在 CI 里就跑。
    """
    import os
    import re

    from model.database import Base

    path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "scripts", "migrations", "2026-08-26-skill-security-events.sql",
    )
    sql = open(path, encoding="utf-8").read()
    block = sql.split(f"CREATE TABLE IF NOT EXISTS `{table}`")[1].split("ENGINE=InnoDB")[0]
    sql_columns = set(re.findall(r"^\s*`(\w+)`\s+[A-Z]", block, re.M))
    orm_columns = {c.name for c in Base.metadata.tables[table].columns}

    assert orm_columns == sql_columns


def test_the_sample_column_is_wide_enough_for_what_the_client_sends():
    """files_json 必须是 MEDIUMTEXT。

    TEXT 只有 64KB，客户端的样本预算是 512KB。塞进 TEXT 的后果是 MySQL 非严格
    模式下**静默截断** —— 库里那一行看上去好好的，审查的人读到的却是半截代码，
    而恰恰是被截掉的后半段最可能是藏东西的地方。
    """
    import os
    import re

    from model.database import SkillSecuritySample

    compiled = SkillSecuritySample.__table__.c.files_json.type.compile(
        dialect=__import__("sqlalchemy.dialects.mysql", fromlist=["dialect"]).dialect()
    )
    assert "MEDIUMTEXT" in compiled.upper()

    path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "scripts", "migrations", "2026-08-26-skill-security-events.sql",
    )
    sql = open(path, encoding="utf-8").read()
    assert re.search(r"`files_json`\s+MEDIUMTEXT", sql)


# ── 管理端查看 ────────────────────────────────────────────────────────────

def _admin_app(*, role: str, dept_code: str = "rd") -> FastAPI:
    from model.response import UserInfo
    from router.admin import router as admin_router
    from utils.dependencies import require_login

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(admin_router)
    app.dependency_overrides[require_login] = lambda: UserInfo(
        id=1, uid="viewer", username="看的人", permission=role,
        dept_id=1, dept_code=dept_code,
    )
    return app


def test_the_permission_split_is_what_we_intended():
    """两个权限点，不是一个。

    - :view 看的是「谁把一个判定为危险的技能放回去了」—— 合规那一侧。
    - :sample 看的是**员工自己写的源码** —— 判断一段 curl 是外发密钥还是正常
      调接口，是工程判断。

    合成一个的话，审计员要么连样本源码一起看（多给了），要么连列表都打不开
    （少给了）。
    """
    from services.permissions import has_permission

    assert has_permission("auditor", "skill_security:view", "audit") is True
    assert has_permission("auditor", "skill_security:sample", "audit") is False
    assert has_permission("developer", "skill_security:sample", "rd") is True
    assert has_permission("president", "skill_security:sample", None) is True
    assert has_permission("employee", "skill_security:view", "rd") is False

    # 部门负责人暂时不给：这两个权限点还没有数据范围概念，给了就等于让他看
    # 全公司的事件，而用量报表那边同样是 manager 可见却限死在本部门。
    # 两边口径不一致时，宽的那边会赢。
    assert has_permission("manager", "skill_security:view", "rd") is False


def test_an_employee_cannot_open_the_list():
    response = TestClient(_admin_app(role="employee")).get("/admin/skill-events/list")

    assert response.json()["code"] != 0


def test_the_list_never_carries_source_code(monkeypatch):
    """列表一律不下发样本内容，哪怕调用方两个权限都有。

    一页四十条全带上，等于把几十个人的代码一次性推给任何打得开这个页面的人。
    要看代码走 /sample，于是「取代码」至少是一个单独的动作。
    """
    import db.selects.skill_report as selects
    import router.admin.skill_report as module

    row = {
        "id": 1, "event": "skill_quarantined", "user_uid": "u1", "dept_id": 1,
        "dept_code": "rd", "device": "box", "skill_name": "leaky", "verdict": "dangerous",
        "source": "hub", "scanner_version": "v1", "bundle_hash": "b" * 64,
        "finding_count": 2, "has_sample": True, "occurred_at": "", "created_at": None,
    }

    async def fake_fetch(**kwargs):
        return [row], 1

    monkeypatch.setattr(module, "fetch_events", fake_fetch)

    body = TestClient(_admin_app(role="developer")).get("/admin/skill-events/list").json()
    serialized = json.dumps(body, default=str)

    assert body["data"]["total"] == 1
    assert "files" not in serialized and "content" not in serialized

    # _summary 是唯一的列表行构造点。直接钉住它的字段集合 —— 上面那条只证明了
    # 「这一行假数据里没有代码」，将来有人往 _summary 里加一个 files_json，
    # 假数据不变，那条断言照样通过。
    class _Row:
        id = 1
        event = "skill_quarantined"
        user_uid = "u1"
        dept_id = 1
        dept_code = "rd"
        device = "box"
        skill_name = "leaky"
        verdict = "dangerous"
        reason = "扫描命中"
        source = "hub"
        origin_relpath = "ops/leaky"
        scanner_version = "v1"
        bundle_hash = "b" * 64
        finding_count = 2
        findings_json = '[{"rule": "exfil"}]'
        sample_id = 5
        occurred_at = ""
        reported_at = ""
        created_at = None

    keys = set(selects._summary(_Row()))
    assert "findings_json" not in keys
    assert "files_json" not in keys
    assert "reason" not in keys, "摘要里不该带扫描器原文，那是详情页的事"
    assert "has_sample" in keys


def test_an_auditor_can_list_but_cannot_read_the_sample(monkeypatch):
    """这是拆两个权限点的全部意义 —— 单独钉一条。"""
    import router.admin.skill_report as module

    async def fake_fetch_sample(sample_id):
        raise AssertionError("权限没过就不该查库")

    async def fake_fetch_events(**kwargs):
        return [], 0

    monkeypatch.setattr(module, "fetch_sample", fake_fetch_sample)
    monkeypatch.setattr(module, "fetch_events", fake_fetch_events)

    client = TestClient(_admin_app(role="auditor", dept_code="audit"))
    assert client.get("/admin/skill-events/list").json()["code"] == 0

    denied = client.get("/admin/skill-events/sample/1").json()
    assert denied["code"] != 0


def test_a_developer_can_read_the_sample(monkeypatch):
    import router.admin.skill_report as module

    class Row:
        id = 1
        sample_hash = "s" * 64
        bundle_hash = "b" * 64
        file_count = 1
        included_count = 1
        included_bytes = 12
        truncated = True
        truncated_reason = "total_bytes"
        files_json = json.dumps([{"path": "SKILL.md", "content": "# bad\n"}])
        seen_count = 3
        first_seen_at = None
        last_seen_at = None

    async def fake_fetch_sample(sample_id):
        return Row()

    monkeypatch.setattr(module, "fetch_sample", fake_fetch_sample)

    body = TestClient(_admin_app(role="developer")).get("/admin/skill-events/sample/1").json()

    assert body["code"] == 0
    assert body["data"]["files"][0]["content"] == "# bad\n"


def test_the_sample_response_says_when_it_is_incomplete(monkeypatch):
    """截断标记必须下发。

    缺了它，读到一份看着人畜无害的样本就会得出「误报」的结论，而真正的东西在
    那个因为超限没传上来的 12MB 文件里。
    """
    import router.admin.skill_report as module

    class Row:
        id = 1
        sample_hash = "s" * 64
        bundle_hash = ""
        file_count = 9
        included_count = 2
        included_bytes = 40
        truncated = True
        truncated_reason = "total_bytes"
        files_json = "[]"
        seen_count = 1
        first_seen_at = None
        last_seen_at = None

    async def fake_fetch_sample(sample_id):
        return Row()

    monkeypatch.setattr(module, "fetch_sample", fake_fetch_sample)

    data = TestClient(_admin_app(role="developer")).get(
        "/admin/skill-events/sample/1"
    ).json()["data"]

    assert data["truncated"] is True
    assert data["truncated_reason"] == "total_bytes"
    assert data["file_count"] == 9 and data["included_count"] == 2


def _compiled(stmt) -> str:
    from sqlalchemy.dialects import mysql

    return str(stmt.compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True}))


def test_the_list_sorts_by_server_time_not_client_time():
    """按 created_at 排，不按 occurred_at。

    occurred_at 是客户端上报的字符串。一台机器时间被改过（或者时区没设对）
    就会排到天边去 —— 而排序的全部用途就是「按时间看事件」。改过时间的那台
    机器，恰恰是最值得排在你眼前的那台。
    """
    import db.selects.skill_report as selects

    sql = _compiled(selects.list_query([], page=1, page_size=20))
    # 只看 ORDER BY 那一段：occurred_at 当然会出现在 SELECT 的列清单里。
    order_by = sql.split("ORDER BY", 1)[1]

    assert order_by.strip().startswith("skill_security_events.created_at DESC")
    assert "occurred_at" not in order_by


def test_paging_has_a_tiebreaker_so_rows_cannot_hop_between_pages():
    """同一秒内的多条如果只按 created_at 排，翻页时顺序是不定的，
    第 2 页会漏掉第 1 页尾巴上的行 —— 而漏掉的正好是「一次批量隔离」里的几条。"""
    import db.selects.skill_report as selects

    sql = _compiled(selects.list_query([], page=1, page_size=20))

    assert "skill_security_events.id DESC" in sql


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"event": "skill_quarantine_restored"}, "skill_security_events.event ="),
        ({"verdict": "dangerous"}, "skill_security_events.verdict ="),
        ({"dept_id": 7}, "skill_security_events.dept_id ="),
        ({"user_uid": "u1"}, "skill_security_events.user_uid ="),
        ({"skill_name": "leaky"}, "skill_security_events.skill_name LIKE"),
    ],
)
def test_each_filter_actually_reaches_the_query(kwargs, expected):
    """筛选条件写了但没接进 WHERE，表现是**筛不筛都返回全部** ——
    看上去像「这个部门真的出了这么多事」。"""
    import db.selects.skill_report as selects

    sql = _compiled(selects.list_query(selects.list_conditions(**kwargs), page=1, page_size=20))

    assert expected in sql


def test_no_filter_means_no_where_clause():
    """基线：不传筛选时不该凭空多出条件。否则上面那些用例可能只是撞上了
    一个恒真的 WHERE。"""
    import db.selects.skill_report as selects

    sql = _compiled(selects.list_query(selects.list_conditions(), page=1, page_size=20))

    assert "WHERE" not in sql


def test_the_frontend_permission_mirror_did_not_drift():
    """前端 constants/roles.ts 是 permissions.py 的镜像，两份手写的东西会漂。

    漂了的表现分两种，都不报错：
    - 前端少一个点 → 有权限的人看不见入口，以为功能没做
    - 前端多给一个角色 → 菜单出现、点进去接口 403，像个 bug 而不像权限设计

    真正的拦截一直在服务端（这一点没变），这条只是让漂移在 CI 里就现形。
    """
    import os
    import re

    from services.permissions import PERMISSIONS

    path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "..", "client", "src", "constants", "roles.ts",
    )
    source = open(path, encoding="utf-8").read()
    block = source.split("export const PERMISSIONS = {")[1].split("} as const")[0]

    mirrored = {}
    for name, roles in re.findall(r"'([\w:]+)':\s*\[([^\]]*)\]", block):
        mirrored[name] = {
            r.strip().split(".")[-1].lower() for r in roles.split(",") if r.strip()
        }

    backend = {name: {r.lower() for r in roles} for name, roles in PERMISSIONS.items()}

    assert set(mirrored) == set(backend), "权限点集合两边不一致"
    for name in backend:
        assert mirrored[name] == backend[name], f"{name} 的角色名单两边不一致"


# ── 部署配置 ──────────────────────────────────────────────────────────────

def test_env_example_covers_every_setting():
    """.env.example 是部署时唯一的照抄对象，漏一项就是「上线之后才发现不能用」。

    这次真漏了一项：UPDATE_PUBLIC_KEY 在 config.py 里有、示例里没有。照着示例
    部署的人不会配它，而现象是**版本发不上去**（发布接口拒绝入库），
    离「示例文件少了一行」隔了好几层。
    """
    import os

    from config import Settings

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env.example")
    example = open(path, encoding="utf-8").read()
    documented = {
        line.split("=", 1)[0].strip()
        for line in example.splitlines()
        if "=" in line and not line.strip().startswith("#")
    }

    # 这些是 pydantic 内部字段或纯派生属性，不该出现在 .env 里
    missing = {
        name.upper() for name in Settings.model_fields
    } - documented

    assert not missing, f".env.example 缺少配置项：{sorted(missing)}"


def test_publish_warning_is_persisted_and_available_to_authorized_review(tmp_path, monkeypatch):
    """Exercise endpoint, real inserts/selects, dedup and sample permissions."""
    import asyncio
    from db.inserts import skill_report as inserts
    from db.selects import skill_report as selects
    from model.database import SkillSecurityEvent, SkillSecuritySample
    from sqlalchemy import Integer
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path / "events.sqlite"}', poolclass=NullPool)
    # SQLite autoincrement requires INTEGER; production keeps its MySQL BIGINT.
    for model in (SkillSecurityEvent, SkillSecuritySample):
        monkeypatch.setattr(model.__table__.c.id, 'type', Integer())

    async def create_tables():
        async with engine.begin() as connection:
            for model in (SkillSecuritySample, SkillSecurityEvent):
                await connection.run_sync(model.__table__.create)

    asyncio.run(create_tables())
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def session_maker():
        return maker

    monkeypatch.setattr(inserts, 'init_async_session', session_maker)
    monkeypatch.setattr(selects, 'init_async_session', session_maker)
    payload = _quarantine_payload(event='skill_publish_warning', published_at='2026-10-14T01:00:00Z')
    try:
        with TestClient(_app()) as client:
            response = client.post('/v1/skill-reports', json=payload)
            assert response.status_code == 202 and response.json()['stored']
            repeated = client.post('/v1/skill-reports', json=payload)
            assert repeated.status_code == 202 and repeated.json()['duplicate']
        with TestClient(_admin_app(role='president')) as admin:
            data = admin.get('/admin/skill-events/list', params={'event': 'skill_publish_warning'}).json()['data']
            assert data['total'] == 1
            row = data['items'][0]
            assert row['has_sample'] and row['user_uid'] == 'real-uid'
            detail = admin.get(f'/admin/skill-events/detail/{row["id"]}').json()['data']
            sample = admin.get(f'/admin/skill-events/sample/{detail["sample_id"]}').json()['data']
            assert sample['bundle_hash'] == payload['bundle_hash']
            assert sample['files'][0]['content'] == payload['sample']['files'][0]['content']
        with TestClient(_admin_app(role='auditor', dept_code='audit')) as auditor:
            assert auditor.get(f'/admin/skill-events/sample/{detail["sample_id"]}').json()['code'] != 0
    finally:
        asyncio.run(engine.dispose())
