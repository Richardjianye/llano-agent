import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from services.quota import (QUOTA_SCOPE_DEPT, QuotaStatus,
                            applicable_quota_statuses, first_blocking_quota,
                            quota_status_for_rule)
from utils.gateway_auth import Caller


class _FakeResult:
    def __init__(self, *, rules=(), scalar=0):
        self._rules = rules
        self._scalar = scalar

    def scalars(self):
        return iter(self._rules)

    def scalar_one(self):
        return self._scalar


class _FakeSession:
    def __init__(self, statements, *, rules=(), scalar=0):
        self._statements = statements
        self._rules = rules
        self._scalar = scalar

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, statement):
        self._statements.append(statement)
        return _FakeResult(rules=self._rules, scalar=self._scalar)


def fake_db(monkeypatch, *, rules=(), scalar=0):
    from services import quota

    statements = []

    async def init_session():
        return lambda: _FakeSession(statements, rules=rules, scalar=scalar)

    monkeypatch.setattr(quota, "init_async_session", init_session)
    return statements


def status(rule_id: int, used: int, limit: int) -> QuotaStatus:
    return QuotaStatus(rule_id, "user", "u1", limit, used)


def test_quota_allows_below_limit():
    assert first_blocking_quota([status(1, 99, 100)]) is None


def test_quota_blocks_at_limit():
    assert first_blocking_quota([status(1, 100, 100)]).rule_id == 1


def test_any_applicable_rule_can_block():
    blocked = first_blocking_quota([status(1, 10, 100), status(2, 200, 200)])
    assert blocked.rule_id == 2


def test_remaining_never_negative():
    quota = status(1, 120, 100)
    assert quota.remaining == 0
    assert quota.exceeded is True


async def test_a_child_request_matches_every_parent_department_quota(monkeypatch):
    from services import quota

    async def ancestor_ids(dept_id):
        assert dept_id == 30
        return (9, 20, 30)

    monkeypatch.setattr(quota, "dept_ancestor_ids", ancestor_ids, raising=False)
    statements = fake_db(monkeypatch)

    assert await applicable_quota_statuses("u-child", 30, 7) == []
    sql = str(statements[0].compile(compile_kwargs={"literal_binds": True}))

    assert "scope_value IN ('9', '20', '30')" in sql


async def test_a_parent_quota_sums_usage_from_the_whole_subtree(monkeypatch):
    from services import quota

    async def descendant_ids(dept_id):
        assert dept_id == 9
        return (9, 20, 21, 30)

    monkeypatch.setattr(quota, "dept_scope_ids", descendant_ids, raising=False)
    statements = fake_db(monkeypatch, scalar=180)
    rule = SimpleNamespace(
        id=8,
        scope=QUOTA_SCOPE_DEPT,
        scope_value="9",
        model_id=7,
        daily_token_limit=200,
    )

    quota = await quota_status_for_rule(rule)
    sql = str(statements[0].compile(compile_kwargs={"literal_binds": True}))

    assert quota.used == 180
    assert quota.remaining == 20
    assert "dept_id IN (9, 20, 21, 30)" in sql


async def test_chat_rejects_exhausted_model_before_upstream(monkeypatch):
    from router.v1 import chat

    class Request:
        state = SimpleNamespace(request_id="quota-test")

        async def json(self):
            return {
                "model": "expensive-model",
                "messages": [{"role": "user", "content": "hello"}],
            }

    async def allowed_model(_subject, _model_ref):
        return SimpleNamespace(id=9, alias="expensive-model", model_name="upstream")

    async def exhausted(_uid, _dept_id, _model_id):
        return [status(7, 100, 100)]

    monkeypatch.setattr(chat, "check_model_permission", allowed_model)
    monkeypatch.setattr(chat, "applicable_quota_statuses", exhausted)

    response = await chat.chat_completions(
        Request(),
        Caller(uid="u1", username="tester", role="employee", dept_id=1, dept_code="dev"),
    )
    body = json.loads(response.body)
    assert response.status_code == 429
    assert body["error"]["code"] == "quota_exceeded"
    assert "手动切换" in body["error"]["message"]


async def test_chat_rejects_exhausted_api_key_before_model_lookup(monkeypatch):
    from router.v1 import chat

    class Request:
        state = SimpleNamespace(request_id="key-quota-test")

        async def json(self):
            return {
                "model": "any-model",
                "messages": [{"role": "user", "content": "hello"}],
            }

    model_lookup = AsyncMock()
    monkeypatch.setattr(chat, "check_model_permission", model_lookup)
    monkeypatch.setattr(chat, "api_key_used_tokens", AsyncMock(return_value=100))

    response = await chat.chat_completions(
        Request(),
        Caller(
            uid="u1", username="tester", role="employee", dept_id=1,
            dept_code="dev", auth_type="apikey", api_key_id=7,
            api_key_token_limit=100,
        ),
    )
    body = json.loads(response.body)
    assert response.status_code == 429
    assert body["error"]["code"] == "api_key_quota_exceeded"
    model_lookup.assert_not_awaited()
