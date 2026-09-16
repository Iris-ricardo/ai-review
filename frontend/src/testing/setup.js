// jsdom 缺少的浏览器 API 补丁（Element Plus 的表格/弹层依赖这些接口）。
// 仅测试环境使用，不进入构建产物；放在 src/testing/ 而非 src/test/，
// 以免被既有的 `node --test`（npm run test:node）当成测试文件收集。
class ObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
  takeRecords() { return [] }
}

if (!globalThis.ResizeObserver) globalThis.ResizeObserver = ObserverStub
if (!globalThis.IntersectionObserver) globalThis.IntersectionObserver = ObserverStub
if (!globalThis.matchMedia) {
  globalThis.matchMedia = () => ({
    matches: false,
    addEventListener() {},
    removeEventListener() {},
    addListener() {},
    removeListener() {},
    dispatchEvent() { return false },
  })
}
if (!globalThis.requestAnimationFrame) {
  globalThis.requestAnimationFrame = callback => setTimeout(() => callback(Date.now()), 0)
  globalThis.cancelAnimationFrame = handle => clearTimeout(handle)
}
if (typeof window !== 'undefined' && !window.scrollTo) window.scrollTo = () => {}
