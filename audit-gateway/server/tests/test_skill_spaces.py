"""技能库的空间分层（阶段 21；09-03 放开全员可见，09-23 收回）

负责人 2026-08-31 定：客户端发布**一律进部门空间**；主管在后台筛选并发到公司
空间；**取消管理部门审核**。

负责人 2026-09-03 再定：**发布即全员可见，主管晋级不再是可见性开关**。
发布初期那道晋级卡得太死——技能做出来得等主管点一下才有第二个人看得见，
而主管多数时候根本不知道有东西在等他，结果是技能躺在部门空间里没人用。
`scope=company` 从此读作「公司精选」（排在最前面），`scope_dept_id` 是分组维度。

负责人 2026-09-23 收回：**部门空间只对本部门（及下级部门）可见**，全公司可见要靠
主管晋级到公司空间——回到 08-31 的分层。

服务端仍然只有两级，**没有 personal** —— 个人技能留在客户端本地，不上传已安装列表。

这一组守三件事：**可见性只有一处定义**、**主管的数据范围是真的**、
**别的部门的技能看不到**。
"""

from __future__ import annotations

import pytest
from services.org_tree import expand_ancestors
from services.skill_hub import SCOPE_COMPANY, SCOPE_DEPT, can_promote

# 视觉中心(9) ├ 视觉设计部(20) └ AI 视频组(30)
#            └ 设计创意部(21)
# 财务中心(40) └ 核算组(41)
TREE = {9: None, 20: 9, 30: 20, 21: 9, 40: None, 41: 40}


# ── 数据范围：主管只能碰自己部门的 ────────────────────────────────────────

def test_a_manager_can_promote_only_their_own_department():
    """上一轮特意没给 manager 权限，理由是「这两个权限点没有数据范围概念，
    给了就是全公司可见」。现在负责人要主管做筛选，那就得把范围真正做出来 ——
    在权限名单里加个词是不够的。
    """
    assert can_promote("manager", 7, 7) is True
    assert can_promote("manager", 7, 9) is False


def test_an_admin_can_promote_any_department():
    """管理部门/开发者不受部门限制。"""
    assert can_promote("president", None, 9, is_admin=True) is True


def test_a_manager_without_a_department_cannot_promote_anything():
    """没有部门的主管账号（数据没配全）不该获得「任意部门」的能力。

    反过来实现（dept_id 为空就放行）是很自然的写法，也是最危险的：
    一个字段没填就等于最高权限。
    """
    assert can_promote("manager", None, 7) is False


def test_a_skill_without_a_department_cannot_be_promoted_by_a_manager():
    """历史脏数据（scope_dept_id 为 NULL）不该被任意主管认领走。"""
    assert can_promote("manager", 7, None) is False


# ── 可见性：公司空间 ∪ 本部门及所有上级空间（2026-09-23 收回全员可见）──────

async def test_skill_visibility_condition_includes_self_and_every_parent(monkeypatch):
    """⚠️ 2026-09-23 收回 09-03 的「发布即全员可见」，这条是它的守卫。

    再放开成全员可见的话，这条会第一个报红。
    """
    from db.selects import skill_hub as selects

    async def ancestor_ids(dept_id):
        assert dept_id == 30
        return (9, 20, 30)

    monkeypatch.setattr(selects, "dept_ancestor_ids", ancestor_ids)
    sql = str((await selects._visible_to(30)).compile(compile_kwargs={"literal_binds": True}))

    assert "scope_dept_id IN (9, 20, 30)" in sql
    assert "'company'" in sql, "公司空间对所有人可见"


async def test_a_user_without_a_department_sees_only_the_company_space(monkeypatch):
    """新入职还没分配部门的账号会看到一个几乎空的技能库 —— 这是对的，不是 bug，
    但要有一条用例把它写下来，否则下次有人会当成缺陷去「修」。"""
    from db.selects import skill_hub as selects

    async def ancestor_ids(dept_id):
        assert dept_id is None
        return ()

    monkeypatch.setattr(selects, "dept_ancestor_ids", ancestor_ids)
    sql = str((await selects._visible_to(None)).compile(compile_kwargs={"literal_binds": True}))

    assert "scope_dept_id" not in sql
    assert "'company'" in sql


def test_a_parent_does_not_see_child_or_sibling_skill_spaces():
    assert expand_ancestors(9, TREE) == (9,)
    assert 21 not in expand_ancestors(20, TREE)


def test_skill_visibility_never_crosses_into_another_department_tree():
    assert set(expand_ancestors(30, TREE)).isdisjoint(expand_ancestors(41, TREE))


def test_the_company_space_sorts_first_and_then_ones_own_department():
    """公司空间排最前，其次是自己部门，上级部门空间的排后面。"""
    from db.selects import skill_hub as selects

    order = selects._relevance_order(30)
    rendered = [str(c.compile(compile_kwargs={"literal_binds": True})) for c in order]

    assert "'company'" in rendered[0], "公司精选排最前"
    assert "scope_dept_id = 30" in rendered[1], "其次是自己部门的"
    assert "updated_at DESC" in rendered[-1]


def test_ordering_without_a_department_still_works():
    """没有部门的账号不该因为少一个排序键就报错。"""
    from db.selects import skill_hub as selects

    order = selects._relevance_order(None)

    assert len(order) == 2  # 公司精选 + 更新时间


# ── 部门树 ────────────────────────────────────────────────────────────────

def test_a_child_department_inherits_every_parent_skill_space():
    """AI 视频组要同时继承视觉设计部和视觉中心，不能只走一层。"""
    assert expand_ancestors(30, TREE) == (9, 20, 30)


@pytest.mark.parametrize("broken", [
    {1: 2, 2: 1},
    {1: 1},
    {1: 99},
])
def test_broken_parent_chains_do_not_widen_skill_visibility(broken):
    """脏树宁可只看本部门，也不能把一个不可信的上级空间放进来。"""
    assert expand_ancestors(1, broken) == (1,)


def test_visibility_is_defined_in_exactly_one_place():
    """search / inspect / bundle / catalog 四条路径复用同一个条件。

    抄三份的话，改可见性规则时漏掉一处的表现是「搜不到但装得上」或者反过来 ——
    两者都不会报错，而后者意味着别的部门的技能能被装走。
    """
    import inspect

    from db.selects import skill_hub as selects

    source = inspect.getsource(selects)

    # 三个读函数都必须走 _visible_to，而不是各自拼 scope 条件。
    assert source.count("await _visible_to(dept_id,") >= 3
    for fn in ["search_skills", "get_skill_meta", "get_skill_bundle", "department_catalog"]:
        body = inspect.getsource(getattr(selects, fn))
        assert "_visible_to" in body, f"{fn} 没走统一的可见性条件"


# ── 发布一律进部门空间 ────────────────────────────────────────────────────

def test_publishing_always_lands_in_the_department_space():
    """晋级是主管在后台做的动作，不是发布者能选的。

    能选的话，「主管筛选」这一步就形同虚设 —— 每个人都会直接选公司空间。
    """
    import inspect

    from db.inserts import skill_hub as inserts

    source = inspect.getsource(inserts.publish_skill)

    assert "scope=SCOPE_DEPT" in source
    assert "SCOPE_COMPANY" not in source, "发布路径不该能写出公司空间"


def test_publish_tells_the_client_where_the_skill_landed():
    """发布响应必须带回 scope，否则客户端只能猜。

    猜的代价是**说错方向**：说「全公司可见」会让员工去跨部门宣传一个别人搜不到
    的技能；对一个已被主管晋级的技能说「本部门可见」，则等于告诉他晋级被撤销了。
    追加版本走的是技能当前的 scope，只有网关知道那是什么。

    三条返回路径（首发 / 同内容 / 新版本）都要带，漏一条就是那一条路径上的
    措辞开始瞎编 —— 而那正好是最难注意到的一条（同内容短路）。
    """
    import inspect

    from db.inserts import skill_hub as inserts

    source = inspect.getsource(inserts.publish_skill)

    assert source.count('"scope":') == 3, "首发/同内容/新版本三条返回都要带 scope"


def test_the_client_facing_reads_take_the_department_from_the_credential():
    """部门从凭据解出来，不听客户端 —— 否则报一个别的部门就能看别人的技能。"""
    import inspect

    from router.v1 import skill_hub as endpoint

    for fn in [endpoint.list_skills, endpoint.inspect_skill, endpoint.fetch_bundle]:
        source = inspect.getsource(fn)
        assert "caller.dept_id" in source, f"{fn.__name__} 没有按调用者部门过滤"


# ── 撤回 ≠ 下架 ──────────────────────────────────────────────────────────

def test_withdraw_and_delist_are_different_permissions():
    """**撤回是降级，下架是否决。**

    技能撤回后在原部门仍然可用；下架之后谁都装不了。给主管下架权的话，一个部门
    主管可以把别的部门发到公司空间的技能干掉。
    """
    from services.permissions import PERMISSIONS

    assert "manager" in PERMISSIONS["skill_hub:promote"]
    assert "manager" not in PERMISSIONS["skill_hub:edit"]


def test_withdrawing_returns_the_skill_to_its_own_department_not_the_operators():
    """操作人可能是管理部门的人，把技能退回到管理部门显然不对。"""
    import inspect

    from db.updates import skill_hub as updates

    source = inspect.getsource(updates.set_skill_scope)

    assert "skill.owner_dept_id" in source


def test_there_is_no_president_approval_queue():
    """负责人明确取消了管理部门审核这一步。

    多一道没人处理的审批队列，等于把技能永远卡在部门里。这条扫的是「有没有人
    顺手把它加回来」—— 加回来不会有任何用例报红，但会让主管的操作变成一次
    石沉大海的申请。
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    source = (root / "router" / "admin" / "skill_hub.py").read_text(encoding="utf-8")

    for banned in ["pending_review", "approval", "await_president", "promotion_status"]:
        assert banned not in source, f"不该有 {banned}：负责人取消了审核队列"
