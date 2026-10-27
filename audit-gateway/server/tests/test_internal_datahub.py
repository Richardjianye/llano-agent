"""自有中台适配器的受信任只读目录与 HTTP 边界测试；不访问真实中台。"""

import base64
import json

import httpx
import pytest

from mcp_adapters.internal_datahub import InternalDatahubAdapter
from mcp_adapters import get_adapter
from utils.errors import McpUpstreamError, PermissionError_


def _login_success(
    token: str = "token-for-tests", expires_in: int | None = None
) -> dict:
    data = {
        "Token": token,
        "IsNeedImgCode": False,
        "IsNeedSecondFactor": False,
        "IsUpdatePassword": False,
    }
    if expires_in is not None:
        data["ExpiresIn"] = expires_in
    return {
        "IsSuccess": True,
        "data": data,
    }


def _adapter_with(handler) -> InternalDatahubAdapter:
    return InternalDatahubAdapter(transport=httpx.MockTransport(handler))


def _credentials() -> dict[str, str]:
    return {"account": "employee@example.com", "password": "secret-value"}


def test_manifest_exposes_only_the_two_verified_read_only_capabilities():
    capabilities = get_adapter("internal_datahub").capabilities()

    assert [item.code for item in capabilities] == [
        "datahub.report.platform.list",
        "datahub.dcms.brand.list",
    ]
    assert [item.path for item in capabilities] == [
        "/api/Platform/GetList",
        "/api/Brand/GetBrandList",
    ]
    assert {item.method for item in capabilities} == {"POST"}
    assert all(item.metadata["read_only"] is True for item in capabilities)
    assert all(
        item.input_schema
        == {"type": "object", "properties": {}, "additionalProperties": False}
        for item in capabilities
    )


def test_manifest_hash_changes_when_a_trusted_request_definition_changes():
    adapter = get_adapter("internal_datahub")
    [report, dcms] = adapter.capabilities()

    assert report.source_hash != dcms.source_hash
    assert len(report.source_hash) == 64
    assert len(dcms.source_hash) == 64


def test_instance_configuration_contains_no_shared_account_secret_fields():
    schema = get_adapter("internal_datahub").config_schema()

    assert schema == [{
        "key": "timeout_seconds",
        "label": "请求超时（秒）",
        "type": "number",
        "required": False,
        "secret": False,
        "default": 30,
        "min": 3,
        "max": 120,
    }]


async def test_health_posts_the_vendor_login_shape_without_exposing_the_token():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_login_success("private-token"))

    result = await _adapter_with(handler).health(_credentials(), {})

    assert result["ok"] is True
    assert isinstance(result["latency_ms"], int)
    assert "private-token" not in json.dumps(result)
    [request] = seen
    assert str(request.url) == "http://auth.example.invalid/sso/SSOLogin"
    assert request.method == "POST"
    payload = json.loads(request.content)
    assert payload == {
        "Phone": "employee@example.com",
        "Password": base64.b64encode(b"secret-value").decode("ascii"),
        "LoginType": "password",
        "ClientIp": "",
        "AppId": "00000000-0000-4000-8000-000000000001",
        "RedirectUrl": "http://portal.example.invalid/home",
        "RequestCid": payload["RequestCid"],
        "IsReturnSubordinate": True,
        "Ati": "llano-gateway",
        "Eid": "llano-gateway",
        "ImgCodeId": "",
        "Token": "",
    }
    assert len(payload["RequestCid"]) == 8
    assert payload["RequestCid"].isdigit()


@pytest.mark.parametrize(
    ("code", "expected_path", "expected_app_id", "expected_system_type"),
    [
        ("datahub.report.platform.list", "/llano/api/Platform/GetList", "app_example_report", "REPORT"),
        ("datahub.dcms.brand.list", "/llano/api/Brand/GetBrandList", "app_example_dcms", "DCMS"),
    ],
)
async def test_execute_posts_only_trusted_paths_and_headers(
    code, expected_path, expected_app_id, expected_system_type
):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "auth.example.invalid":
            return httpx.Response(200, json=_login_success())
        return httpx.Response(200, json={"code": "0000", "detail": [{"id": 1}]})

    adapter = _adapter_with(handler)
    capability = next(item for item in adapter.capabilities() if item.code == code)
    result = await adapter.execute(capability, {}, _credentials(), {})

    assert result == {"code": "0000", "detail": [{"id": 1}]}
    business_request = seen[-1]
    assert business_request.url.host == "data.example.invalid"
    assert business_request.url.path == expected_path
    assert json.loads(business_request.content) == {}
    assert business_request.headers["token"] == "token-for-tests"
    assert business_request.headers["appid"] == expected_app_id
    assert business_request.headers["systemtype"] == expected_system_type
    assert len(business_request.headers["requestcid"]) == 8
    assert business_request.headers["ip"] == ""


async def test_execute_reuses_token_but_health_forces_fresh_verification():
    login_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal login_count
        if request.url.host == "auth.example.invalid":
            login_count += 1
            return httpx.Response(200, json=_login_success(f"token-{login_count}"))
        return httpx.Response(200, json={"code": "0000", "detail": []})

    adapter = _adapter_with(handler)
    capability = adapter.capabilities()[0]

    await adapter.execute(capability, {}, _credentials(), {})
    await adapter.execute(capability, {}, _credentials(), {})
    assert login_count == 1

    await adapter.health(_credentials(), {})
    assert login_count == 2


async def test_short_upstream_token_lifetime_is_never_extended_by_cache():
    login_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal login_count
        if request.url.host == "auth.example.invalid":
            login_count += 1
            return httpx.Response(
                200,
                json=_login_success(f"token-{login_count}", expires_in=30),
            )
        return httpx.Response(200, json={"code": "0000", "detail": []})

    adapter = _adapter_with(handler)
    capability = adapter.capabilities()[0]

    await adapter.execute(capability, {}, _credentials(), {})
    await adapter.execute(capability, {}, _credentials(), {})

    assert login_count == 2


@pytest.mark.parametrize(
    ("upstream_payload", "expected_code"),
    [
        ({"IsSuccess": True, "data": {"IsNeedImgCode": True}}, "datahub_interactive_verification_required"),
        ({"IsSuccess": True, "data": {"IsNeedSecondFactor": True, "Fid": "fid-1"}}, "datahub_interactive_verification_required"),
        ({"IsSuccess": False, "msg": "employee@example.com secret-value invalid"}, "datahub_auth_failed"),
    ],
)
async def test_login_stops_on_interactive_or_rejected_auth_without_leaking_details(
    upstream_payload, expected_code
):
    request_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, json=upstream_payload)

    adapter = _adapter_with(handler)
    with pytest.raises(McpUpstreamError) as caught:
        await adapter.execute(adapter.capabilities()[0], {}, _credentials(), {})

    assert caught.value.error_code == expected_code
    assert caught.value.http_status == 502
    assert "employee@example.com" not in caught.value.message
    assert "secret-value" not in caught.value.message
    assert request_count == 1


@pytest.mark.parametrize(
    ("network_error", "expected_code", "expected_status"),
    [
        (httpx.ReadTimeout("slow upstream"), "datahub_timeout", 504),
        (httpx.ConnectError("offline"), "datahub_connect_failed", 502),
    ],
)
async def test_network_failures_return_typed_safe_feedback(
    network_error, expected_code, expected_status
):
    def handler(request: httpx.Request) -> httpx.Response:
        raise network_error

    with pytest.raises(McpUpstreamError) as caught:
        await _adapter_with(handler).health(_credentials(), {})

    assert caught.value.error_code == expected_code
    assert caught.value.http_status == expected_status
    assert "slow upstream" not in caught.value.message
    assert "offline" not in caught.value.message


async def test_business_rejection_is_typed_and_does_not_return_raw_upstream_message():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.example.invalid":
            return httpx.Response(200, json=_login_success())
        return httpx.Response(200, json={
            "code": "SO011",
            "msg": "employee@example.com has no access with secret-value",
        })

    adapter = _adapter_with(handler)
    with pytest.raises(McpUpstreamError) as caught:
        await adapter.execute(adapter.capabilities()[0], {}, _credentials(), {})

    assert caught.value.error_code == "datahub_upstream_rejected"
    assert caught.value.http_status == 502
    assert caught.value.message == "中台拒绝了本次只读请求，请检查账号权限或稍后重试"


@pytest.mark.parametrize(
    ("response", "expected_code"),
    [
        (httpx.Response(503, json={"message": "maintenance"}), "datahub_upstream_http_error"),
        (httpx.Response(200, content=b"<html>not json</html>"), "datahub_invalid_response"),
        (httpx.Response(200, json=["unexpected", "array"]), "datahub_invalid_response"),
    ],
)
async def test_http_and_malformed_responses_are_safely_classified(
    response, expected_code
):
    def handler(_request: httpx.Request) -> httpx.Response:
        return response

    with pytest.raises(McpUpstreamError) as caught:
        await _adapter_with(handler).health(_credentials(), {})

    assert caught.value.error_code == expected_code
    assert "maintenance" not in caught.value.message
    assert "not json" not in caught.value.message


async def test_login_stops_when_the_vendor_requires_a_password_update():
    payload = {
        "IsSuccess": True,
        "data": {"IsUpdatePassword": True, "Token": "must-not-be-used"},
    }

    with pytest.raises(McpUpstreamError) as caught:
        await _adapter_with(
            lambda _request: httpx.Response(200, json=payload)
        ).health(_credentials(), {})

    assert caught.value.error_code == "datahub_password_update_required"
    assert caught.value.message == "中台要求更新密码，请先在中台完成密码更新"


async def test_execute_rejects_a_capability_object_not_owned_by_the_adapter():
    adapter = _adapter_with(lambda _request: httpx.Response(500))
    trusted = adapter.capabilities()[0]
    forged = type(trusted)(
        code=trusted.code,
        name=trusted.name,
        domain=trusted.domain,
        method=trusted.method,
        path="/api/Admin/DeleteEverything",
        input_schema=trusted.input_schema,
        source_hash=trusted.source_hash,
        metadata=trusted.metadata,
    )

    with pytest.raises(PermissionError_, match="受信任"):
        await adapter.execute(forged, {}, _credentials(), {})
