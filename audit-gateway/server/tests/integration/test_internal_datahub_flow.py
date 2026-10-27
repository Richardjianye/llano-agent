"""每用户中台凭据到 MCP 只读执行的真实数据库纵向闭环。"""

import json

import httpx
from fastapi import APIRouter, FastAPI
from sqlalchemy import func, select

from mcp_adapters.internal_datahub import InternalDatahubAdapter
from model.database import (CredentialAccessLog, McpCallLog, McpInstance,
                            McpUserPermission, PlatformCredential, User)
from router.v1.mcp import router as mcp_router
from router.v1.mcp_credentials import router as credential_router
from services import mcp as mcp_service
from utils.gateway_auth import Caller, require_caller
from utils.middleware import RequestContextMiddleware, register_exception_handlers


def _login_payload(token: str) -> dict:
    return {
        "IsSuccess": True,
        "data": {
            "Token": token,
            "IsNeedImgCode": False,
            "IsNeedSecondFactor": False,
            "IsUpdatePassword": False,
        },
    }


async def test_per_user_credentials_remain_isolated_across_search_and_execute(
    mysql_session_factory, monkeypatch
):
    caller = {"value": None}
    state = {"timeout_business": False, "business_tokens": []}

    def upstream(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.example.invalid":
            phone = json.loads(request.content)["Phone"]
            token = {"alice-upstream": "token-a", "bob-upstream": "token-b"}[phone]
            return httpx.Response(200, json=_login_payload(token))
        if state["timeout_business"]:
            raise httpx.ReadTimeout("simulated timeout", request=request)
        state["business_tokens"].append(request.headers["token"])
        return httpx.Response(200, json={"code": "0000", "detail": [{"id": 1}]})

    adapter = InternalDatahubAdapter(transport=httpx.MockTransport(upstream))
    from mcp_adapters import registry

    monkeypatch.setattr(registry, "_registry", lambda: {"internal_datahub": adapter})

    async with mysql_session_factory() as session:
        session.add_all([
            User(uid="user-a", username="alice", password="hash", status=1),
            User(uid="user-b", username="bob", password="hash", status=1),
        ])
        instance = McpInstance(
            code="datahub-main",
            name="自有数据中台",
            adapter_key="internal_datahub",
            config_json='{"timeout_seconds":30}',
            status=1,
        )
        session.add(instance)
        await session.commit()
        instance_id = instance.id

    await mcp_service.sync_capabilities(instance_id)

    app = FastAPI()
    api = APIRouter(prefix="/v1")
    api.include_router(mcp_router)
    api.include_router(credential_router)
    app.include_router(api)
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    app.dependency_overrides[require_caller] = lambda: caller["value"]

    def as_user(uid: str, username: str) -> None:
        caller["value"] = Caller(
            uid=uid,
            username=username,
            role="employee",
            dept_id=1,
            dept_code="ops",
        )

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=True)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        as_user("user-a", "alice")
        response = await client.put(
            "/v1/mcp/instances/datahub-main/credential",
            json={"account": "alice-upstream", "password": "alice-password"},
        )
        assert response.status_code == 200
        assert response.json()["data"]["status"] == "verified"
        assert "alice-upstream" not in response.text
        assert "alice-password" not in response.text

        as_user("user-b", "bob")
        response = await client.put(
            "/v1/mcp/instances/datahub-main/credential",
            json={"account": "bob-upstream", "password": "bob-password"},
        )
        assert response.status_code == 200
        assert "bob-upstream" not in response.text
        assert "bob-password" not in response.text

        as_user("user-a", "alice")
        search_body = {
            "instance_code": "datahub-main",
            "domain": "业务分析报表",
            "limit": 20,
        }
        response = await client.post("/v1/mcp/capabilities/search", json=search_body)
        assert response.json()["data"][0]["access_status"] == "available"

        execute_body = {
            "instance_code": "datahub-main",
            "capability_code": "datahub.report.platform.list",
            "arguments": {},
        }
        response = await client.post("/v1/mcp/execute", json=execute_body)
        assert response.status_code == 200
        assert response.json()["result"]["code"] == "0000"
        assert state["business_tokens"] == ["token-a"]

        response = await client.delete(
            "/v1/mcp/instances/datahub-main/credential"
        )
        assert response.status_code == 200
        response = await client.post("/v1/mcp/capabilities/search", json=search_body)
        assert response.json()["data"][0]["access_status"] == "setup_required"

        as_user("user-b", "bob")
        response = await client.post("/v1/mcp/capabilities/search", json=search_body)
        assert response.json()["data"][0]["access_status"] == "available"

        state["timeout_business"] = True
        response = await client.post("/v1/mcp/execute", json=execute_body)
        assert response.status_code == 504
        assert response.json()["error"]["code"] == "datahub_timeout"

    async with mysql_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(McpUserPermission)) == 0
        credentials = list((await session.execute(
            select(PlatformCredential).order_by(PlatformCredential.user_uid)
        )).scalars())
        assert [(row.user_uid, row.account) for row in credentials] == [
            ("user-b", "bob-upstream"),
        ]
        assert credentials[0].secret_encrypted != "bob-password"

        credential_logs = list((await session.execute(
            select(CredentialAccessLog).order_by(CredentialAccessLog.id)
        )).scalars())
        assert [(row.operator_uid, row.target_user_uid, row.action)
                for row in credential_logs] == [
            ("user-a", "user-a", "create"),
            ("user-b", "user-b", "create"),
            ("user-a", "user-a", "decrypt"),
            ("user-a", "user-a", "delete"),
            ("user-b", "user-b", "decrypt"),
        ]
        call_logs = list((await session.execute(
            select(McpCallLog).order_by(McpCallLog.id)
        )).scalars())
        assert [(row.user_uid, row.status, row.error_code) for row in call_logs] == [
            ("user-a", "success", None),
            ("user-b", "failed", "datahub_timeout"),
        ]
        serialized_logs = json.dumps([
            {"args": row.args_json, "error": row.error_code}
            for row in call_logs
        ])
        assert "alice-password" not in serialized_logs
        assert "bob-password" not in serialized_logs
        assert "token-a" not in serialized_logs
        assert "token-b" not in serialized_logs
