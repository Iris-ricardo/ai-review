import { defineConfig, mergeConfig } from 'vitest/config'
import viteConfig from './vite.config.js'

// R14：组件级验收测试。复用 vite.config.js（含 @vitejs/plugin-vue）以保证
// 测试与构建使用同一套 .vue 解析配置。
// 注意：include 只收集 *.spec.js；既有的 node:test 用例（test/api.test.js）
// 仍由 `npm run test:node`（node --test）执行，二者互不干扰。
export default mergeConfig(viteConfig, defineConfig({
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['src/**/__tests__/**/*.spec.js'],
    setupFiles: ['./src/testing/setup.js'],
    restoreMocks: true,
    clearMocks: true,
  },
}))
