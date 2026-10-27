"""「本部门」包不包括下级（2026-09-02）

## 这组是一次真实事故的化石

63 个部门里 48 个填了 `parent_id`，树在数据里是完整的 —— 但没有任何一处权限
判定读过它。所有数据范围都是 `dept_id == X`，一个等号。

在真库上量出来的样子：

    负责人  部门            本部门  下级   看得到 / 实际管辖
    玖九   客户服务中心I        1    47    1 / 48
    白术   国内营销中心         2    32    2 / 34
    子龙   视觉中心           2    29    2 / 31

**37 个部门负责人里 8 个受影响，153 个人对他们的负责人完全不可见。**
玖九打开报表看到「1 个人」，他不会觉得这是权限设计，他会觉得系统坏了。

## 这组守三件事

1. 向下走得到（这是修的东西）
2. **向上和平级走不到**（这是修它时最容易顺手做错的方向，而错的代价更大）
3. 忘了展开时**当场报错**，不是默默放行或默默返回空
"""

from __future__ import annotations

import pytest
from services.org_tree import expand_descendants
from services.permissions import (USAGE_SCOPE_DEPT, resolve_usage_scope)

# 视觉中心(9) ├ 视觉设计部(20) └ AI视频组(30)
#            └ 设计创意部(21)
# 财务中心(40) └ 核算组(41)
TREE = {9: None, 20: 9, 30: 20, 21: 9, 40: None, 41: 40}


# ── 1. 向下 ──────────────────────────────────────────────────────────────

def test_a_center_head_reaches_every_level_below():
    """视觉中心的负责人要能看到设计部、创意部，**以及设计部下面的 AI 视频组**。

    只展开一层是个很自然的实现（`WHERE parent_id = X`），而它在三级组织上
    照样漏人 —— 公司里「客户服务中心I」正是这种深度。
    """
    assert expand_descendants(9, TREE) == (9, 20, 21, 30)


def test_a_sub_department_head_reaches_only_their_own_branch():
    assert expand_descendants(20, TREE) == (20, 30)


def test_a_leaf_department_is_just_itself():
    assert expand_descendants(30, TREE) == (30,)


# ── 2. 不向上、不向平级 ──────────────────────────────────────────────────

def test_a_sub_department_head_cannot_reach_the_parent():
    """**这一条比上面三条都重要。**

    向下漏人的表现是「我的人怎么不见了」，有人会来报；向上多看的表现是
    「设计部主管看到了整个视觉中心的用量」，没有任何人会来报。
    """
    assert 9 not in expand_descendants(20, TREE)


def test_siblings_never_leak_into_each_other():
    assert 21 not in expand_descendants(20, TREE)
    assert 20 not in expand_descendants(21, TREE)


def test_separate_trees_do_not_bleed():
    assert set(expand_descendants(9, TREE)).isdisjoint(expand_descendants(40, TREE))


# ── 3. 脏数据不能让权限判定挂掉，也不能让它变宽 ──────────────────────────

@pytest.mark.parametrize("bad,root,expect", [
    ({1: 2, 2: 1}, 1, {1, 2}),      # 环
    ({1: 1}, 1, {1}),               # 自环
    ({1: None}, 99, {99}),          # 指向不存在的部门 / 孤儿
    ({}, 5, {5}),                   # 空表
])
def test_broken_trees_degrade_to_less_not_more(bad, root, expect):
    """环、自环、断链都可能出现在真数据里。碰到就停在那儿 ——
    结果是**少看到**，不是多看到，也不是抛异常把整个报表打挂。"""
    assert set(expand_descendants(root, bad)) == expect


# ── 4. 忘了展开必须报错 ──────────────────────────────────────────────────

def test_forgetting_to_widen_raises_instead_of_guessing():
    """`UsageScope.dept_ids` 为空而范围是 DEPT —— 说明路由层忘了展开。

    这里有两个「默默犯错」的选项，都比报错糟：
    放行 = 部门负责人看到全公司；空过滤 = 他以为部门一条用量都没有。
    """
    from db.selects.usage import _apply_scope
    from model.database import UsageDaily
    from sqlalchemy import select

    scope = resolve_usage_scope(role="manager", dept_id=9, uid="u", dept_code="visual")
    assert scope.scope == USAGE_SCOPE_DEPT
    assert scope.dept_ids == (), "resolve_usage_scope 应保持纯函数，不自己去读库"

    with pytest.raises(ValueError, match="没有展开"):
        _apply_scope(select(UsageDaily), UsageDaily, scope)


def test_a_widened_scope_filters_by_the_whole_subtree():
    from db.selects.usage import _apply_scope
    from model.database import UsageDaily
    from sqlalchemy import select

    scope = resolve_usage_scope(
        role="manager", dept_id=9, uid="u", dept_code="visual"
    ).widened(expand_descendants(9, TREE))

    sql = str(_apply_scope(select(UsageDaily), UsageDaily, scope))

    assert "IN" in sql.upper(), "应该是 IN 一组部门，不是一个等号"


def test_self_scope_is_untouched_by_all_of_this():
    """反向基线：普通员工仍然只看自己。

    只测部门那条的话，把 `_apply_scope` 改成「永远不加 WHERE」也能过一半用例，
    而那是全公司可见。
    """
    from db.selects.usage import _apply_scope
    from model.database import UsageDaily
    from sqlalchemy import select

    scope = resolve_usage_scope(role="employee", dept_id=9, uid="u-me", dept_code="visual")
    sql = str(_apply_scope(select(UsageDaily), UsageDaily, scope))

    assert "user_uid" in sql


# ── 5. 落点只有一处 ─────────────────────────────────────────────────────

def test_manager_data_scope_goes_through_org_tree_everywhere():
    """扫源码：凡是按角色算部门范围的地方，都必须走展开。

    漏掉一处的表现是「用量报表里看得到，技能库里看不到」—— 两处口径不一致时，
    没人会觉得是 bug，只会觉得是两个功能。
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for path in [root / "router" / "admin" / "usage.py",
                 root / "router" / "admin" / "skill_hub.py"]:
        text = path.read_text(encoding="utf-8")
        if "dept_scope_ids" not in text:
            offenders.append(path.name)

    assert offenders == [], f"这些地方按部门限范围但没展开部门树：{offenders}"
