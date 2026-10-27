"""权限判定 —— 全系统唯一的权限真源

三套正交的规则：

1. **功能权限**（PERMISSIONS）：某角色能不能调某类接口。纯按角色判定。

2. **会话内容可见性**（audit:view）：**按角色判定，不看部门**。
   总裁、开发者、管理部门审计三个角色可见，部门负责人与普通员工不可见。
   「管理部门审计」这个角色本身就是决策 #8「仅管理部门可见」的落点，
   不再要求账号所属部门编码是 exec —— 之前要求「角色 + 部门」双重满足，
   结果部门没填对的审计员什么都看不到。

3. **账号管理层级**（can_manage）：只能操作级别严格低于自己的账号，
   **唯一的例外**是开发者可以管理其他开发者。
   由此推出：总裁可管理开发者；开发者可管理除总裁以外的所有账号，也动不了总裁；
   任何人不能把别人提到比自己更高的级别（开发者可以再设开发者，不能设总裁）。

前端 constants/roles.ts 是这份表的镜像，只影响界面显示；真正的拦截在这里。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Dict, Optional, Tuple

from model.database import (ROLE_AUDITOR, ROLE_DEVELOPER, ROLE_EMPLOYEE,
                            ROLE_MANAGER, ROLE_PRESIDENT, role_level)
from model.org_defaults import AUDIT_DEPT_CODE

# ==================== 功能权限（按角色）====================

PERMISSIONS: Dict[str, Tuple[str, ...]] = {
    # 部门
    "dept:view": (ROLE_PRESIDENT, ROLE_DEVELOPER),
    "dept:edit": (ROLE_PRESIDENT, ROLE_DEVELOPER),
    # 账号
    "user:view": (ROLE_PRESIDENT, ROLE_DEVELOPER),
    "user:edit": (ROLE_PRESIDENT, ROLE_DEVELOPER),
    # 接入应用可签发登录凭据，独立于账号管理权限。
    "sso:manage": (ROLE_PRESIDENT, ROLE_DEVELOPER),
    # 模型与白名单
    "model:view": (ROLE_PRESIDENT, ROLE_DEVELOPER),
    "model:edit": (ROLE_PRESIDENT, ROLE_DEVELOPER),
    # 成员搜索（/admin/member/search）—— 各配置页的「选人」控件共用。单独一个点而不是
    # 借 user:view：选人只需要姓名、账号、部门，不该连带账号管理页的全部字段；以后要给
    # 部门负责人开「在本部门里选人」，在这里加角色并在 services/member_search 里收范围。
    "member:search": (ROLE_PRESIDENT, ROLE_DEVELOPER),
    # 全公司设备 Key 总览与批量吊销（2026-10-19）。批量吊销等于让一批人当场掉线，
    # 与停用账号同级；逐条仍受 can_manage 约束（开发者动不了总裁的 Key）。
    "apikey:admin": (ROLE_PRESIDENT, ROLE_DEVELOPER),
    # MCP 平台配置、能力目录与用户授权
    "mcp:view": (ROLE_PRESIDENT, ROLE_DEVELOPER),
    "mcp:edit": (ROLE_PRESIDENT, ROLE_DEVELOPER),
    # 桌面端版本发布：发布等于把安装包推给全公司，与改模型路由同级
    "release:view": (ROLE_PRESIDENT, ROLE_DEVELOPER),
    "release:edit": (ROLE_PRESIDENT, ROLE_DEVELOPER),
    # 用量报表：能进页面 ≠ 能看全公司，范围由 usage:view:all 单独控制
    "usage:view": (ROLE_PRESIDENT, ROLE_DEVELOPER, ROLE_AUDITOR, ROLE_MANAGER),
    "usage:view:all": (ROLE_PRESIDENT, ROLE_DEVELOPER),
    # 技能安全事件（阶段 7 · S7-C）。拆成两个权限点，不是一个：
    #
    # - :view 看的是「谁在什么时候把一个判定为危险的技能放回去了」。这是合规与
    #   部门指标那一侧的事，审计员、主管该看得到。
    # - :sample 看的是**员工自己写的源码**。判断一段 curl 到底是外发密钥还是
    #   正常调接口，是工程判断，交给能读代码的人。
    #
    # 合成一个权限点的话，审计员要么连样本源码一起看（多给了），要么连列表都
    # 打不开（少给了）。要收紧成一条的话，把 :sample 的名单抄成 :view 的即可。
    #
    # ⚠️ 部门负责人（manager）**暂时不给**，尽管技能指标是按部门考核的。
    #
    #   现在这两个权限点都没有数据范围概念，给了 manager 就等于让他看**全公司**
    #   的技能安全事件 —— 而用量报表那边同样是 manager 可见，却由
    #   resolve_usage_scope 限死在本部门。两边口径不一致，宽的那边会赢。
    #   要给 manager 开，先做部门范围过滤（照抄 USAGE_SCOPE_DEPT 那一套），
    #   不是在这行名单里加个词。
    "skill_security:view": (ROLE_PRESIDENT, ROLE_DEVELOPER, ROLE_AUDITOR),
    "skill_security:sample": (ROLE_PRESIDENT, ROLE_DEVELOPER),
    # 自有 skill hub（阶段 11 · S11-G）。拆成两个权限点：
    #
    # - :view 是技能列表与部门指标
    # - :edit 是下架/恢复。下架等于把一个技能从全公司的可安装列表里摘掉，
    #   与推一个安装包同级，所以照 release:edit 的名单
    #
    # ⚠️ 部门负责人（manager）**没给**，尽管技能指标正是按部门考核的。
    #
    #   本来想给 —— 主管看得到自己部门的数字，这个功能才算闭环。但
    #   test_manager_only_gets_the_usage_report 明确钉着「部门负责人唯一的管理端
    #   权限就是看用量报表」，那是一条组织策略，不该在实现一个功能时顺手改掉。
    #
    #   而且真要给，缺的不只是这行名单里加个词：这两个权限点都没有数据范围概念，
    #   给了就是**全公司**可见，而用量报表那边同样是 manager 可见却由
    #   resolve_usage_scope 限死在本部门。两边口径不一致时，宽的那边会赢。
    #   要开的话先照抄 USAGE_SCOPE_DEPT 那一套做部门范围过滤。已记 TODO。
    # ⚠️ 2026-08-31 负责人决定给部门负责人（manager）开这两个点 —— 主管要在后台
    #   筛选本部门技能并发布到公司空间。这**改掉了**原先「部门负责人唯一的管理端
    #   权限就是看用量报表」那条策略，是负责人的明确决定，不是实现顺手改的。
    #
    #   开之前必须先有**数据范围**，否则就是上一轮警告过的那个坑：这两个点没有
    #   范围概念，给了 manager 就是全公司可见。范围做在
    #   db/selects/skill_hub.admin_list_skills 的 restrict_to_dept 与
    #   services/skill_hub.can_promote 里 —— 在权限名单里加个词是不够的。
    "skill_hub:view": (ROLE_PRESIDENT, ROLE_DEVELOPER, ROLE_AUDITOR, ROLE_MANAGER),
    # 晋级到公司空间。主管只能晋级自己部门的（范围在 can_promote 里判）。
    # 没有管理部门审核那一步：管理部门基本不上线看，而且已经过主管审查；出问题由主管
    # 撤回或管理部门下架。多一道没人处理的审批队列等于把技能永远卡在部门里。
    "skill_hub:promote": (ROLE_PRESIDENT, ROLE_DEVELOPER, ROLE_MANAGER),
    # 下架是**否决**，不是降级。留给管理部门 —— 主管手上有「撤回到部门空间」，
    # 那是他该用的工具。
    "skill_hub:edit": (ROLE_PRESIDENT, ROLE_DEVELOPER),
    # 屏蔽词（阶段 18）。**不拆 view/edit** —— 与配额、白名单同一档：能看到
    # 「哪些词被遮」的人本来就该是能改它的人，拆开只会多一个没人用的角色组合。
    #
    # ⚠️ 名单与 model:edit 一致而不是更宽。改屏蔽词看着像内容配置，实际是安全
    #   配置：删一个词等于当场把那条防线关掉，且**已经发出去的消息不会追溯遮蔽**。
    #   与「改模型路由」同级是保守的，但这一项做错的代价是不可逆的。
    "redaction:edit": (ROLE_PRESIDENT, ROLE_DEVELOPER),
    # 提示词策略发布/回滚。草稿 view/edit 同时依赖部门，在下方单独判定。
    "prompt_policy:publish": (ROLE_PRESIDENT, ROLE_DEVELOPER),
}

# 提示词策略不是普通的「配置页」权限。用户明确要求：
# - 管理部门审计员只读
# - 开发者或管理部门负责人（manager + exec）可编辑草稿
# - 总裁或开发者可发布/回滚
#
# 因为 edit 同时依赖角色与部门，不能只往上面的角色表塞 ROLE_MANAGER；那样会把
# 全公司每个部门的负责人都放进来。三个权限点统一在 has_permission 里计算。
PROMPT_POLICY_VIEW_PERMISSION = "prompt_policy:view"
PROMPT_POLICY_EDIT_PERMISSION = "prompt_policy:edit"
PROMPT_POLICY_PUBLISH_PERMISSION = "prompt_policy:publish"
PROMPT_POLICY_SCOPED_PERMISSIONS = (
    PROMPT_POLICY_VIEW_PERMISSION,
    PROMPT_POLICY_EDIT_PERMISSION,
)

# ==================== 会话审计（按角色）====================

AUDIT_PERMISSION = "audit:view"

# 角色级别下限：总裁、开发者、管理部门审计可见，部门负责人及以下不可见
AUDIT_MIN_ROLE_LEVEL = role_level(ROLE_AUDITOR)

ALL_PERMISSIONS = (*PERMISSIONS, *PROMPT_POLICY_SCOPED_PERMISSIONS, AUDIT_PERMISSION)


def _legacy_equivalent(role: str) -> str:
    if role == "admin":
        return ROLE_DEVELOPER
    if role == "operator":
        return ROLE_EMPLOYEE
    return role


def can_view_audit(role: Optional[str], dept_code: Optional[str] = None) -> bool:
    """会话内容可见性：总裁、开发者、管理部门审计三个角色，不看部门

    dept_code 参数只为兼容既有调用方保留，不参与判定。
    """
    if not role:
        return False

    return role_level(role) >= AUDIT_MIN_ROLE_LEVEL


def can_view_prompt_policy(role: Optional[str], dept_code: Optional[str]) -> bool:
    """后台提示词策略页面的只读范围。"""
    effective_role = _legacy_equivalent(role or "")
    if effective_role in {ROLE_PRESIDENT, ROLE_DEVELOPER}:
        return True
    return dept_code == AUDIT_DEPT_CODE and effective_role in {
        ROLE_AUDITOR,
        ROLE_MANAGER,
    }


def can_edit_prompt_policy(role: Optional[str], dept_code: Optional[str]) -> bool:
    """草稿编辑：开发者，或管理部门负责人；总裁不自动获得该权限。"""
    effective_role = _legacy_equivalent(role or "")
    return effective_role == ROLE_DEVELOPER or (
        effective_role == ROLE_MANAGER and dept_code == AUDIT_DEPT_CODE
    )


def can_publish_prompt_policy(role: Optional[str]) -> bool:
    """发布和回滚：总裁或开发者。"""
    return _legacy_equivalent(role or "") in {ROLE_PRESIDENT, ROLE_DEVELOPER}


def has_permission(
    role: Optional[str], permission: str, dept_code: Optional[str] = None
) -> bool:
    """角色（+部门）是否具备某权限。未知权限点一律拒绝，避免拼错字导致放行"""
    if not role:
        return False

    if permission == AUDIT_PERMISSION:
        return can_view_audit(role, dept_code)

    if permission == PROMPT_POLICY_VIEW_PERMISSION:
        return can_view_prompt_policy(role, dept_code)
    if permission == PROMPT_POLICY_EDIT_PERMISSION:
        return can_edit_prompt_policy(role, dept_code)
    if permission == PROMPT_POLICY_PUBLISH_PERMISSION:
        return can_publish_prompt_policy(role)

    allowed = PERMISSIONS.get(permission)
    if allowed is None:
        return False

    return role in allowed or _legacy_equivalent(role) in allowed


def permissions_of(
    role: Optional[str], dept_code: Optional[str] = None
) -> Tuple[str, ...]:
    """某人拥有的全部权限点，下发给前端用"""
    return tuple(
        p for p in ALL_PERMISSIONS if has_permission(role, p, dept_code)
    )


# ==================== 用量报表的数据范围 ====================

USAGE_PERMISSION = "usage:view"
USAGE_ALL_PERMISSION = "usage:view:all"

# 数据范围三档
USAGE_SCOPE_ALL = "all"      # 全公司
USAGE_SCOPE_DEPT = "dept"    # 仅本部门
USAGE_SCOPE_SELF = "self"    # 仅自己


@dataclass(frozen=True)
class UsageScope:
    """某人能看到哪些用量数据

    与「功能权限」分开：能打开报表页是一回事，能看到谁的数字是另一回事。
    这两件事混在一起，就会出现部门负责人点开报表看到全公司花销的事故。
    """

    scope: str
    dept_id: Optional[int] = None
    user_uid: Optional[str] = None

    # 展开后的部门范围：本部门 + 所有下级（2026-09-02）。
    #
    # `resolve_usage_scope` 是纯函数、不碰数据库，所以这里留空；由路由层调
    # `services.org_tree.dept_scope_ids` 展开后用 `widened()` 填进来。
    #
    # ⚠️ 分成两步不是啰嗦：DEPT 范围下这个元组**为空是错误状态**，
    # 说明有人忘了展开。`_apply_scope` 会当场报错，而不是默默返回全公司
    # 或者默默返回空 —— 前者泄露，后者让人以为「这个部门没数据」。
    dept_ids: tuple = ()

    def widened(self, dept_ids) -> "UsageScope":
        """填入展开后的部门集合，返回新对象（这个类是 frozen 的）。"""
        return replace(self, dept_ids=tuple(sorted({int(i) for i in dept_ids})))

    @property
    def is_all(self) -> bool:
        return self.scope == USAGE_SCOPE_ALL


def resolve_usage_scope(
    role: Optional[str],
    dept_id: Optional[int],
    uid: Optional[str],
    dept_code: Optional[str] = None,
) -> UsageScope:
    """算出数据范围 —— 报表接口必须拿这个结果去加 WHERE，不能信前端传的过滤条件

    - 总裁 / 开发者：全公司（usage:view:all）
    - 审计 / 部门负责人：仅本部门；**没有部门的话降级成只看自己**，
      否则 `dept_id IS NULL` 会把所有无部门账号的用量都漏出去
    - 其余：仅自己
    """
    if has_permission(role, USAGE_ALL_PERMISSION, dept_code):
        return UsageScope(scope=USAGE_SCOPE_ALL)

    if role_level(role) >= role_level(ROLE_MANAGER) and dept_id is not None:
        return UsageScope(scope=USAGE_SCOPE_DEPT, dept_id=dept_id, user_uid=uid)

    return UsageScope(scope=USAGE_SCOPE_SELF, dept_id=dept_id, user_uid=uid)


# ==================== 账号管理层级 ====================


def _is_developer_pair(actor_role: Optional[str], other_role: Optional[str]) -> bool:
    """开发者对开发者：唯一允许同级操作的一对（遗留 admin 按 developer 算）"""
    return (
        _legacy_equivalent(actor_role or "") == ROLE_DEVELOPER
        and _legacy_equivalent(other_role or "") == ROLE_DEVELOPER
    )


def can_manage(actor_role: Optional[str], target_role: Optional[str]) -> bool:
    """actor 能否管理（停用/改角色/改部门/重置密码）target 账号

    级别必须严格更高，唯一的例外是开发者可以管理其他开发者。
    总裁级别更高，开发者永远动不了总裁；总裁之间同级，互相也管不了。
    """
    return role_level(actor_role) > role_level(target_role) or _is_developer_pair(
        actor_role, target_role
    )


def can_assign_role(actor_role: Optional[str], new_role: Optional[str]) -> bool:
    """actor 能否把某人设成 new_role —— 不能造出比自己更高的账号

    开发者可以再设开发者，但设不了总裁；总裁只能设出比自己低的角色。
    """
    return role_level(actor_role) > role_level(new_role) or _is_developer_pair(
        actor_role, new_role
    )


def manageable_roles(actor_role: Optional[str]) -> Tuple[str, ...]:
    """actor 可以分配出去的角色列表，供前端渲染下拉框"""
    return tuple(r for r in (ROLE_PRESIDENT, ROLE_DEVELOPER, ROLE_AUDITOR,
                             ROLE_MANAGER, ROLE_EMPLOYEE)
                 if can_assign_role(actor_role, r))
