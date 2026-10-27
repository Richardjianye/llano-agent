"""角色权限与账号管理层级测试

这套规则决定「谁能停用谁」和「谁能看会话内容」，配错就是治理事故，
所以把每条边界都钉死在测试里。
"""

from model.database import (ROLE_ADMIN, ROLE_AUDITOR, ROLE_DEVELOPER,
                            ROLE_EMPLOYEE, ROLE_MANAGER, ROLE_OPERATOR,
                            ROLE_PRESIDENT, role_level)
from model.org_defaults import AUDIT_DEPT_CODE
from services.permissions import (USAGE_SCOPE_ALL, USAGE_SCOPE_DEPT,
                                  USAGE_SCOPE_SELF, can_assign_role,
                                  can_manage, can_view_audit, has_permission,
                                  manageable_roles, permissions_of,
                                  resolve_usage_scope)

OTHER_DEPT = "rnd"


# ==================== 功能权限（按角色）====================

def test_developer_has_all_config_permissions():
    """开发者的系统配置权限与总裁一致"""
    for permission in ("dept:view", "dept:edit", "user:view", "user:edit",
                       "model:view", "model:edit"):
        assert has_permission(ROLE_DEVELOPER, permission)
        assert has_permission(ROLE_PRESIDENT, permission)


def test_sso_management_is_limited_to_president_and_developer():
    for role in (ROLE_PRESIDENT, ROLE_DEVELOPER):
        assert has_permission(role, "sso:manage")
    for role in (ROLE_AUDITOR, ROLE_MANAGER, ROLE_EMPLOYEE):
        assert not has_permission(role, "sso:manage")


def test_auditor_cannot_touch_config():
    """能看会话内容不等于能改配置，两者是正交的"""
    for permission in ("dept:edit", "user:edit", "model:edit"):
        assert not has_permission(ROLE_AUDITOR, permission, AUDIT_DEPT_CODE)


def test_employee_has_nothing():
    assert permissions_of(ROLE_EMPLOYEE, OTHER_DEPT) == ()


def test_manager_gets_the_usage_report_and_the_department_skill_space():
    """部门负责人的管理端权限。

    ⚠️ 这条曾经断言 `== ("usage:view",)`，注释写着「唯一的管理端权限」。
    2026-08-31 负责人改定：主管要在后台筛选本部门技能并发布到公司空间，所以多了
    skill_hub 的两个点。**这是组织策略变更，不是把用例放宽**。

    开这两个点的前提是先有**数据范围**：
    - 列表由 admin_list_skills 的 restrict_to_dept 限死在本部门
    - 晋级由 services.skill_hub.can_promote 判「只能晋级自己部门的」

    没有那两处的话，给 manager 加权限就等于让他看全公司 —— 而用量报表那边同样是
    manager 可见却由 resolve_usage_scope 限死在本部门，两边口径不一致时宽的那边
    会赢。这条用例只管名单，范围由各自的用例钉。
    """
    assert permissions_of(ROLE_MANAGER, OTHER_DEPT) == (
        "usage:view", "skill_hub:view", "skill_hub:promote",
    )


def test_manager_still_cannot_delist_a_skill():
    """下架是**否决**，不是降级 —— 留给管理部门。

    主管手上有「撤回到部门空间」，那是他该用的工具。给主管下架权的话，一个部门
    主管可以把别的部门发到公司空间的技能干掉。
    """
    assert not has_permission(ROLE_MANAGER, "skill_hub:edit", OTHER_DEPT)


def test_unknown_role_and_permission_are_denied():
    assert not has_permission("hacker", "dept:edit")
    assert not has_permission(ROLE_PRESIDENT, "dept:destroy")
    assert not has_permission(None, "dept:view")


def test_legacy_roles_map_to_new_ones():
    """历史数据里的 admin/operator 不能因为改名突然失去或获得权限"""
    assert has_permission(ROLE_ADMIN, "model:edit")
    assert permissions_of(ROLE_OPERATOR, OTHER_DEPT) == ()


# ==================== 会话审计（按角色）====================

def test_audit_is_granted_by_role_not_department():
    """总裁、开发者、管理部门审计三个角色可见，与所属部门无关"""
    for role in (ROLE_PRESIDENT, ROLE_DEVELOPER, ROLE_AUDITOR):
        for dept in (AUDIT_DEPT_CODE, OTHER_DEPT, None):
            assert can_view_audit(role, dept)
            assert has_permission(role, "audit:view", dept)


def test_audit_works_even_when_department_is_not_passed():
    """审计员的部门没填对也不该被挡在外面 —— 这正是旧版的 bug"""
    assert has_permission(ROLE_AUDITOR, "audit:view")
    assert has_permission(ROLE_DEVELOPER, "audit:view")
    assert can_view_audit(ROLE_AUDITOR)


def test_manager_and_employee_never_see_audit():
    """管理部门的助理、部门负责人不该看到全公司聊天记录，划入管理部门也不行"""
    for role in (ROLE_MANAGER, ROLE_EMPLOYEE, ROLE_OPERATOR):
        for dept in (AUDIT_DEPT_CODE, OTHER_DEPT, None):
            assert not can_view_audit(role, dept)


def test_audit_denies_unknown_and_missing_roles():
    assert not can_view_audit(None)
    assert not can_view_audit("hacker", AUDIT_DEPT_CODE)


def test_legacy_admin_sees_audit_like_developer():
    assert can_view_audit(ROLE_ADMIN)


def test_developer_and_president_have_identical_permissions():
    """开发者的可视组件与权限和总裁一致；唯一的差别在账号管理层级

    例外：提示词策略的草稿编辑是刻意只给开发者、不给总裁的
    （见 can_edit_prompt_policy），这一项按现有决策保留。
    """
    for dept in (AUDIT_DEPT_CODE, OTHER_DEPT, None):
        dev = set(permissions_of(ROLE_DEVELOPER, dept))
        pres = set(permissions_of(ROLE_PRESIDENT, dept))
        assert pres - dev == set()
        assert dev - pres == {"prompt_policy:edit"}


def test_permissions_of_no_longer_depends_on_department():
    for role in (ROLE_PRESIDENT, ROLE_DEVELOPER, ROLE_AUDITOR):
        assert "audit:view" in permissions_of(role, OTHER_DEPT)
        assert "audit:view" in permissions_of(role, None)


# ==================== 用量报表的数据范围 ====================

def test_president_and_developer_see_all_usage():
    for role in (ROLE_PRESIDENT, ROLE_DEVELOPER):
        scope = resolve_usage_scope(role, dept_id=3, uid="u1", dept_code=OTHER_DEPT)
        assert scope.scope == USAGE_SCOPE_ALL
        assert scope.is_all


def test_manager_is_limited_to_own_department():
    """点开报表看到全公司花销是典型事故，这条必须钉死"""
    scope = resolve_usage_scope(ROLE_MANAGER, dept_id=7, uid="u2", dept_code=OTHER_DEPT)
    assert scope.scope == USAGE_SCOPE_DEPT
    assert scope.dept_id == 7
    assert not scope.is_all


def test_auditor_is_limited_to_own_department_too():
    """能看会话内容（管理部门）与能看全公司用量是两件事，不互相蕴含"""
    scope = resolve_usage_scope(ROLE_AUDITOR, dept_id=1, uid="u3",
                                dept_code=AUDIT_DEPT_CODE)
    assert scope.scope == USAGE_SCOPE_DEPT
    assert scope.dept_id == 1


def test_employee_only_sees_self():
    scope = resolve_usage_scope(ROLE_EMPLOYEE, dept_id=7, uid="u4",
                                dept_code=OTHER_DEPT)
    assert scope.scope == USAGE_SCOPE_SELF
    assert scope.user_uid == "u4"


def test_manager_without_department_degrades_to_self():
    """没部门的负责人若按部门过滤，dept_id IS NULL 会把所有无部门账号漏出去"""
    scope = resolve_usage_scope(ROLE_MANAGER, dept_id=None, uid="u5",
                                dept_code=None)
    assert scope.scope == USAGE_SCOPE_SELF
    assert scope.user_uid == "u5"


def test_unknown_role_gets_the_narrowest_scope():
    """脏数据里的未知角色按最低级处理，不能因此提权"""
    scope = resolve_usage_scope("hacker", dept_id=1, uid="u6", dept_code=OTHER_DEPT)
    assert scope.scope == USAGE_SCOPE_SELF


def test_legacy_admin_still_sees_all_usage():
    assert resolve_usage_scope(ROLE_ADMIN, 1, "u7", OTHER_DEPT).is_all


def test_legacy_operator_only_sees_self():
    assert resolve_usage_scope(ROLE_OPERATOR, 1, "u8", OTHER_DEPT).scope \
        == USAGE_SCOPE_SELF


def test_usage_view_all_is_not_granted_by_department():
    """用量范围只看角色，不像 audit:view 那样挂在管理部门上，别把两套规则搞混"""
    assert not has_permission(ROLE_AUDITOR, "usage:view:all", AUDIT_DEPT_CODE)
    assert not has_permission(ROLE_MANAGER, "usage:view:all", AUDIT_DEPT_CODE)


def test_employee_cannot_open_the_report_page():
    """员工没有 usage:view，报表页对他们直接是 403"""
    assert not has_permission(ROLE_EMPLOYEE, "usage:view", OTHER_DEPT)
    assert has_permission(ROLE_MANAGER, "usage:view", OTHER_DEPT)


# ==================== 账号管理层级 ====================

def test_president_can_disable_developer():
    assert can_manage(ROLE_PRESIDENT, ROLE_DEVELOPER)


def test_developer_cannot_touch_president():
    assert not can_manage(ROLE_DEVELOPER, ROLE_PRESIDENT)


def test_developers_can_manage_each_other():
    """开发者之间可以互相管理；总裁之间仍然不行"""
    assert can_manage(ROLE_DEVELOPER, ROLE_DEVELOPER)
    assert not can_manage(ROLE_PRESIDENT, ROLE_PRESIDENT)


def test_developer_can_manage_lower_roles():
    for role in (ROLE_AUDITOR, ROLE_MANAGER, ROLE_EMPLOYEE):
        assert can_manage(ROLE_DEVELOPER, role)


def test_legacy_admin_is_same_level_as_developer():
    assert role_level(ROLE_ADMIN) == role_level(ROLE_DEVELOPER)
    assert can_manage(ROLE_ADMIN, ROLE_DEVELOPER)
    assert not can_manage(ROLE_ADMIN, ROLE_PRESIDENT)
    assert can_manage(ROLE_PRESIDENT, ROLE_ADMIN)


def test_cannot_assign_role_at_or_above_own_level():
    assert not can_assign_role(ROLE_DEVELOPER, ROLE_PRESIDENT)
    assert can_assign_role(ROLE_DEVELOPER, ROLE_DEVELOPER)
    assert can_assign_role(ROLE_PRESIDENT, ROLE_DEVELOPER)
    assert not can_assign_role(ROLE_PRESIDENT, ROLE_PRESIDENT)
    assert not can_assign_role(ROLE_AUDITOR, ROLE_AUDITOR)


def test_manageable_roles_excludes_president_for_developer():
    assert ROLE_PRESIDENT not in manageable_roles(ROLE_DEVELOPER)
    assert ROLE_DEVELOPER in manageable_roles(ROLE_DEVELOPER)
    assert ROLE_PRESIDENT not in manageable_roles(ROLE_PRESIDENT)
    assert ROLE_AUDITOR in manageable_roles(ROLE_DEVELOPER)
    assert ROLE_DEVELOPER in manageable_roles(ROLE_PRESIDENT)
    assert manageable_roles(ROLE_EMPLOYEE) == ()


def test_unknown_role_manages_nobody():
    assert not can_manage("hacker", ROLE_EMPLOYEE)
    assert manageable_roles(None) == ()
