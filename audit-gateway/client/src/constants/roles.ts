/**
 * 角色与权限定义
 *
 * ⚠️ 这份表只决定「界面上显示什么」，**不是安全边界**。
 * 真正的校验在网关的 services/permissions.py，
 * 前端漏判只会让用户点了以后收到 10008，后端漏判才是事故。
 *
 * 登录后以服务端下发的 permissions 为准；本地表只给旧版后端做兼容回退。
 */

/** 角色常量 —— 与 server/model/database.py 的 ROLE_* 严格对应 */
export const ROLE = {
  /** 总裁：最高权限，不能被任何人管理 */
  PRESIDENT: 'president',
  /** 开发者：可视组件与权限和总裁一致，唯一区别是不能管理总裁账号 */
  DEVELOPER: 'developer',
  /** 管理部门审计员：只读会话审计 */
  AUDITOR: 'auditor',
  MANAGER: 'manager',
  EMPLOYEE: 'employee',
  /** 模板遗留值，仅兼容历史数据 */
  ADMIN: 'admin',
  OPERATOR: 'operator',
} as const

export type Role = (typeof ROLE)[keyof typeof ROLE]

/** 可分配的角色（不含遗留值） */
export const ASSIGNABLE_ROLES: string[] = [
  ROLE.PRESIDENT,
  ROLE.DEVELOPER,
  ROLE.AUDITOR,
  ROLE.MANAGER,
  ROLE.EMPLOYEE,
]

/**
 * 角色级别 —— 账号管理遵循「只能操作级别严格低于自己的账号」，唯一例外是
 * 开发者可以管理其他开发者。由此推出：总裁可管理开发者；开发者动不了总裁
 */
export const ROLE_LEVELS: Record<string, number> = {
  [ROLE.PRESIDENT]: 100,
  [ROLE.DEVELOPER]: 90,
  [ROLE.ADMIN]: 90,
  [ROLE.AUDITOR]: 60,
  [ROLE.MANAGER]: 40,
  [ROLE.EMPLOYEE]: 10,
  [ROLE.OPERATOR]: 10,
}

export function roleLevel (role: string | null | undefined): number {
  if (!role) {
    return 0
  }
  return ROLE_LEVELS[role] ?? 0
}

/** 角色展示名 */
export const ROLE_LABELS: Record<string, string> = {
  [ROLE.PRESIDENT]: '总裁',
  [ROLE.DEVELOPER]: '开发者',
  [ROLE.AUDITOR]: '管理部门审计',
  [ROLE.MANAGER]: '部门负责人',
  [ROLE.EMPLOYEE]: '普通员工',
  [ROLE.ADMIN]: '开发者（旧）',
  [ROLE.OPERATOR]: '普通员工（旧）',
}

/** 角色标识色，用于 v-chip */
export const ROLE_COLORS: Record<string, string> = {
  [ROLE.PRESIDENT]: 'red-darken-2',
  [ROLE.DEVELOPER]: 'primary',
  [ROLE.AUDITOR]: 'deep-purple',
  [ROLE.MANAGER]: 'teal',
  [ROLE.EMPLOYEE]: 'grey',
  [ROLE.ADMIN]: 'primary',
  [ROLE.OPERATOR]: 'grey',
}

export function roleLabel (role: string | null | undefined): string {
  if (!role) {
    return '未知角色'
  }
  return ROLE_LABELS[role] ?? role
}

export function roleColor (role: string | null | undefined): string {
  if (!role) {
    return 'grey'
  }
  return ROLE_COLORS[role] ?? 'grey'
}

/**
 * 功能权限：权限点 → 允许的角色（镜像 server/services/permissions.py）
 * 注意这里没有 audit:view —— 会话可见性按角色判定，见下方 canViewAudit
 */
export const PERMISSIONS = {
  'dept:view': [ROLE.PRESIDENT, ROLE.DEVELOPER],
  'dept:edit': [ROLE.PRESIDENT, ROLE.DEVELOPER],
  'user:view': [ROLE.PRESIDENT, ROLE.DEVELOPER],
  'user:edit': [ROLE.PRESIDENT, ROLE.DEVELOPER],
  'sso:manage': [ROLE.PRESIDENT, ROLE.DEVELOPER],
  'model:view': [ROLE.PRESIDENT, ROLE.DEVELOPER],
  'model:edit': [ROLE.PRESIDENT, ROLE.DEVELOPER],
  /** 各配置页「选人」控件共用的全量成员搜索（/admin/member/search） */
  'member:search': [ROLE.PRESIDENT, ROLE.DEVELOPER],
  /** 全公司设备 Key 总览与批量吊销 —— 批量吊销等于让一批人当场掉线，与停用账号同级 */
  'apikey:admin': [ROLE.PRESIDENT, ROLE.DEVELOPER],
  'mcp:view': [ROLE.PRESIDENT, ROLE.DEVELOPER],
  'mcp:edit': [ROLE.PRESIDENT, ROLE.DEVELOPER],
  /** 发布桌面端新版本 —— 等于把安装包推给全公司，与改模型路由同级 */
  'release:view': [ROLE.PRESIDENT, ROLE.DEVELOPER],
  'release:edit': [ROLE.PRESIDENT, ROLE.DEVELOPER],
  /** 能打开用量报表页 ≠ 能看全公司，范围由 usage:view:all 单独控制 */
  'usage:view': [ROLE.PRESIDENT, ROLE.DEVELOPER, ROLE.AUDITOR, ROLE.MANAGER],
  'usage:view:all': [ROLE.PRESIDENT, ROLE.DEVELOPER],
  /**
   * 技能安全事件（阶段 7 · S7-C）。拆成两个权限点：
   * :view 是「谁把判定为危险的技能放回去了」，合规那一侧；
   * :sample 是员工自己写的源码，判断一段 curl 是外发密钥还是正常调接口，
   * 是工程判断，交给能读代码的人。
   *
   * 部门负责人暂时不给 —— 这两个点还没有数据范围概念，给了就是全公司可见，
   * 而用量报表那边同样 manager 可见却限死在本部门。详见 permissions.py。
   */
  'skill_security:view': [ROLE.PRESIDENT, ROLE.DEVELOPER, ROLE.AUDITOR],
  'skill_security:sample': [ROLE.PRESIDENT, ROLE.DEVELOPER],
  /**
   * 自有 skill hub（阶段 11 · S11-G）：
   * :view 是技能列表与部门技能指标；
   * :edit 是下架/恢复 —— 下架等于把一个技能从全公司的可安装列表里摘掉，
   * 与推一个安装包同级，所以照 release:edit 的名单。
   *
   * 部门负责人暂时不给，尽管技能指标正是按部门考核的。理由与上面那条一样：
   * 还没有数据范围概念，给了就是全公司可见。详见 permissions.py。
   */
  /**
   * 技能库分层（阶段 21）。2026-08-31 负责人决定给部门负责人开 view/promote ——
   * 主管要在后台筛选本部门技能并发布到公司空间。
   *
   * 开之前先有了数据范围（列表限死本部门、晋级只能晋级自己部门的），否则就是
   * 「给了 manager 就是全公司可见」那个坑。
   *
   * :edit（下架）不给主管：下架是**否决**不是降级，主管手上有「撤回到部门空间」。
   */
  'skill_hub:view': [ROLE.PRESIDENT, ROLE.DEVELOPER, ROLE.AUDITOR, ROLE.MANAGER],
  'skill_hub:promote': [ROLE.PRESIDENT, ROLE.DEVELOPER, ROLE.MANAGER],
  'skill_hub:edit': [ROLE.PRESIDENT, ROLE.DEVELOPER],
  /**
   * 屏蔽词（阶段 18）。不拆 view/edit —— 能看到「哪些词被遮」的人本来就该是能改
   * 它的人。
   *
   * 名单与 model:edit 一致：改屏蔽词看着像内容配置，实际是安全配置。删一个词等于
   * 当场把那条防线关掉，且**已发出的消息不会追溯遮蔽**（脱敏发生在写入那一刻）。
   */
  'redaction:edit': [ROLE.PRESIDENT, ROLE.DEVELOPER],
  /** 提示词发布/回滚；草稿编辑还受部门约束，在下方单独判定 */
  'prompt_policy:publish': [ROLE.PRESIDENT, ROLE.DEVELOPER],
} as const satisfies Record<string, readonly string[]>

export const AUDIT_PERMISSION = 'audit:view'
export const PROMPT_POLICY_VIEW_PERMISSION = 'prompt_policy:view'
export const PROMPT_POLICY_EDIT_PERMISSION = 'prompt_policy:edit'

export type Permission
  = keyof typeof PERMISSIONS
    | typeof AUDIT_PERMISSION
    | typeof PROMPT_POLICY_VIEW_PERMISSION
    | typeof PROMPT_POLICY_EDIT_PERMISSION

/**
 * 管理部门的部门编码。会话审计已改为按角色授权，不再依赖它；
 * 提示词策略里「管理部门负责人」等按部门判定的规则仍以它为锚点。
 */
export const AUDIT_DEPT_CODE = 'exec'

/** 会话审计的角色级别下限：总裁、开发者、管理部门审计可见 */
const AUDIT_MIN_ROLE_LEVEL = ROLE_LEVELS[ROLE.AUDITOR]

/** 遗留角色按同级映射 */
function legacyEquivalent (role: string): string {
  if (role === ROLE.ADMIN) {
    return ROLE.DEVELOPER
  }
  if (role === ROLE.OPERATOR) {
    return ROLE.EMPLOYEE
  }
  return role
}

/** 会话内容可见性：总裁、开发者、管理部门审计三个角色，不看部门（deptCode 仅为兼容保留） */
export function canViewAudit (
  role: string | null | undefined,
  _deptCode?: string | null,
): boolean {
  if (!role) {
    return false
  }
  return roleLevel(role) >= AUDIT_MIN_ROLE_LEVEL
}

/**
 * 提示词策略可见性：总裁、开发者，以及管理部门的审计员/负责人。
 *
 * 与会话审计不同，开发者不需要划入管理部门才能维护系统配置；管理部门审计员只拿
 * view，edit/publish 仍由上面的独立权限点控制。
 */
export function canViewPromptPolicy (
  role: string | null | undefined,
  deptCode: string | null | undefined,
): boolean {
  if (!role) {
    return false
  }
  const effectiveRole = legacyEquivalent(role)
  if (effectiveRole === ROLE.PRESIDENT || effectiveRole === ROLE.DEVELOPER) {
    return true
  }
  return deptCode === AUDIT_DEPT_CODE
    && (effectiveRole === ROLE.AUDITOR || effectiveRole === ROLE.MANAGER)
}

/** 草稿编辑：开发者，或管理部门负责人。总裁只负责发布/回滚，不默认编辑草稿。 */
export function canEditPromptPolicy (
  role: string | null | undefined,
  deptCode: string | null | undefined,
): boolean {
  if (!role) {
    return false
  }
  const effectiveRole = legacyEquivalent(role)
  return effectiveRole === ROLE.DEVELOPER
    || (effectiveRole === ROLE.MANAGER && deptCode === AUDIT_DEPT_CODE)
}

/** 判断是否具备权限点。提示词策略的只读/编辑范围需要额外传部门编码 */
export function roleHasPermission (
  role: string | null | undefined,
  permission: Permission,
  deptCode?: string | null,
  grantedPermissions?: readonly string[],
): boolean {
  if (grantedPermissions) {
    return grantedPermissions.includes(permission)
  }
  if (!role) {
    return false
  }
  if (permission === AUDIT_PERMISSION) {
    return canViewAudit(role, deptCode)
  }
  if (permission === PROMPT_POLICY_VIEW_PERMISSION) {
    return canViewPromptPolicy(role, deptCode)
  }
  if (permission === PROMPT_POLICY_EDIT_PERMISSION) {
    return canEditPromptPolicy(role, deptCode)
  }
  const allowed = PERMISSIONS[permission] as readonly string[]
  return allowed.includes(role) || allowed.includes(legacyEquivalent(role))
}

/** 开发者对开发者：唯一允许同级操作的一对（遗留 admin 按 developer 算） */
function isDeveloperPair (
  actorRole: string | null | undefined,
  otherRole: string | null | undefined,
): boolean {
  return legacyEquivalent(actorRole ?? '') === ROLE.DEVELOPER
    && legacyEquivalent(otherRole ?? '') === ROLE.DEVELOPER
}

/** 能否管理目标账号（停用/改角色/重置密码）—— 级别更高，或开发者对开发者 */
export function canManageRole (
  actorRole: string | null | undefined,
  targetRole: string | null | undefined,
): boolean {
  return roleLevel(actorRole) > roleLevel(targetRole) || isDeveloperPair(actorRole, targetRole)
}

/** 当前角色可以分配出去的角色 */
export function assignableRoles (actorRole: string | null | undefined): string[] {
  return ASSIGNABLE_ROLES.filter(r => canManageRole(actorRole, r))
}
