"""全员通讯录（2026-09-07）

## 为什么有它

在它之前，`/v1/projects/{code}/members` 要一个 `user_uid`，而客户端**没有任何
办法拿到别人的 uid** —— 后台的账号列表要 `user:view`，普通员工没有。于是
「协作全部在客户端进行」缺了最后一块：项目建得出来、自己的 agent 拉得进来，
但同事拉不进来。

开放范围由负责人拍板：与钉钉一致，通讯录全员可见。

## 这组守三件事

1. **只回在职账号** —— 停用的人不该还能被拉进项目
2. **字段就那四个** —— 不回角色、权限、部门 id、联系方式。多一个字段的代价不是
   这次多显示一点，是下一个页面会直接拿去用
3. **群聊令牌拿不到** —— 群聊 turn 里的 agent 正在读别人的消息，让它把全员名单
   拉一遍等于给决策 #45 开一个只出不进的口子：名单出去了收不回来，而它连一次
   工具调用都不需要
"""

from __future__ import annotations

import pytest

import services.conversation_scope as cs
from router.v1 import directory as directory_mod


class _Caller:
    def __init__(self, scope="private"):
        self.uid = "u-1"
        self.scope = scope


class _User:
    def __init__(self, uid, username, realname, dept_id, status=1):
        self.uid = uid
        self.username = username
        self.realname = realname
        self.dept_id = dept_id
        self.status = status


class _Dept:
    def __init__(self, dept_id, name):
        self.id = dept_id
        self.name = name


class _Response:
    def __init__(self):
        self.headers = {}


@pytest.fixture
def _people(monkeypatch):
    rows = [
        _User("u-1", "阿甲", "协作测试甲", 3),
        _User("u-2", "阿乙", "协作测试乙", 3),
    ]

    async def fake_search(keyword="", limit=50, offset=0):
        return rows

    async def fake_count(keyword=""):
        return len(rows)

    async def fake_depts(include_disabled=False):
        return [_Dept(3, "产品研发中心")]

    monkeypatch.setattr(directory_mod, "search_directory", fake_search)
    monkeypatch.setattr(directory_mod, "count_directory", fake_count)
    monkeypatch.setattr(directory_mod, "list_departments", fake_depts)
    return rows


@pytest.mark.asyncio
async def test_it_returns_exactly_four_fields(_people):
    body = await directory_mod.directory(_Response(), q="", limit=50, offset=0,
                                         caller=_Caller())

    assert body["total"] == 2
    for person in body["users"]:
        assert set(person) == {"uid", "username", "realname", "dept_name"}


@pytest.mark.asyncio
async def test_it_never_leaks_role_or_department_id(_people):
    """「谁是审计员」不是全员该知道的事；部门用名字答就够了。"""
    body = await directory_mod.directory(_Response(), q="", limit=50, offset=0,
                                         caller=_Caller())

    blob = repr(body)
    for leaked in ("permission", "dept_id", "password", "role"):
        assert leaked not in blob


@pytest.mark.asyncio
async def test_a_group_token_cannot_enumerate_the_company(_people):
    """决策 #45：能力不跨界。名单出去了收不回来。"""
    from utils.errors import AppError

    with pytest.raises(AppError) as caught:
        await directory_mod.directory(_Response(), q="", limit=50, offset=0,
                                      caller=_Caller(scope=cs.GROUP))

    assert caught.value.http_status == 403
    # 与其他作用域拒绝同一句话：不告诉对方「你手里这把是群聊令牌」
    assert caught.value.message == cs.SCOPE_DENIED_MESSAGE


@pytest.mark.asyncio
async def test_the_directory_is_never_cached(_people):
    """停用一个人之后，别的客户端不该还能从中间层缓存里把他拉进项目。"""
    response = _Response()
    await directory_mod.directory(response, q="", limit=50, offset=0, caller=_Caller())

    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.asyncio
async def test_the_query_only_filters_active_accounts():
    """这条扫的是查询本身：`status == 1` 的条件不能被谁「顺手」去掉。

    真跑一次 SQL 要 MySQL，而这个约束是**语句的形状**，不是数据的形状 ——
    看语句就够，而且看语句不会因为测试库里恰好没有停用账号而假绿。
    """
    import inspect

    from db.selects import user as user_selects

    source = inspect.getsource(user_selects.search_directory)
    # 只看函数体：docstring 里提到 list_users 是在解释「为什么不复用」，
    # 那句话恰恰要留着。
    body = source.split(chr(34) * 3)[-1]

    assert "User.status == 1" in body
    # 也不能改成复用 list_users：那个是给管理端的，它必须看得见停用账号。
    assert "list_users" not in body
