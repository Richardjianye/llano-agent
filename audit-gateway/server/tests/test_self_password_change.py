"""自助修改密码（阶段 9 · S9-A）

这一层要保证的核心只有一句：**「修改」不能退化成「重置」**。

差别不在名字上。验当前密码，会话被偷的人顶多在会话有效期内活动，机主改一次
密码就把他踢出去；不验，一次会话劫持直接升级成永久接管 —— 机主被锁在门外，
而他能看到的全部线索就是「我的密码怎么不对了」。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from model.response import UserInfo
from router.admin import router as admin_router
from utils.crypt import hash_password
from utils.dependencies import require_login
from utils.middleware import register_exception_handlers

CURRENT = "OldPass!234"
NEW = "NewPass!567"


class _Row:
    def __init__(self, uid="u1", username="admin", password=None):
        self.uid = uid
        self.username = username
        self.password = password if password is not None else hash_password(CURRENT)
        self.permission = "president"
        self.status = 1
        self.dept_id = 1


@pytest.fixture
def app(monkeypatch, tmp_path):
    import router.admin.account as module

    state = {
        "row": _Row(),
        "written": [],
        "revoked_tokens": [],
        "revoked_keys": [],
        "stored_tokens": [],
    }

    async def fake_get_user_info(uid=None, **kwargs):
        return state["row"] if uid == "u1" else None

    async def fake_update_password(item):
        state["written"].append((item.uid, item.password))

    async def fake_revoke_user_keys(uid, reason=None, by=None):
        state["revoked_keys"].append(uid)

    class _TM:
        async def revoke_user_tokens(self, uid, reason="security"):
            state["revoked_tokens"].append((uid, reason))
            return 3

        async def store_token(self, token, uid, device_info=None):
            state["stored_tokens"].append((token, uid))
            return True

    monkeypatch.setattr(module, "get_user_info", fake_get_user_info)
    monkeypatch.setattr(module, "update_user_password", fake_update_password)
    monkeypatch.setattr(module, "revoke_user_api_keys", fake_revoke_user_keys)
    monkeypatch.setattr(module, "token_manager", _TM())

    # SSO 的首次改密标记用真实临时表验证，不能连测试配置里的 localhost MySQL。
    from sqlalchemy import create_engine, insert
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from model.database import User
    database = tmp_path / "password.db"
    sync_engine = create_engine(f"sqlite:///{database}")
    User.__table__.create(sync_engine)
    with sync_engine.begin() as connection:
        connection.execute(insert(User).values(uid="u1", username="admin",
            password=state["row"].password, permission="president", status=1, must_change_password=True))
    sync_engine.dispose()
    factory = async_sessionmaker(create_async_engine(f"sqlite+aiosqlite:///{database}"))
    async def local_factory(): return factory
    monkeypatch.setattr(module, "init_async_session", local_factory)
    state["database"] = database

    application = FastAPI()
    register_exception_handlers(application)
    application.include_router(admin_router)
    application.dependency_overrides[require_login] = lambda: UserInfo(
        id=1, uid="u1", username="admin", permission="president", dept_id=1, dept_code="exec"
    )
    application.state.test_state = state
    return application


def _post(app, **body):
    payload = {"current_password": CURRENT, "new_password": NEW}
    payload.update(body)
    return TestClient(app).post("/admin/account/me/password", json=payload)


# ── 「修改」不能退化成「重置」──────────────────────────────────────────

def test_baseline_a_correct_current_password_changes_it(app):
    """基线。先立这条，否则下面的拒绝用例可能只是因为接口什么都不接受。"""
    body = _post(app).json()
    state = app.state.test_state

    assert body["code"] == 0
    assert len(state["written"]) == 1
    assert state["written"][0][0] == "u1"
    import sqlite3
    with sqlite3.connect(state["database"]) as connection:
        assert connection.execute("SELECT must_change_password FROM users WHERE uid='u1'").fetchone() == (0,)


def test_a_wrong_current_password_changes_nothing(app):
    """这是整个功能的安全前提。

    不验当前密码的话，偷到 Cookie 的人可以直接改密码把机主锁在门外 ——
    一次会话劫持变成永久接管。
    """
    body = _post(app, current_password="whatever").json()
    state = app.state.test_state

    assert body["code"] != 0
    assert state["written"] == [], "当前密码不对却写了新密码"
    assert state["revoked_tokens"] == []


def test_the_stored_hash_is_not_the_plaintext(app):
    _post(app)
    _, written = app.state.test_state["written"][0]

    assert written != NEW
    assert written.startswith("$2")


def test_reusing_the_same_password_is_refused(app):
    body = _post(app, new_password=CURRENT).json()

    assert body["code"] != 0
    assert app.state.test_state["written"] == []


def test_a_short_password_is_refused_before_anything_is_written(app):
    body = _post(app, new_password="short").json()

    assert body["code"] != 0
    assert app.state.test_state["written"] == []


# ── 改完要把别人踢下线 ────────────────────────────────────────────────

def test_other_sessions_are_revoked(app):
    """改密码最常见的动机就是「我怀疑有人在用我的号」。

    不吊销其它会话的话，那个人手上的令牌照样有效 —— 改密码只是让机主
    自己安心了一下，实际什么都没变。
    """
    _post(app)
    state = app.state.test_state

    assert state["revoked_tokens"] == [("u1", "self_password_change")]


def test_api_keys_are_left_alone_by_default(app):
    """例行改密不动 API Key：那是用户另外签发的凭据，一改密码就把桌面客户端
    全部踢下线，只会让人不愿意改密码。"""
    _post(app)

    assert app.state.test_state["revoked_keys"] == []


def test_api_keys_can_be_revoked_on_demand(app):
    """怀疑凭据外泄时要能一起吊销 —— 否则攻击者用旧密码建的 Key 会活下来。"""
    _post(app, revoke_api_keys=True)

    assert app.state.test_state["revoked_keys"] == ["u1"]


# ── 谁能用 ────────────────────────────────────────────────────────────

def test_an_ordinary_employee_can_change_their_own_password(app, monkeypatch):
    """这个接口不走 user:edit。走的话只有总裁和开发者能改自己的密码，
    而全公司每个人都要改。"""
    app.dependency_overrides[require_login] = lambda: UserInfo(
        id=2, uid="u1", username="员工", permission="employee", dept_id=2, dept_code="rd"
    )

    assert _post(app).json()["code"] == 0


def test_the_route_requires_a_login(app):
    """没登录就不该够得着 —— 否则它就是个匿名改密接口。"""
    app.dependency_overrides.pop(require_login, None)

    assert _post(app).status_code in (401, 403) or _post(app).json()["code"] != 0


# ── 破窗脚本 ──────────────────────────────────────────────────────────

def test_the_break_glass_script_has_no_remote_entry_point():
    """破窗脚本必须只能在服务器上跑。

    它存在的全部理由是「Web 那一侧的信任链已经断了」—— 总裁被拿下之后没有
    更高的角色能重置他。给它开任何 HTTP 入口，就等于把最后一道门搬回到
    已经被攻破的那一侧。
    """
    import os

    path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "scripts", "reset_account_password.py",
    )
    source = open(path, encoding="utf-8").read()

    for forbidden in ("APIRouter", "@router", "FastAPI", "fastapi"):
        assert forbidden not in source, f"破窗脚本里出现了 {forbidden}"


def test_the_app_never_imports_or_shells_out_to_the_break_glass_script():
    """应用代码不能 import 它，也不能 subprocess 起它。

    只找**真的调用**，不找提到 —— account.py 的文档字符串里指向这个脚本是件
    好事（那是告诉后来人最后一道门在哪），把它一并判红只会逼人删掉说明。
    """
    import ast
    import os

    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    hits = []

    for sub in ("router", "services", "db", "utils"):
        for dirpath, _, names in os.walk(os.path.join(root, sub)):
            for n in names:
                if not n.endswith(".py"):
                    continue
                path = os.path.join(dirpath, n)
                tree = ast.parse(open(path, encoding="utf-8").read())

                for node in ast.walk(tree):
                    # import scripts.reset_account_password / from scripts... import ...
                    if isinstance(node, ast.Import):
                        if any("reset_account_password" in a.name for a in node.names):
                            hits.append(f"{path}: import")
                    elif isinstance(node, ast.ImportFrom):
                        if "reset_account_password" in (node.module or ""):
                            hits.append(f"{path}: from-import")
                    # subprocess/os.system 里出现脚本名
                    elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                        if "reset_account_password.py" in node.value and ".py" in node.value:
                            # 只有出现在字符串**且不是文档字符串**时才算
                            if node is not ast.get_docstring(tree, clean=False):
                                pass  # 由下面的 subprocess 检查兜底

    # subprocess 调用里带脚本名的，单独扫一遍源码文本
    for sub in ("router", "services", "db", "utils"):
        for dirpath, _, names in os.walk(os.path.join(root, sub)):
            for n in names:
                if not n.endswith(".py"):
                    continue
                path = os.path.join(dirpath, n)
                text = open(path, encoding="utf-8").read()
                if "reset_account_password" in text and (
                    "subprocess" in text or "os.system" in text or "Popen" in text
                ):
                    hits.append(f"{path}: subprocess")

    assert hits == [], f"破窗脚本被应用代码调用了：{hits}"
