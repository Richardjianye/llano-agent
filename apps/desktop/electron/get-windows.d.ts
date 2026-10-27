/**
 * `get-windows` 的类型垫片 —— 这个包在品牌构建里被**故意移除**了。
 *
 * 移除见 87027c79a9：它是 node-gyp 从源码编译的原生模块，是 Windows 包无法在
 * macOS 上交叉构建的**唯一**一道墙（顺带还拖进 103 个 npm 包）。配套的能力位
 * `product_policy.WINDOW_ENUMERATION` 也关掉了，两处缺一不可：只关能力位还是
 * 编不了，只删依赖则模型会去调一个永远只回错误的工具。
 *
 * 运行期不需要这个文件：`loadGetWindows()` 的 `import('get-windows')` 包在
 * try/catch 里，解析失败会走 `return null`，而调用方本来就把 null 当成
 * 「枚举不可用」的正常分支（Wayland、缺 xprop、addon 加载失败走的是同一条路）。
 *
 * 坏的只有类型解析：`tsc -p tsconfig.electron.json` 报 TS2307，`npm run typecheck`
 * 因此在这条分支上一直是红的。这里补一份声明，形状照抄 window-below.ts 里已有的
 * `GetWindowsModule`（按 get-windows 9.3.0 的 API）。
 *
 * ⚠️ 哪天真把依赖装回来，TS 会因为重复声明报错 —— 那是**故意**的，是提醒来删掉
 * 这个文件的信号，别去改成 `declare module` 之外的写法把它压下去。
 */
declare module 'get-windows' {
  export function openWindows(options?: {
    accessibilityPermission?: boolean
    screenRecordingPermission?: boolean
  }): Promise<
    Array<{
      bounds?: { height?: number; width?: number; x?: number; y?: number }
      id?: number
      owner?: { name?: string; processId?: number }
      title?: string
    }>
  >
}
