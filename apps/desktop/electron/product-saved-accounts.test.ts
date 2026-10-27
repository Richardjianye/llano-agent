import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, describe, expect, it } from 'vitest'

import { MAX_SAVED_ACCOUNTS, ProductSavedAccounts, SAVED_PASSWORD_TTL_MS } from './product-saved-accounts'

const dirs: string[] = []

afterEach(() => {
  for (const dir of dirs.splice(0)) {
    fs.rmSync(dir, { force: true, recursive: true })
  }
})

/** 模拟 safeStorage：「加密」只在同一台机器（同一个 key）上解得开。 */
function store({ canEncrypt = true, machine = 'mac-1', now = (): number => 1_000_000 } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'saved-accounts-'))
  dirs.push(dir)
  const filePath = path.join(dir, 'product-saved-accounts.json')

  const make = (m = machine, clock = now) =>
    new ProductSavedAccounts({
      canEncrypt: () => canEncrypt,
      decrypt: secret => {
        const value = String((secret as { value: string }).value)
        const [owner, plain] = Buffer.from(value, 'base64').toString('utf8').split('\u0000')

        if (owner !== m) {
          throw new Error('cannot decrypt on this machine')
        }

        return plain
      },
      encrypt: value => ({
        encoding: 'safeStorage',
        value: Buffer.from(`${m}\u0000${value}`, 'utf8').toString('base64')
      }),
      filePath,
      now: clock
    })

  return { filePath, make, saved: make() }
}

describe('记住密码', () => {
  it('勾选后记住，渲染层只看到「有没有」，拿不到密码本身', () => {
    const { filePath, saved } = store()

    saved.recordLogin('demo-admin', 's3cret!', true)

    expect(saved.snapshot()).toEqual({
      accounts: [{ hasSavedPassword: true, username: 'demo-admin' }],
      canRememberPassword: true,
      rememberPassword: true
    })
    expect(saved.password('demo-admin')).toBe('s3cret!')
    // 落盘的是密文
    expect(fs.readFileSync(filePath, 'utf8')).not.toContain('s3cret!')
  })

  it('不勾选只记账号，并把之前记住的密码一起忘掉', () => {
    const { saved } = store()

    saved.recordLogin('demo-admin', 's3cret!', true)
    saved.recordLogin('demo-admin', 's3cret!', false)

    expect(saved.snapshot().accounts).toEqual([{ hasSavedPassword: false, username: 'demo-admin' }])
    expect(saved.snapshot().rememberPassword).toBe(false)
    expect(saved.password('demo-admin')).toBeNull()
  })

  it('系统钥匙串不可用时不提供记住密码，也绝不退回明文', () => {
    const { filePath, saved } = store({ canEncrypt: false })

    saved.recordLogin('demo-admin', 's3cret!', true)

    expect(saved.snapshot().canRememberPassword).toBe(false)
    expect(saved.password('demo-admin')).toBeNull()
    expect(fs.readFileSync(filePath, 'utf8')).not.toContain('s3cret!')
  })

  it('文件拷到别的机器上解不开', () => {
    const { make, saved } = store()

    saved.recordLogin('demo-admin', 's3cret!', true)

    expect(make('other-machine').password('demo-admin')).toBeNull()
  })

  it('30 天没成功登录过就作废，下次打开登录页不再预填', () => {
    let clock = 1_000_000
    const { make } = store()
    const saved = make('mac-1', () => clock)

    saved.recordLogin('demo-admin', 's3cret!', true)
    clock += SAVED_PASSWORD_TTL_MS - 1
    expect(saved.password('demo-admin')).toBe('s3cret!')

    clock += 2
    expect(saved.password('demo-admin')).toBeNull()
    expect(saved.snapshot().accounts).toEqual([{ hasSavedPassword: false, username: 'demo-admin' }])
  })

  it('每次登录成功都续期', () => {
    let clock = 1_000_000
    const { make } = store()
    const saved = make('mac-1', () => clock)

    saved.recordLogin('demo-admin', 's3cret!', true)
    clock += SAVED_PASSWORD_TTL_MS - 10
    saved.recordLogin('demo-admin', 's3cret!', true)
    clock += SAVED_PASSWORD_TTL_MS - 10

    expect(saved.password('demo-admin')).toBe('s3cret!')
  })

  it('密码被拒后只忘掉密码，账号仍在下拉里', () => {
    const { saved } = store()

    saved.recordLogin('demo-admin', 'old', true)
    saved.forgetPassword('demo-admin')

    expect(saved.snapshot().accounts).toEqual([{ hasSavedPassword: false, username: 'demo-admin' }])
  })
})

describe('最近账号', () => {
  it('最近登录的排最前，最多留几个', () => {
    const { saved } = store()

    for (let index = 0; index < MAX_SAVED_ACCOUNTS + 2; index += 1) {
      saved.recordLogin(`user-${index}`, 'p', false)
    }

    saved.recordLogin('user-3', 'p', false)

    const names = saved.snapshot().accounts.map(item => item.username)

    expect(names).toHaveLength(MAX_SAVED_ACCOUNTS)
    expect(names[0]).toBe('user-3')
    expect(new Set(names).size).toBe(names.length)
  })

  it('点 × 连账号带密码一起删', () => {
    const { saved } = store()

    saved.recordLogin('demo-admin', 's3cret!', true)
    saved.recordLogin('悟空', 'p', false)
    saved.remove('demo-admin')

    expect(saved.snapshot().accounts.map(item => item.username)).toEqual(['悟空'])
    expect(saved.password('demo-admin')).toBeNull()
  })

  it('文件损坏时当作空列表，不影响登录', () => {
    const { filePath, saved } = store()

    fs.writeFileSync(filePath, '{not json')

    expect(saved.snapshot().accounts).toEqual([])
    saved.recordLogin('demo-admin', 'p', false)
    expect(saved.snapshot().accounts).toHaveLength(1)
  })
})
