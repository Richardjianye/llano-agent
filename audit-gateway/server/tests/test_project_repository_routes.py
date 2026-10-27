"""项目仓库调度不能改变本地项目创建的事务边界。"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from utils.gateway_auth import Caller, require_caller


def _owner() -> Caller:
    return Caller(
        uid="owner",
        username="负责人",
        role="user",
        dept_id=1,
        dept_code="RD",
    )


def _client() -> TestClient:
    from router.v1.projects import router

    app = FastAPI()
    app.include_router(router, prefix="/v1")
    app.dependency_overrides[require_caller] = _owner
    return TestClient(app)


def test_local_project_commits_before_repository_is_scheduled(monkeypatch):
    events = []

    async def create(**kwargs):
        events.append(("created", kwargs["code"]))
        return {"id": 9, "code": kwargs["code"]}

    monkeypatch.setattr("router.v1.projects.create_project", create)
    monkeypatch.setattr(
        "router.v1.projects.schedule_project_repository",
        lambda project_id: events.append(("scheduled", project_id)),
    )

    response = _client().post(
        "/v1/projects", json={"code": "demo", "name": "示例"}
    )

    assert response.status_code == 201
    assert events == [("created", "demo"), ("scheduled", 9)]


def test_owner_can_retry_a_failed_repository(monkeypatch):
    scheduled = []

    async def resolve(code, caller):
        return (
            {"id": 9, "code": code, "git_repo_status": "failed"},
            {"status": "active", "role": "owner"},
        )

    async def pending(project_id):
        return True

    monkeypatch.setattr("router.v1.projects._resolve", resolve)
    monkeypatch.setattr(
        "db.updates.project_repository.mark_repository_pending", pending
    )
    monkeypatch.setattr(
        "router.v1.projects.schedule_project_repository", scheduled.append
    )

    response = _client().post("/v1/projects/demo/repository/retry")

    assert response.status_code == 202
    assert response.json()["status"] == "pending"
    assert scheduled == [9]
