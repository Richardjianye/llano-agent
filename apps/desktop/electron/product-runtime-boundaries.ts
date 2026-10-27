import path from 'node:path'

interface ProductHermesHomeOptions {
  brandId: string
  environment: string
  explicitHermesHome?: string
  productUserData: string
  userDataOverride?: string
}

/** Resolve the data root owned by a branded desktop without touching stock ~/.hermes. */
export function brandedHermesHome({
  brandId,
  environment,
  explicitHermesHome,
  productUserData,
  userDataOverride
}: ProductHermesHomeOptions): string | null {
  if (explicitHermesHome?.trim()) {
    return explicitHermesHome.trim()
  }

  if (userDataOverride?.trim()) {
    return path.join(path.resolve(userDataOverride), 'hermes-home')
  }

  if (!brandId.trim()) {
    return null
  }

  return path.join(productUserData, 'hermes-home', environment)
}

export function productDeepLinkProtocol(brandId: string, configuredProtocol: string): string {
  return brandId.trim() ? configuredProtocol.trim() : 'hermes'
}

/**
 * 品牌构建只跑我们自己签出来、随包分发的 sidecar。
 *
 * 上游的后端解析阶梯有两级是「用户装的」：PATH 上的 `hermes`、系统 Python 里的
 * `hermes_cli`。这两级排在 fail-closed 守卫**前面**，所以一台同时装了原版 Hermes
 * 的机器上，品牌构建会安安静静地把上游 CLI 拉起来 —— 阶段 1—5 做的那些定制
 * （禁掉 `hermes update`、关掉 nous_services、脱敏、全量审计）在那份二进制里
 * 一条都没有，而日志里不会有任何异常。
 *
 * 跟「登录后无限 CONNECTING」是同一类毛病：守卫写对了，但被一道上游的门挡在前面。
 */
export function allowsUserInstalledBackend(brandId: string): boolean {
  return !brandId.trim()
}

/**
 * 开发期的后端覆盖（`HERMES_DESKTOP_HERMES_ROOT` 指向任意 checkout）在 dev 下要留着，
 * 但它是一个**用户可写的环境变量**：装好的品牌包里放行它，等于把审计边界交给用户。
 * 未打包时照旧，打包后的品牌构建一律不认。
 */
export function allowsDeveloperBackendOverride(brandId: string, isPackaged: boolean): boolean {
  return !brandId.trim() || !isPackaged
}

interface ProductSidecarOptions {
  appRoot: string
  brandId: string
  exists: (candidate: string) => boolean
  isPackaged: boolean
  platform: NodeJS.Platform
  resourcesPath: string
}

/** 随包分发的 sidecar 可执行文件名（不含扩展名）。内部名字，不出现在界面上。 */
export const PRODUCT_SIDECAR_NAME = 'llano-sidecar'

/**
 * 品牌构建里唯一合法的后端来源：随安装包一起分发、我们自己构建签名的 sidecar。
 *
 * 打包后在 `<resources>/sidecar/`（electron-builder 的 extraResources），
 * 开发时在 `<appRoot>/build/sidecar/` —— 后者让 `npm run dev` 也能跑冻结产物，
 * 不然「冻结之后才会炸」的那一类问题（静态分析少收依赖、按 __file__ 找资源）
 * 要等到打包才暴露。
 *
 * 找不到就返回 null，让阶梯落到 fail-closed 那一级。**不回落到用户装的运行时** ——
 * 那正是 allowsUserInstalledBackend 挡掉的事。
 */
export function productSidecarCommand({
  appRoot,
  brandId,
  exists,
  isPackaged,
  platform,
  resourcesPath
}: ProductSidecarOptions): string | null {
  if (!brandId.trim()) {
    return null
  }

  const filename = platform === 'win32' ? `${PRODUCT_SIDECAR_NAME}.exe` : PRODUCT_SIDECAR_NAME
  const root = isPackaged ? resourcesPath : path.join(appRoot, 'build')
  const candidate = path.join(root, 'sidecar', filename)

  return exists(candidate) ? candidate : null
}

/**
 * 随包 sidecar 自带的 skills 目录。
 *
 * 上游用 `Path(__file__).parent.parent / "skills"` 定位它，冻结之后那条路径是错的
 * （指向 PyInstaller 的 `_internal` 里面）。上游给打包方留了 `HERMES_BUNDLED_SKILLS`
 * 这个正式口子，我们就走它，别去跟 `__file__` 较劲。
 */
export function productSidecarBundledSkills(sidecarCommand: string): string {
  return path.join(path.dirname(sidecarCommand), 'skills')
}

/**
 * 随包 sidecar 自带的静态文案目录（`agent/i18n.py` 的 `HERMES_BUNDLED_LOCALES`）。
 *
 * 跟 skills 同一个道理：冻结之后上游按 `<repo-root>/locales` 找不到，而上游给
 * 打包方留了口子。只发 zh + en，见 scripts/build-sidecar.mjs 的 SHIPPED_LOCALES。
 */
export function productSidecarBundledLocales(sidecarCommand: string): string {
  return path.join(path.dirname(sidecarCommand), 'locales')
}
