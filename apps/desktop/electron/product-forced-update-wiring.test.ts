/**
 * 强制更新遮罩的接线回归（阶段 5 · U4）
 *
 * 遮罩本身的行为由渲染层测试覆盖（`src/app/product-mandatory-update-overlay.test.tsx`）。
 * 这里管的是**它有没有被接上**：那几处接线全都是「删掉之后所有单元测试照样
 * 全绿，只有真机上才看得出来」的类型 —— 组件不挂载、退出按钮打不到主进程、
 * 层级被别的遮罩压住，单测里都察觉不到。
 *
 * 所以这个文件读源码。读源码的测试很脆，但脆在「改了要来改测试」，
 * 不脆在「坏了没人知道」，后者才是这几处的真实风险。
 */

import fs from 'node:fs'
import path from 'node:path'

import { describe, expect, it } from 'vitest'

const desktopRoot = path.resolve(__dirname, '..')
const read = (relative: string) => fs.readFileSync(path.join(desktopRoot, relative), 'utf8')

describe('退出应用这条通道是通的', () => {
  it('主进程注册了退出处理器，并且走 app.quit() 而不是硬杀', () => {
    const source = read('electron/main.ts')
    const handler = /ipcMain\.handle\('hermes:updates:quit-app',[\s\S]*?\n\}\)/.exec(source)?.[0]

    expect(handler, '主进程没有注册 hermes:updates:quit-app').toBeTruthy()
    // 断言处理器体里到底调了什么，而不是「没出现 process.exit」——
    // 否定式断言在这里是空话：处理器整个被删掉它也照样通过。
    // app.exit()/process.exit() 会跳过 before-quit 的清理（sidecar、窗口状态），
    // 而用户按的只是「这次先不更新」，不该顺手弄脏下次启动。
    expect(handler).toContain('app.quit()')
    expect(handler).not.toMatch(/process\.exit|app\.exit/)
  })

  it('preload 把它暴露给渲染层', () => {
    expect(read('electron/preload.ts')).toContain("quitApp: () => ipcRenderer.invoke('hermes:updates:quit-app')")
  })
})

describe('遮罩确实被挂上了', () => {
  const wiring = read('src/app/contrib/wiring.tsx')

  it('在品牌构建的遮罩集合里', () => {
    expect(wiring).toContain('<ProductMandatoryUpdateOverlay />')
    expect(wiring).toContain("import { ProductMandatoryUpdateOverlay } from '../product-mandatory-update-overlay'")
  })

  it('排在登录遮罩之后 —— 版本不能用时，先登录再被挡住是白走一趟', () => {
    expect(wiring.indexOf('<ProductMandatoryUpdateOverlay />')).toBeGreaterThan(
      wiring.indexOf('<ProductLoginOverlay />')
    )
  })
})

describe('层级排在正确的一档', () => {
  const styles = read('src/styles.css')

  const rung = (name: string) => {
    const match = new RegExp(`--z-${name}:\\s*(\\d+);`).exec(styles)

    expect(match, `--z-${name} 不在样式表里`).not.toBeNull()

    return Number(match?.[1])
  }

  it('压过启动期的各种遮罩，否则强制更新会被「连接中」盖住', () => {
    expect(rung('forced-update')).toBeGreaterThan(rung('connecting'))
    expect(rung('forced-update')).toBeGreaterThan(rung('setup'))
  })

  it('低于崩溃报告 —— 崩溃信息被更新提示挡住，比反过来糟得多', () => {
    expect(rung('forced-update')).toBeLessThan(rung('crash'))
  })

  it('遮罩用的是这一档，不是随手写的字面量', () => {
    expect(read('src/app/product-mandatory-update-overlay.tsx')).toContain('z-(--z-forced-update)')
  })
})

// 首次运行安装方式选择的门（阶段 6）
//
// 门本身有单测。这里管的是**它有没有被接上**：`branded` 这个开关算出来了却
// 没传下去，所有单测照样全绿，而真机上的表现是登录之后无限 CONNECTING。
describe('品牌构建关掉了首次运行的安装方式选择', () => {
  const source = read('electron/main.ts')

  it('createFirstRunSetupGate 收到了 branded 开关', () => {
    const call = /createFirstRunSetupGate\(\{[\s\S]*?\n {4}\}\)/.exec(source)?.[0]

    expect(call, 'main.ts 里找不到 createFirstRunSetupGate 调用').toBeTruthy()
    expect(call).toContain('branded: Boolean(PRODUCT_POLICY.brandId)')
  })
})
