/**
 * 登录页的「记住密码」与最近账号（仿 QQ）。
 *
 * CUSTOM：品牌版登录页专用，stock Hermes 不用。
 *
 * ## 几条设计取舍
 *
 * - **密码只在主进程里明文存在一瞬间。** 渲染层只拿到「这个账号有没有记住的密码」，
 *   密码框里显示的是固定长度的占位圆点（不暴露真实长度），用户一改动就整框清空，
 *   登录时由主进程自己解出密码去换令牌。开发者工具、渲染层崩溃转储里都看不到它。
 * - **绑在这台电脑、这个系统用户上。** 用 Electron safeStorage（macOS 钥匙串 /
 *   Windows DPAPI）加密，文件拷到别的机器或别的系统账号解不开 —— 论坛上讨论
 *   QQ 记住密码时说的「机器特征码」就是这件事，系统已经替我们做好了。
 *   safeStorage 不可用时**不提供**记住密码，绝不退回明文。
 * - **30 天没成功登录过就作废。** 记住的密码只在每次用它（或手输）登录成功时续期，
 *   长期不用的机器上不会一直躺着一份可还原的密码。
 * - **密码错了就忘掉。** 管理员重置过密码后，记住的旧密码立刻失效，不会反复拿去撞。
 *
 * 设备 API Key 本来就以同样的方式存在本机，而且权限更大、永不过期，所以记住密码
 * 新增的风险主要是「员工在别处复用了同一个密码」。默认不勾选，勾不勾由员工自己定。
 */

import fs from 'node:fs'
import path from 'node:path'

export const SAVED_PASSWORD_TTL_MS = 30 * 24 * 60 * 60 * 1000
export const MAX_SAVED_ACCOUNTS = 5

export interface SavedAccountSummary {
  hasSavedPassword: boolean
  username: string
}

export interface SavedAccountsSnapshot {
  accounts: SavedAccountSummary[]
  /** 这台电脑上能不能安全地记住密码（系统钥匙串可用）。 */
  canRememberPassword: boolean
  /** 上次登录时「记住密码」勾没勾，下次打开登录页照样。 */
  rememberPassword: boolean
}

interface StoredAccount {
  lastLoginAt: number
  /** safeStorage 加密后的密码；没有就是没记住。 */
  secret?: unknown
  /** 记住的密码最近一次被证明正确的时间，过期判断用它。 */
  verifiedAt?: number
  username: string
}

interface StoredFile {
  accounts: StoredAccount[]
  rememberPassword: boolean
}

export interface ProductSavedAccountsOptions {
  canEncrypt: () => boolean
  decrypt: (secret: unknown) => string
  encrypt: (value: string) => unknown
  filePath: string
  now?: () => number
}

function isSafeStorageBlob(secret: unknown): boolean {
  return (
    typeof secret === 'object' &&
    secret !== null &&
    Reflect.get(secret, 'encoding') === 'safeStorage' &&
    typeof Reflect.get(secret, 'value') === 'string'
  )
}

export class ProductSavedAccounts {
  private readonly options: ProductSavedAccountsOptions
  private readonly now: () => number

  constructor(options: ProductSavedAccountsOptions) {
    this.options = options
    this.now = options.now ?? (() => Date.now())
  }

  private read(): StoredFile {
    try {
      const raw = JSON.parse(fs.readFileSync(this.options.filePath, 'utf8'))
      const accounts = Array.isArray(raw?.accounts) ? raw.accounts : []

      return {
        accounts: accounts
          .filter(
            (item: unknown): item is StoredAccount =>
              typeof item === 'object' &&
              item !== null &&
              typeof Reflect.get(item, 'username') === 'string' &&
              Boolean(String(Reflect.get(item, 'username')).trim())
          )
          .slice(0, MAX_SAVED_ACCOUNTS),
        rememberPassword: raw?.rememberPassword === true
      }
    } catch {
      return { accounts: [], rememberPassword: false }
    }
  }

  private write(file: StoredFile): void {
    fs.mkdirSync(path.dirname(this.options.filePath), { recursive: true })
    fs.writeFileSync(this.options.filePath, JSON.stringify(file), { mode: 0o600 })
  }

  private passwordIsLive(account: StoredAccount): boolean {
    return (
      isSafeStorageBlob(account.secret) &&
      typeof account.verifiedAt === 'number' &&
      this.now() - account.verifiedAt < SAVED_PASSWORD_TTL_MS
    )
  }

  private canEncrypt(): boolean {
    try {
      return this.options.canEncrypt()
    } catch {
      return false
    }
  }

  snapshot(): SavedAccountsSnapshot {
    const file = this.read()
    const canRememberPassword = this.canEncrypt()
    let pruned = false

    for (const account of file.accounts) {
      if (account.secret !== undefined && !this.passwordIsLive(account)) {
        delete account.secret
        delete account.verifiedAt
        pruned = true
      }
    }

    if (pruned) {
      this.write(file)
    }

    return {
      accounts: file.accounts.map(account => ({
        hasSavedPassword: canRememberPassword && this.passwordIsLive(account),
        username: account.username
      })),
      canRememberPassword,
      rememberPassword: canRememberPassword && file.rememberPassword
    }
  }

  /** 记住的密码；没有、过期或解不开都返回 null。 */
  password(username: string): string | null {
    const account = this.read().accounts.find(item => item.username === username.trim())

    if (!account || !this.passwordIsLive(account) || !this.canEncrypt()) {
      return null
    }

    try {
      return this.options.decrypt(account.secret) || null
    } catch {
      return null
    }
  }

  /** 登录成功后调用：账号挪到最前，按勾选记住或忘掉密码，并续期。 */
  recordLogin(username: string, password: string, rememberPassword: boolean): void {
    const name = username.trim()

    if (!name) {
      return
    }

    const file = this.read()
    const next: StoredAccount = { lastLoginAt: this.now(), username: name }
    const remember = rememberPassword && this.canEncrypt()

    if (remember && password) {
      try {
        const secret = this.options.encrypt(password)

        if (isSafeStorageBlob(secret)) {
          next.secret = secret
          next.verifiedAt = this.now()
        }
      } catch {
        // 钥匙串这一刻不可用：这次就不记住，登录本身不受影响
      }
    }

    this.write({
      accounts: [next, ...file.accounts.filter(item => item.username !== name)].slice(0, MAX_SAVED_ACCOUNTS),
      rememberPassword: remember
    })
  }

  /** 密码被网关拒绝（多半是管理员重置过）：只忘掉密码，账号留在下拉里。 */
  forgetPassword(username: string): void {
    const file = this.read()
    const account = file.accounts.find(item => item.username === username.trim())

    if (!account || account.secret === undefined) {
      return
    }

    delete account.secret
    delete account.verifiedAt
    this.write(file)
  }

  /** 下拉里点 ×：连账号带密码一起删掉。 */
  remove(username: string): void {
    const file = this.read()

    this.write({ ...file, accounts: file.accounts.filter(item => item.username !== username.trim()) })
  }
}
