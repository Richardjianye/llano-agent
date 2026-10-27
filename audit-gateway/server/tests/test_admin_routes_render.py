"""管理端 GET 路由的**渲染冒烟**

## 为什么补这一组

`router/admin/skill_hub.py` 和 `router/admin/redaction.py` 里有 11 处写的是
`Response.success(data=...)`。`Response` 是 pydantic 模型，**根本没有 success
这个属性** —— 这两个页面的每一个接口在真机上都是 500，从写出来那天起。

而当时 633 条用例全绿。原因是它们测的是 service 层与 db 层：`admin_list_skills`
被单独调用过、`keywords_for` 被单独调用过，唯独**没有任何一条用例发出过一次
HTTP 请求**。函数对了，把结果装进响应的那一行错了，测试看不见。

108 的 gateway.log 里那条 `AttributeError: success` 是唯一的证据，而它只在
有人真的打开那个页面时才产生。

## 这组测什么

不测业务逻辑（那些已经有各自的用例了），只测**每个 GET 路由能不能把结果渲染
出来**：依赖全部 override 掉、数据层全部 stub 成空结果，然后看状态码和信封。
这样任何「返回那一行写错了」都会当场红掉，不管错的是属性名、字段名还是类型。

数据层 stub 成空是有意的：这组要在没有数据库的环境里跑。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from model.response import UserInfo
from utils.dependencies import get_current_user, require_login
from utils.middleware import register_exception_handlers


def _president() -> UserInfo:
    """管理部门账号 —— 管理端权限最全的那个，能走到最多的路由。"""
    return UserInfo(id=1, uid="u-president", username="test", permission="president",
                    dept_id=1, dept_code="ZCB", permissions=[])


@pytest.fixture
def client(monkeypatch):
    # 数据层一律 stub。这组不验证查询，只验证「查出来之后那一行」。
    async def _rows(*a, **kw):
        return []

    async def _page(*a, **kw):
        return [], 0

    async def _keywords(*a, **kw):
        return ["LLANO Agent"]

    async def _options(*a, **kw):
        return {"departments": [], "models": []}

    monkeypatch.setattr("router.admin.skill_hub.admin_list_skills", _page)
    monkeypatch.setattr("router.admin.skill_hub.list_versions", _rows)

    async def _files(slug, version=None):
        return {"slug": slug, "name": "demo", "status": "listed", "scope": "dept",
                "version": version or 1, "bundle_hash": "abc", "file_count": 1,
                "total_bytes": 10, "changelog": "", "publisher_username": "",
                "published_at": "", "files": []}

    monkeypatch.setattr("router.admin.skill_hub.get_version_files", _files)
    monkeypatch.setattr("router.admin.skill_hub.count_skills_by_dept", _rows)
    monkeypatch.setattr("router.admin.skill_hub.count_skills_by_publisher", _rows)
    monkeypatch.setattr("router.admin.apikey.list_keys", _rows)
    monkeypatch.setattr("router.admin.catalog.list_providers", _rows)
    monkeypatch.setattr("router.admin.department.list_departments", _rows)
    monkeypatch.setattr("router.admin.quota.list_quota_rules", _rows)
    monkeypatch.setattr("router.admin.release.list_releases", _rows)
    monkeypatch.setattr("router.admin.usage.fetch_filter_options", _options)
    monkeypatch.setattr("router.admin.audit.fetch_audit_filter_options", _options)
    monkeypatch.setattr("router.admin.skill_report.fetch_events", _page)
    monkeypatch.setattr("router.admin.mcp.list_adapters", lambda: [])
    # redaction 的两条路由：/effective 在函数体里 import keywords_for，
    # 所以要打在源模块上；/list 直接开数据库会话，这组不带库，只测另一条。
    monkeypatch.setattr("services.redaction_policy.keywords_for", _keywords)

    from router.admin import router as admin_router

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(admin_router)
    # 权限本身有专门的用例钉着（test_permissions / test_skill_spaces），
    # 这里放行，是为了让请求走到 return 那一行 —— 那正是要测的地方。
    app.dependency_overrides[require_login] = _president
    app.dependency_overrides[get_current_user] = _president

    return TestClient(app)


@pytest.mark.parametrize("path", [
    "/admin/user/info",
    "/admin/account/me/context",
    "/admin/apikey/list",
    "/admin/department/list",
    "/admin/catalog/provider/list",
    "/admin/quota/list",
    "/admin/usage/filters",
    "/admin/audit/filters",
    "/admin/mcp/adapters",
    "/admin/release/list",
    "/admin/skill-events/list",
    "/admin/skill-hub/list",
    "/admin/skill-hub/versions?slug=demo",
    "/admin/skill-hub/files?slug=demo",
    "/admin/skill-hub/stats",
    "/admin/redaction/effective",
])
def test_the_page_actually_renders(client, path):
    """每条都必须返回 200 + code=0。

    在 `Response.success` 那个 bug 还在的时候，对应入口会直接变成 500。
    """
    response = client.get(path)

    assert response.status_code == 200, f"{path} -> {response.status_code}: {response.text[:300]}"
    assert response.json()["code"] == 0, f"{path} -> {response.text[:300]}"


def test_response_has_no_success_helper():
    """反向钉一句：`Response.success` 不存在，别再写出来。

    它读起来完全合理（很多项目确实有这么个工厂方法），所以下一个人写出来的
    概率不低。这条不是替代上面那组 —— 上面那组才是真正的防线，这条只是让
    错误信息直接说出原因，省掉一次翻 traceback。
    """
    from model.response import Response

    assert not hasattr(Response, "success"), (
        "本项目的约定是 `Response(data=...)`，没有 success 工厂方法"
    )
