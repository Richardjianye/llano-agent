import type { TestProjectConfiguration } from 'vitest/config';
import { defineConfig } from 'vitest/config'

import { loadProductEnvironment } from './scripts/product-environment.mjs'

const reactUi: TestProjectConfiguration = {
  extends: './vite.config.ts',
  test: {
    name: 'ui',
    environment: 'jsdom',
    setupFiles: ['./vitest.setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    globals: true,
    // The first test in each file pays jsdom env init + full module transform,
    // which can exceed vitest's 5000ms default under CI/load. 15s gives the
    // cold start headroom without masking genuinely hung tests.
    testTimeout: 15_000
  }
}

const electronNative: TestProjectConfiguration = {
  // 主进程模块里 product-policy.ts 读的是构建期注入的 __LLANO_PRODUCT_POLICY__。
  // 不在这里注入的话，任何 import 到它的测试都会以
  // "__LLANO_PRODUCT_POLICY__ is not defined" 挂掉 —— 那是构建配置缺一项，
  // 却长得像被测代码坏了。用与 vite.config.ts 同一个 loader，两边不会漂。
  define: {
    __LLANO_PRODUCT_POLICY__: JSON.stringify(loadProductEnvironment())
  },
  test: {
    name: 'electron',
    environment: 'node',
    // .contract.mjs 也要收进来。这些文件之前用的是 node:test，跑得起来但
    // **没有任何 runner 会跑它们** —— npm test 不含、CI 不含，只有手动
    // `node --test` 才会执行。19 条盯着品牌身份的断言就这么长期空转着。
    include: ['electron/**/*.test.ts', 'scripts/**.{test,contract}.{ts,mjs}']
  }
}

export default defineConfig({
  test: {
    projects: [reactUi, electronNative]
  }
})
