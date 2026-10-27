"""MCP 安全边界的纯单元测试，不访问数据库或真实领星账号。"""

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from mcp_adapters import get_adapter, list_adapters
from mcp_adapters.lingxing import make_signature
from model.database import (McpCapability, McpInstance, McpUserPermission)
from model.request.mcp import McpExecuteRequest, McpInstanceUpdate
from router.admin.mcp import _call_log_payload
from router.v1.mcp import (_capability_payload, _category_payloads,
                           execute as execute_route, search)
from services.mcp import (CatalogEntry, enforce_constraints,
                          validate_arguments)
from utils.gateway_auth import Caller
from utils.errors import (McpUpstreamError, PermissionError_,
                          ValidationError_)


def test_lingxing_manifest_is_read_only_unique_and_curated():
    capabilities = get_adapter("lingxing").capabilities()
    codes = [item.code for item in capabilities]
    assert len(capabilities) == 443
    assert len(codes) == len(set(codes))
    assert not any(code.startswith("lx..") for code in codes)
    assert {item.method for item in capabilities} <= {"GET", "POST"}
    assert "lx.basic.seller.list" in codes
    assert "lx.sale.order.list" in codes


def test_registry_exposes_dynamic_configuration_schema():
    adapters = {item["key"]: item for item in list_adapters()}
    lingxing = adapters["lingxing"]
    assert lingxing["key"] == "lingxing"
    assert lingxing["capability_count"] == 443
    assert {field["key"] for field in lingxing["config_schema"]} == {
        "app_id", "app_secret", "proxy_url", "timeout_seconds"
    }


def test_registry_exposes_permission_mode_and_credential_scope():
    adapters = {item["key"]: item for item in list_adapters()}

    assert adapters["lingxing"]["permission_mode"] == "local_grants"
    assert adapters["lingxing"]["credential_platform"] is None
    assert adapters["internal_datahub"]["permission_mode"] == "upstream_account"
    assert adapters["internal_datahub"]["credential_platform"] == "internal_datahub"


def test_arguments_reject_unknown_and_missing_fields():
    cap = next(item for item in get_adapter("lingxing").capabilities()
               if item.code == "lx.sale.order.list")
    with pytest.raises(ValidationError_, match="不接受参数"):
        validate_arguments(cap, {"start_date": "2026-01-01", "end_date": "2026-01-02", "evil": 1})
    with pytest.raises(ValidationError_, match="缺少参数"):
        validate_arguments(cap, {})


def test_permission_constraints_reject_out_of_scope_values():
    enforce_constraints({"sid_list": [113], "length": 50}, {
        "allowed_sids": [113, 114], "max_page_size": 100,
    })
    with pytest.raises(PermissionError_, match="超出管理员授权范围"):
        enforce_constraints({"sid": 999}, {"allowed_sids": [113]})
    with pytest.raises(PermissionError_, match="超过授权上限"):
        enforce_constraints({"length": 101}, {"max_page_size": 100})
    with pytest.raises(PermissionError_, match="时间范围超过授权上限"):
        enforce_constraints({"start_date": "2026-01-01", "end_date": "2026-02-01"},
                            {"max_days": 7})


def test_signature_is_deterministic_and_sensitive_to_nested_arguments():
    params = {
        "access_token": "token", "app_key": "1234567890abcdef",
        "timestamp": "1720429074", "content": {"city": "lake", "age": "133"},
    }
    signature = make_signature(params, "1234567890abcdef")
    assert signature == "xDYMr4QfJVdj2JoFjfVO4X2A9bqV00Q8nZjfQ4a2d14C3sVlLAIV+Tr2nwCfD21Y"
    assert make_signature({**params, "content": {"city": "sea"}}, "1234567890abcdef") != signature


def test_generated_manifest_matches_checked_in_source_count():
    root = Path(__file__).resolve().parents[3]
    audited = json.loads((root / "artifacts/lingxing-readonly-api/included.json").read_text())
    assert len(audited) == len(get_adapter("lingxing").capabilities())


def test_call_log_payload_uses_username_and_readable_time():
    row = SimpleNamespace(
        id=1, request_id="req-1", user_uid="uid-1",
        capability_code="lx.basic.seller.list", status="success",
        error_code=None, latency_ms=42, upstream_request_id=None,
        response_size=128, created_at=datetime(2026, 8, 13, 15, 23, 15),
    )
    payload = _call_log_payload(row, SimpleNamespace(username="alice"))

    assert payload["username"] == "alice"
    assert payload["user_uid"] == "uid-1"
    assert payload["created_at"] == "2026-08-13 15:23:15"


def test_call_log_payload_falls_back_to_uid_for_deleted_user():
    row = SimpleNamespace(
        id=1, request_id="req-1", user_uid="uid-1",
        capability_code="lx.basic.seller.list", status="success",
        error_code=None, latency_ms=42, upstream_request_id=None,
        response_size=128, created_at=None,
    )

    assert _call_log_payload(row, None)["username"] == "uid-1"


def test_instance_update_allows_name_but_rejects_stable_identifiers():
    assert McpInstanceUpdate(name="华南领星").model_dump(exclude_unset=True) == {
        "name": "华南领星",
    }
    with pytest.raises(ValidationError):
        McpInstanceUpdate.model_validate({"name": "华南领星", "code": "new-code"})
    with pytest.raises(ValidationError):
        McpInstanceUpdate.model_validate({"name": "华南领星", "id": 2})


def test_catalog_only_exposes_schema_for_available_capabilities():
    instance = SimpleNamespace(code="lingxing-main", status=1)
    capability = SimpleNamespace(
        capability_code="lx.basic.seller.list", name="查询店铺列表",
        domain="基础数据", description="文档", status=1,
        input_schema_json='{"type":"object"}',
    )
    allowed = _capability_payload(CatalogEntry(instance, capability, "available"))
    denied = _capability_payload(CatalogEntry(instance, capability, "request_access"))

    assert allowed["access_status"] == "available"
    assert allowed["input_schema"] == {"type": "object"}
    assert denied["access_status"] == "request_access"
    assert "联系负责人" in denied["access_message"]
    assert "input_schema" not in denied


def test_upstream_account_catalog_prompts_for_account_setup_not_admin_grant():
    instance = SimpleNamespace(code="datahub-main", status=1)
    capability = SimpleNamespace(
        capability_code="datahub.dcms.brand.list", name="查询品牌列表",
        domain="数据资产", description=None, status=1,
        input_schema_json='{"type":"object"}',
    )

    payload = _capability_payload(CatalogEntry(instance, capability, "setup_required"))

    assert payload["access_status"] == "setup_required"
    assert payload["access_message"] == "请先绑定并测试自己的中台账号"
    assert "联系负责人" not in payload["access_message"]
    assert "input_schema" not in payload


def test_catalog_category_level_counts_access_states():
    instance = SimpleNamespace(code="lingxing-main", status=1)
    capability = SimpleNamespace(domain="基础数据", status=1)
    rows = [
        CatalogEntry(instance, capability, "available"),
        CatalogEntry(instance, capability, "request_access"),
    ]

    assert _category_payloads(rows, 20) == [{
        "instance": "lingxing-main", "domain": "基础数据", "total": 2,
        "available": 1, "request_access": 1, "expired": 0, "unavailable": 0,
        "setup_required": 0,
        "drilldown": {"instance_code": "lingxing-main", "domain": "基础数据"},
    }]


async def test_search_uses_categories_first_and_domain_for_drilldown(monkeypatch):
    instance = SimpleNamespace(code="lingxing-main", status=1)
    capability = SimpleNamespace(
        capability_code="lx.basic.seller.list", name="查询店铺列表",
        domain="基础数据", description="文档", status=1,
        input_schema_json='{"type":"object"}',
    )
    async def catalog(*_args):
        return [CatalogEntry(instance, capability, "available")]

    monkeypatch.setattr("router.v1.mcp.service.search_catalog", catalog)
    caller = Caller(
        uid="u1", username="tester", role="employee", dept_id=1, dept_code="dev",
    )

    # 目录搜索这轮多了一个 request 形参 —— 会话作用域闸门要读
    # X-Llano-Conversation-Scope（决策 #45）。这里给的是私聊（没有那个头），
    # 所以下面的断言测的仍然是原来那件事：目录先给分类、下钻才给能力。
    # 群聊下目录为空由 test_conversation_scope.py 单独钉。
    private = SimpleNamespace(headers={})

    categories = await search(SimpleNamespace(
        query=None, instance_code=None, domain=None, limit=20,
    ), private, caller)
    capabilities = await search(SimpleNamespace(
        query=None, instance_code="lingxing-main", domain="基础数据", limit=20,
    ), private, caller)

    assert categories["level"] == "categories"
    assert categories["data"][0]["drilldown"] == {
        "instance_code": "lingxing-main", "domain": "基础数据",
    }
    assert capabilities["level"] == "capabilities"
    assert capabilities["data"][0]["access_status"] == "available"


def _execute_request() -> Request:
    request = Request({
        "type": "http", "method": "POST", "path": "/v1/mcp/execute",
        "headers": [], "client": ("10.0.0.9", 1234), "query_string": b"",
        "server": ("test", 80), "scheme": "http",
    })
    request.state.request_id = "req-upstream"
    return request


async def test_execute_preserves_typed_upstream_timeout_and_failed_audit(monkeypatch):
    trusted = get_adapter("internal_datahub").capabilities()[0]
    instance = SimpleNamespace(
        id=7, adapter_key="internal_datahub", config_json="{}",
    )
    db_capability = SimpleNamespace(id=11)
    resolved = SimpleNamespace(
        instance=instance,
        db_capability=db_capability,
        trusted=trusted,
        auth={"account": "alice", "password": "private-password"},
        constraints={},
    )
    adapter = SimpleNamespace(execute=AsyncMock(side_effect=McpUpstreamError(
        "datahub_timeout", "中台请求超时，请稍后重试", http_status=504,
    )))
    record_call = AsyncMock()
    monkeypatch.setattr("router.v1.mcp.service.resolve_capability", AsyncMock(return_value=resolved))
    monkeypatch.setattr("router.v1.mcp.service.record_call", record_call)
    monkeypatch.setattr("router.v1.mcp.get_adapter", lambda _key: adapter)
    caller = Caller(
        uid="user-a", username="alice", role="employee", dept_id=1,
        dept_code="ops",
    )

    response = await execute_route(
        McpExecuteRequest(
            capability_code=trusted.code,
            instance_code="datahub-main",
            arguments={},
        ),
        _execute_request(),
        caller,
    )

    assert response.status_code == 504
    payload = json.loads(response.body)
    assert payload["error"] == {
        "message": "中台请求超时，请稍后重试",
        "type": "mcp_upstream_error",
        "code": "datahub_timeout",
    }
    assert "private-password" not in response.body.decode()
    assert record_call.await_args.kwargs["status"] == "failed"
    assert record_call.await_args.kwargs["error_code"] == "datahub_timeout"


class _QueryResult:
    def __init__(self, *, rows=None, scalars=None):
        self._rows = rows or []
        self._scalars = scalars or []

    def all(self):
        return self._rows

    def scalars(self):
        return self._scalars


class _QuerySession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.statements = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, statement):
        self.statements.append(str(statement))
        if not self.responses:
            raise AssertionError("unexpected additional database query")
        return self.responses.pop(0)


async def _query_session_factory(session):
    return lambda: session


def _datahub_db_rows():
    trusted = get_adapter("internal_datahub").capabilities()[0]
    instance = McpInstance(
        id=7, code="datahub-main", name="中台", adapter_key="internal_datahub",
        config_json="{}", status=1,
    )
    capability = McpCapability(
        id=11, instance_id=7, capability_code=trusted.code,
        name=trusted.name, domain=trusted.domain, method=trusted.method,
        path=trusted.path, input_schema_json=json.dumps(trusted.input_schema),
        metadata_json=json.dumps(trusted.metadata), source_hash=trusted.source_hash,
        status=1,
    )
    return trusted, instance, capability


async def test_resolve_upstream_account_uses_callers_credential_without_permission_query(
    monkeypatch,
):
    from services import mcp as service

    trusted, instance, capability = _datahub_db_rows()
    session = _QuerySession([_QueryResult(rows=[(instance, capability)])])
    load = AsyncMock(return_value={
        "account": "alice", "password": "private-password",
    })
    monkeypatch.setattr(
        service, "init_async_session", lambda: _query_session_factory(session)
    )
    monkeypatch.setattr(service.mcp_credentials, "load_for_execute", load)

    resolved = await service.resolve_capability(
        "user-a", trusted.code, "datahub-main", "req-resolve"
    )

    assert resolved.auth == {
        "account": "alice", "password": "private-password",
    }
    assert resolved.constraints == {}
    load.assert_awaited_once_with("user-a", instance, "req-resolve")
    assert len(session.statements) == 1
    assert "mcp_user_permissions" not in session.statements[0]


@pytest.mark.parametrize(
    ("verified_platforms", "expected_status"),
    [(["internal_datahub"], "available"), ([], "setup_required")],
)
async def test_search_upstream_account_uses_verified_owner_credential_not_grants(
    monkeypatch, verified_platforms, expected_status
):
    from services import mcp as service

    _trusted, instance, capability = _datahub_db_rows()
    session = _QuerySession([
        _QueryResult(rows=[(instance, capability)]),
        _QueryResult(scalars=verified_platforms),
    ])
    monkeypatch.setattr(
        service, "init_async_session", lambda: _query_session_factory(session)
    )

    [entry] = await service.search_catalog(
        "user-a", None, "datahub-main", "业务分析报表", 20
    )

    assert entry.access_status == expected_status
    assert all("mcp_user_permissions" not in statement for statement in session.statements)


class _PermissionMutationSession:
    def __init__(self, instance, capability):
        self.instance = instance
        self.capability = capability
        self.added = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, _model, _instance_id):
        return self.instance

    async def scalar(self, _statement):
        return 1

    async def execute(self, _statement):
        return _QueryResult(scalars=[self.capability])

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        return None


async def test_upstream_account_instances_reject_local_permission_mutations(
    monkeypatch,
):
    from services import mcp as service

    trusted, instance, capability = _datahub_db_rows()
    session = _PermissionMutationSession(instance, capability)
    monkeypatch.setattr(
        service, "init_async_session", lambda: _query_session_factory(session)
    )

    with pytest.raises(ValidationError_, match="不使用本地能力授权"):
        await service.replace_permissions(
            "user-a",
            instance.id,
            [trusted.code],
            "allow",
            {},
            None,
            "admin-a",
        )

    assert session.added == []


async def test_local_grant_resolution_ignores_ungranted_duplicate_instances(
    monkeypatch,
):
    from services import mcp as service

    trusted = next(
        item for item in get_adapter("lingxing").capabilities()
        if item.code == "lx.basic.seller.list"
    )
    instance_a = McpInstance(
        id=21, code="lingxing-a", name="领星 A", adapter_key="lingxing",
        auth_config_encrypted="encrypted-a", config_json="{}", status=1,
    )
    instance_b = McpInstance(
        id=22, code="lingxing-b", name="领星 B", adapter_key="lingxing",
        auth_config_encrypted="encrypted-b", config_json="{}", status=1,
    )
    capability_a = McpCapability(
        id=31, instance_id=21, capability_code=trusted.code,
        name=trusted.name, domain=trusted.domain, method=trusted.method,
        path=trusted.path, input_schema_json=json.dumps(trusted.input_schema),
        metadata_json=json.dumps(trusted.metadata), source_hash=trusted.source_hash,
        status=1,
    )
    capability_b = McpCapability(
        id=32, instance_id=22, capability_code=trusted.code,
        name=trusted.name, domain=trusted.domain, method=trusted.method,
        path=trusted.path, input_schema_json=json.dumps(trusted.input_schema),
        metadata_json=json.dumps(trusted.metadata), source_hash=trusted.source_hash,
        status=1,
    )
    permission = McpUserPermission(
        user_uid="user-a", instance_id=21, capability_id=31,
        effect="allow", constraints_json='{"allowed_sids":[113]}',
        status=1, granted_by="admin-a",
    )
    session = _QuerySession([
        _QueryResult(rows=[
            (instance_a, capability_a),
            (instance_b, capability_b),
        ]),
        _QueryResult(scalars=[permission]),
    ])
    monkeypatch.setattr(
        service, "init_async_session", lambda: _query_session_factory(session)
    )
    monkeypatch.setattr(service, "decrypt_auth", lambda _instance: {"app_id": "a"})

    resolved = await service.resolve_capability("user-a", trusted.code)

    assert resolved.instance.id == instance_a.id
    assert resolved.auth == {"app_id": "a"}
    assert resolved.constraints == {"allowed_sids": [113]}
