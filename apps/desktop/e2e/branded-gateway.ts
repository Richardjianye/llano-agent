/**
 * 阶段 4 · CP2：品牌客户端 × 真实 Audit Gateway 的 E2E 夹具。
 *
 * ## 和既有夹具的区别
 *
 * `fixtures.ts` 的 `mockBackend` 用假推理服务，验证的是「Electron → hermes serve
 * → provider → 渲染」这条链能不能通。本夹具**不用任何 mock**：客户端连真实
 * Audit Gateway，网关打真实上游。理由是阶段 4 要证的恰恰是那些 mock 会抹掉的东西
 * —— 鉴权、白名单、审计落库、用量记账、脱敏双通道，全在网关里。
 *
 * 会真的产生上游费用和真实库记录。负责人已确认接受这个成本。
 *
 * ## 账号从哪来
 *
 * **不复用人工账号**，每次开一个专用员工账号：随机密码只存在于本进程内存里，
 * 不落文件、不进日志。理由有两条 ——
 *
 * 1. 阶段 1 的验收里「停用账号」「吊销设备 Key」「重置密码」都是被测行为。
 *    拿人在用的账号跑，测完对方就登不上了。
 * 2. 员工角色能顺带验证权限范围：它进不去报表页、白名单默认拒绝对它同样生效。
 *    用总裁账号跑，这两类问题一个都测不出来。
 *
 * 收尾时把账号停用而不是删除：审计记录要能追溯到这次跑了什么，删账号会让
 * 那批会话失去归属人。
 *
 * ## 需要的环境变量
 *
 *     LLANO_E2E_GATEWAY        默认 http://127.0.0.1:7899
 *     LLANO_E2E_ADMIN_USER     总裁账号（开子账号要 user:edit）
 *     LLANO_E2E_ADMIN_PASSWORD
 *
 * 本地可以从凭据文件导：`set -a; . ~/Desktop/llano-agent-keys; set +a`，
 * 再把 `superadmin_username/password` 映射过来。**不要把凭据写进仓库。**
 */

import { spawnSync } from 'node:child_process'
import * as crypto from 'node:crypto'
import * as fs from 'node:fs'
import * as path from 'node:path'

import type { ElectronApplication, Page } from '@playwright/test'

import { buildAppEnv, createSandbox, launchDesktop, type Sandbox } from './fixtures'

const DESKTOP_ROOT = path.resolve(import.meta.dirname, '..')
const REPO_ROOT = path.resolve(DESKTOP_ROOT, '..', '..')
const SERVER_ROOT = path.join(REPO_ROOT, 'audit-gateway', 'server')

const fsExists = (target: string): boolean => fs.existsSync(target)

export const GATEWAY_ORIGIN = process.env.LLANO_E2E_GATEWAY || 'http://127.0.0.1:7899'

/** 员工能拿到的三个模型。多模态只有豆包这一个（决策 #29，base64 内联）。 */
export const TEXT_MODEL = 'deepseek-v4-flash'
export const MULTIMODAL_MODEL = 'doubao-seed-2-0-lite-260428'

export interface E2EAccount {
  uid: string
  username: string
  password: string
}

export interface BrandedGatewayFixture {
  app: ElectronApplication
  page: Page
  sandbox: Sandbox
  account: E2EAccount
  /** 本次开跑前网关已记的请求总数，用来核对「这次记了几笔」 */
  baseline: number
  cleanup: () => Promise<void>
}

// ─── 网关管理端 ───────────────────────────────────────────────────────────

function requireAdminCredentials(): { user: string; password: string } {
  const user = process.env.LLANO_E2E_ADMIN_USER
  const password = process.env.LLANO_E2E_ADMIN_PASSWORD

  if (!user || !password) {
    throw new Error(
      '缺少 LLANO_E2E_ADMIN_USER / LLANO_E2E_ADMIN_PASSWORD。' +
        '这两个变量用于开专用测试账号，请从本地凭据文件导入，不要写进仓库。',
    )
  }

  return { user, password }
}

async function gatewayPost<T>(path: string, body: unknown, token?: string): Promise<T> {
  const response = await fetch(`${GATEWAY_ORIGIN}${path}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(body),
  })

  const payload = (await response.json()) as { code: number; message: string; data: T }

  // 管理端错误也走 {code,message,data} 信封并返回 HTTP 200 —— 只判 response.ok
  // 会把每一个业务错误都当成成功。同一个坑在 e2e/reconcile 的附件检查里踩过一次。
  if (payload.code !== 0) {
    throw new Error(`${path} 返回 code=${payload.code}：${payload.message}`)
  }

  return payload.data
}

async function gatewayGet<T>(path: string, token: string): Promise<T> {
  const response = await fetch(`${GATEWAY_ORIGIN}${path}`, {
    headers: { Authorization: `Bearer ${token}` },
  })
  const payload = (await response.json()) as { code: number; message: string; data: T }

  if (payload.code !== 0) {
    throw new Error(`${path} 返回 code=${payload.code}：${payload.message}`)
  }

  return payload.data
}

async function adminLogin(): Promise<string> {
  const { user, password } = requireAdminCredentials()
  const data = await gatewayPost<{ access_token: string }>('/admin/user/login', {
    username: user,
    password,
  })
  return data.access_token
}

/**
 * 开一个本次专用的员工账号。
 *
 * 用户名带时间戳与随机后缀：并行 worker 同时开账号不会撞名，
 * 而撞名的表现是 409 冲突，排查起来像是"权限出问题了"。
 */
async function provisionAccount(token: string): Promise<E2EAccount> {
  const suffix = `${Date.now().toString(36)}${crypto.randomBytes(2).toString('hex')}`
  const username = `回归${suffix}`
  // 只活在本进程内存里：不落盘、不打日志，用完账号即停用
  const password = `E2e-${crypto.randomBytes(12).toString('base64url')}`

  const data = await gatewayPost<{ uid: string }>(
    '/admin/account/create',
    { username, password, permission: 'employee', status: 1 },
    token,
  )

  return { uid: data.uid, username, password }
}

/** 停用而非删除：这次跑出来的会话记录仍要能追溯到归属人。 */
async function disableAccount(token: string, uid: string): Promise<void> {
  await gatewayPost('/admin/account/status', { uid, status: 0 }, token)
}

async function currentRequestTotal(token: string): Promise<number> {
  const data = await gatewayGet<{ total: { requests: number } }>(
    '/admin/usage/summary',
    token,
  )
  return data.total.requests
}

// ─── 对账（复用 Python 那份唯一口径，见 CP1）─────────────────────────────

export interface ReconcileResult {
  ok: boolean
  failures: string[]
  warnings: string[]
  checks: { ok: boolean; title: string; detail: string }[]
}

/**
 * 调 `scripts/e2e_reconcile.py` 核对审计 / 附件 / 查阅日志 / 用量。
 *
 * 为什么不在 TypeScript 里重写一遍：口径必须和网关侧脚本**逐条一致**。
 * 两套实现各自绿着、却在核对同一件事时用了两套标准，比不测更危险 ——
 * 那种"都通过了"会让人真的相信账是对的。
 *
 * 用**总裁账号**对账而不是测试账号：会话内容按部门授权（决策 #8、#17），
 * 员工账号查不了审计接口，全会是 403。
 */
export function reconcile(options: {
  expectRequests?: number
  baseline?: number
  sentinel?: string
} = {}): ReconcileResult {
  const { user, password } = requireAdminCredentials()

  const args = [
    path.join(SERVER_ROOT, 'scripts', 'e2e_reconcile.py'),
    '--base-url', GATEWAY_ORIGIN,
    '--user', user,
    '--password', password,
    '--json',
  ]

  if (options.expectRequests !== undefined) {
    args.push('--expect-requests', String(options.expectRequests))
  }
  if (options.baseline !== undefined) {
    args.push('--baseline', String(options.baseline))
  }
  if (options.sentinel) {
    args.push('--sentinel', options.sentinel)
  }

  const result = spawnSync('python3', args, {
    cwd: SERVER_ROOT,
    encoding: 'utf8',
    // 真实上游偶有慢响应，给足时间；卡住比误报失败好排查
    timeout: 300_000,
  })

  if (!result.stdout?.trim()) {
    throw new Error(
      `对账脚本没有输出。exit=${result.status} stderr=${result.stderr?.slice(0, 500)}`,
    )
  }

  return JSON.parse(result.stdout.trim()) as ReconcileResult
}

// ─── 夹具 ────────────────────────────────────────────────────────────────

/**
 * 把 Electron 主进程重新打成 **dev bundle**，然后才启动。
 *
 * 为什么必须这么做：`npm run build` 走的是 production bundle，
 * `scripts/bundle-electron-main.mjs` 会在编译期把
 * `process.env.HERMES_DESKTOP_IS_PACKAGED=true` 烧进产物（有意设计，
 * 好让未打包的 `electron .` 表现得像打包版）。于是 `main.ts` 里
 * `PRODUCT_MANAGED_CONFIG_DIR` 走 `process.resourcesPath` 分支 ——
 * 真打包时 electron-builder 会把 `build/managed-config` 复制到那儿，
 * 但**未打包跑 production bundle 时那个目录不存在**，登录时报
 * "Missing generated product policy for development: …/Electron.app/Contents/Resources/managed-config"。
 *
 * 这里不去加一个"托管目录环境变量覆盖"来绕开 —— 托管目录定义的正是产品的
 * 能力锁定策略，给它开一个任意路径的入口，等于把 CP4 矩阵守的那些能力
 * 全部变成可绕过。dev bundle 是既有 e2e 夹具本来就在用的形态
 * （见 fixtures.ts 顶部说明），打包版另有 launch-packaged-app.spec.ts 覆盖。
 */
function ensureDevMainBundle(): void {
  const managed = path.join(DESKTOP_ROOT, 'build', 'managed-config', 'config.yaml')

  if (!fsExists(managed)) {
    throw new Error(
      `缺少托管策略 ${managed}。先跑 npm run build（其 prebuild 会生成它）。`,
    )
  }

  const result = spawnSync(
    'node',
    [path.join(DESKTOP_ROOT, 'scripts', 'bundle-electron-main.mjs'), '--dev'],
    { cwd: DESKTOP_ROOT, encoding: 'utf8' },
  )

  if (result.status !== 0) {
    throw new Error(`重打 dev bundle 失败：${result.stderr?.slice(0, 500)}`)
  }
}

/**
 * 起一个已登录的品牌客户端。
 *
 * 前置：`npm run build`（`prebuild` 会跑 prepare-product-environment.mjs
 * 生成托管 config.yaml），且 Audit Gateway 已在 GATEWAY_ORIGIN 上运行。
 */
export async function setupBrandedGateway(): Promise<BrandedGatewayFixture> {
  ensureDevMainBundle()

  const token = await adminLogin()
  const account = await provisionAccount(token)
  const baseline = await currentRequestTotal(token)

  const sandbox = createSandbox('branded')
  // 刻意**不写** config.yaml：品牌构建的 provider / base_url 来自托管目录
  // （electron/main.ts 注入 HERMES_MANAGED_DIR），个人配置压不过它。
  // 这里写一份反而会把"托管层到底生不生效"这个问题掩盖掉。
  const env = buildAppEnv(sandbox)
  const { app, page } = await launchDesktop(env)

  await login(page, account)

  return {
    app,
    page,
    sandbox,
    account,
    baseline,
    cleanup: async () => {
      await app.close().catch(() => undefined)
      sandbox.cleanup()
      await disableAccount(token, account.uid).catch(() => undefined)
    },
  }
}

/** 走真实登录页，不注入本地会话 —— 登录本身就是被测链路的第一环。 */
export async function login(page: Page, account: E2EAccount, timeoutMs = 90_000): Promise<void> {
  const username = page.locator('#product-username')
  await username.waitFor({ state: 'visible', timeout: timeoutMs })

  await username.fill(account.username)
  await page.locator('#product-password').fill(account.password)
  await page.locator('form button[type="submit"]').click()

  // 登录成功的判据是登录页消失，而不是"看见了聊天框"：
  // sidecar 起来还要一会儿，用聊天框做判据会把启动耗时算进登录失败里。
  await username.waitFor({ state: 'detached', timeout: timeoutMs })
}
