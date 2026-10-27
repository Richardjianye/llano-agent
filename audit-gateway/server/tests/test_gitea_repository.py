"""项目仓库创建的幂等与失败契约。"""

import json

import httpx
import pytest

from services.gitea import GiteaClient, GiteaError
from services.project_repository import repository_name


@pytest.mark.asyncio
async def test_creates_private_empty_repository():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            201,
            json={
                "id": 42,
                "name": "project-7-demo",
                "html_url": "https://git.example/llano-agent/project-7-demo",
            },
        )

    client = GiteaClient(
        base_url="https://git.example",
        token="test-token",
        owner="llano-agent",
        transport=httpx.MockTransport(handler),
    )
    repo = await client.ensure_private_repository(
        name="project-7-demo", description="Demo"
    )

    assert repo.id == 42
    assert seen == {
        "path": "/api/v1/user/repos",
        "auth": "token test-token",
        "body": {
            "name": "project-7-demo",
            "description": "Demo",
            "private": True,
            "auto_init": False,
        },
    }


@pytest.mark.asyncio
async def test_remote_repo_created_before_local_commit_is_discovered_on_retry():
    paths = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(422, json={"message": "already exists"})
        return httpx.Response(
            200,
            json={
                "id": 43,
                "name": "project-7-demo",
                "html_url": "https://git.example/llano-agent/project-7-demo",
            },
        )

    client = GiteaClient(
        base_url="https://git.example",
        token="test-token",
        owner="llano-agent",
        transport=httpx.MockTransport(handler),
    )
    repo = await client.ensure_private_repository(name="project-7-demo")

    assert repo.id == 43
    assert paths == [
        ("POST", "/api/v1/user/repos"),
        ("GET", "/api/v1/repos/llano-agent/project-7-demo"),
    ]


@pytest.mark.asyncio
async def test_missing_configuration_is_explicit_failure():
    with pytest.raises(GiteaError, match="未配置"):
        GiteaClient(base_url="", token="", owner="llano-agent")


def test_repository_name_is_stable_and_scoped_by_project_id():
    assert repository_name({"id": 7, "code": "demo"}) == "project-7-demo"
