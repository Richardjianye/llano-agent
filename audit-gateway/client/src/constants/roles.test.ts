import { describe, expect, it } from 'vitest'
import {
  assignableRoles,
  canManageRole,
  canViewAudit,
  ROLE,
  roleHasPermission,
} from '@/constants/roles'

describe('roleHasPermission', () => {
  it('allows only president and developer to manage SSO clients', () => {
    expect(roleHasPermission(ROLE.PRESIDENT, 'sso:manage')).toBe(true)
    expect(roleHasPermission(ROLE.DEVELOPER, 'sso:manage')).toBe(true)
    expect(roleHasPermission(ROLE.AUDITOR, 'sso:manage')).toBe(false)
    expect(roleHasPermission(ROLE.MANAGER, 'sso:manage')).toBe(false)
  })

  it('uses server-granted permissions when a permission list is available', () => {
    expect(roleHasPermission(ROLE.PRESIDENT, 'user:view', 'exec', [])).toBe(false)
    expect(
      roleHasPermission(ROLE.EMPLOYEE, 'user:view', null, ['user:view']),
    ).toBe(true)
  })

  it('keeps prompt policy view, draft editing, and publishing separate', () => {
    const permissions = [
      'prompt_policy:view',
      'prompt_policy:edit',
      'prompt_policy:publish',
    ] as const

    expect(permissions.map(permission =>
      roleHasPermission(ROLE.DEVELOPER, permission),
    )).toEqual([true, true, true])

    // 总裁只发布/回滚，不默认改草稿。
    expect(permissions.map(permission =>
      roleHasPermission(ROLE.PRESIDENT, permission),
    )).toEqual([true, false, true])

    // 管理部门负责人可以审阅并维护草稿，但不能发布。
    expect(permissions.map(permission =>
      roleHasPermission(ROLE.MANAGER, permission, 'exec'),
    )).toEqual([true, true, false])

    // 管理部门审计员严格只读。
    expect(permissions.map(permission =>
      roleHasPermission(ROLE.AUDITOR, permission, 'exec'),
    )).toEqual([true, false, false])
  })

  it('does not grant prompt policy access to the same roles outside the exec department', () => {
    expect(roleHasPermission(ROLE.AUDITOR, 'prompt_policy:view', 'finance')).toBe(false)
    expect(roleHasPermission(ROLE.MANAGER, 'prompt_policy:view', 'finance')).toBe(false)
    expect(roleHasPermission(ROLE.MANAGER, 'prompt_policy:edit', 'finance')).toBe(false)
  })

  it('still treats server permissions as authoritative for prompt policy actions', () => {
    expect(roleHasPermission(
      ROLE.PRESIDENT,
      'prompt_policy:publish',
      'exec',
      [],
    )).toBe(false)
    expect(roleHasPermission(
      ROLE.EMPLOYEE,
      'prompt_policy:edit',
      null,
      ['prompt_policy:edit'],
    )).toBe(true)
  })
})

describe('audit:view is granted by role, not department', () => {
  it('grants president, developer and auditor regardless of department', () => {
    for (const role of [ROLE.PRESIDENT, ROLE.DEVELOPER, ROLE.AUDITOR]) {
      for (const dept of ['exec', 'finance', null]) {
        expect(canViewAudit(role, dept)).toBe(true)
        expect(roleHasPermission(role, 'audit:view', dept)).toBe(true)
      }
    }
  })

  it('never grants manager or employee, even inside the exec department', () => {
    for (const role of [ROLE.MANAGER, ROLE.EMPLOYEE, ROLE.OPERATOR]) {
      expect(canViewAudit(role, 'exec')).toBe(false)
    }
  })
})

describe('account management hierarchy', () => {
  it('lets developers manage other developers but never the president', () => {
    expect(canManageRole(ROLE.DEVELOPER, ROLE.DEVELOPER)).toBe(true)
    expect(canManageRole(ROLE.ADMIN, ROLE.DEVELOPER)).toBe(true)
    expect(canManageRole(ROLE.DEVELOPER, ROLE.PRESIDENT)).toBe(false)
    expect(canManageRole(ROLE.PRESIDENT, ROLE.DEVELOPER)).toBe(true)
    expect(canManageRole(ROLE.PRESIDENT, ROLE.PRESIDENT)).toBe(false)
    expect(canManageRole(ROLE.AUDITOR, ROLE.AUDITOR)).toBe(false)
  })

  it('lets developers assign every role except president', () => {
    expect(assignableRoles(ROLE.DEVELOPER)).toEqual([
      ROLE.DEVELOPER,
      ROLE.AUDITOR,
      ROLE.MANAGER,
      ROLE.EMPLOYEE,
    ])
    expect(assignableRoles(ROLE.PRESIDENT)).not.toContain(ROLE.PRESIDENT)
  })
})
