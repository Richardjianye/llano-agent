from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from router.v1 import router as v1_router
from services.redaction_policy import build_redaction_policy
from utils.gateway_auth import Caller, require_caller
from utils.middleware import register_exception_handlers


def test_policy_normalizes_keywords_and_derives_stable_version(monkeypatch):
    import config

    config.get_settings.cache_clear()
    monkeypatch.setenv("MASK_KEYWORDS", "  示例公司科技,LLANO,示例公司,llano,  ")
    monkeypatch.setenv("MASK_PLACEHOLDER", "[企业]")

    now = datetime(2026, 8, 18, 4, 0, tzinfo=timezone.utc)
    policy = build_redaction_policy(now=now)

    assert set(policy["keywords"]) == {"示例公司科技", "示例公司", "LLANO"}
    assert policy["keywords"].index("示例公司科技") < policy["keywords"].index("示例公司")
    assert policy["placeholder"] == "[企业]"
    assert policy["version"].startswith("sha256:")
    assert policy["expires_at"] == "2026-08-18T04:05:00Z"

    first_version = policy["version"]
    config.get_settings.cache_clear()
    monkeypatch.setenv("MASK_KEYWORDS", "llano,示例公司,示例公司科技")

    assert build_redaction_policy(now=now)["version"] == first_version
    config.get_settings.cache_clear()


def _policy_app(*, authenticated: bool) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(v1_router)

    if authenticated:
        app.dependency_overrides[require_caller] = lambda: Caller(
            uid="u1",
            username="员工",
            role="employee",
            dept_id=1,
            dept_code="rd",
            auth_type="apikey",
            api_key_id=7,
        )

    return app


def test_policy_route_requires_authentication():
    response = TestClient(_policy_app(authenticated=False)).get("/v1/redaction-policy")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "gateway_auth_failed"


def test_policy_route_is_not_cacheable_by_shared_intermediaries(monkeypatch):
    import config

    config.get_settings.cache_clear()
    monkeypatch.setenv("MASK_KEYWORDS", "示例公司")

    response = TestClient(_policy_app(authenticated=True)).get("/v1/redaction-policy")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["object"] == "llano.redaction_policy"
    assert response.json()["keywords"] == ["示例公司"]
    config.get_settings.cache_clear()
