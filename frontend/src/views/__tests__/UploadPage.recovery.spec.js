// R14：UploadPage「任务恢复与轮询边界」组件级验收测试。真实挂载组件（Element Plus + vue-router），
// 只 mock ../services/api 的网络函数，classifyError / ApiError / getCurrentUser 走真实实现。
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import ElementPlus from 'element-plus'
import UploadPage from '../UploadPage.vue'

vi.mock('../../services/api', async (importOriginal) => {
  const actual = await importOriginal()
  return {
    ...actual,
    getRulesets: vi.fn(),
    listReviews: vi.fn(),
    getReview: vi.fn(),
    createReview: vi.fn(),
    uploadDocument: vi.fn(),
    cancelReview: vi.fn(),
  }
})

const api = await import('../../services/api')
const { ApiError } = api

const USER_A = 'user-a'
const USER_B = 'user-b'
const KEY_A = `activeReviewId:${USER_A}`
const KEY_B = `activeReviewId:${USER_B}`
const TAB_KEY_A = `${KEY_A}:tab`
const LEGACY_KEY = 'activeReviewId'

const testRouter = createRouter({
  history: createMemoryHistory(),
  routes: [
    { path: '/', component: { template: '<div />' } },
    { path: '/result/:taskId', component: { template: '<div />' } },
  ],
})

function signIn(userId, extra = {}) {
  sessionStorage.setItem('currentUser', JSON.stringify({
    id: userId, username: userId, role_label: '普通用户', capabilities: [], ...extra,
  }))
}

function reviewData(overrides = {}) {
  return {
    review_id: 'review-a',
    status: 'running',
    progress: 10,
    stage: 'rules',
    current_rule: '',
    rules_completed: 1,
    rules_total: 10,
    rule_statuses: {},
    error: '',
    conclusion: '',
    last_activity_at: '',
    ...overrides,
  }
}

function mountPage() {
  return mount(UploadPage, {
    global: { plugins: [ElementPlus, testRouter] },
    attachTo: document.body,
  })
}

// 不依赖真实计时器的微任务清空（fake timers 下 flushPromises 会挂住）
async function settle(times = 24) {
  for (let index = 0; index < times; index += 1) await Promise.resolve()
}

function buttonByText(wrapper, text) {
  const found = wrapper.findAll('button').find(item => item.text().includes(text))
  if (!found) throw new Error(`未找到按钮：${text}`)
  return found
}

// 页面整体可见文本（含 Element Plus 的 ElMessage 吐司——它挂在 document.body 上，
// 不在 wrapper 内部，所以“提交失败”这类一次性提示必须在这里断言）
function pageText() {
  return document.body.textContent || ''
}

function networkError() {
  return new ApiError('网络连接失败：Failed to fetch', { status: 0 })
}

async function selectFileAndConsent(wrapper) {
  const input = wrapper.find('input[type="file"]')
  const file = new File(['%PDF-1.4 synthetic'], '申报书.pdf', { type: 'application/pdf' })
  Object.defineProperty(input.element, 'files', { value: [file], configurable: true })
  await input.trigger('change')
  const consent = wrapper.find('.consent input[type="checkbox"]')
  await consent.setValue(true)
  await settle()
}

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  for (const name of ['getRulesets', 'listReviews', 'getReview', 'createReview', 'uploadDocument', 'cancelReview']) {
    api[name].mockReset()
  }
  api.getRulesets.mockResolvedValue([{ id: 'campus_general_v1', name: '默认规则集', rule_count: 3 }])
  api.listReviews.mockResolvedValue([])
  api.getReview.mockResolvedValue(reviewData())
  api.uploadDocument.mockResolvedValue({ document_id: 'doc-1' })
  api.createReview.mockResolvedValue({ review_id: 'review-new', status: 'queued' })
  api.cancelReview.mockResolvedValue(reviewData({ status: 'cancelled' }))
})

afterEach(() => {
  vi.useRealTimers()
  document.body.innerHTML = ''
})

describe('R14-1/3 恢复失败时的键保留与用户隔离', () => {
  it('恢复时断网：保留该用户的恢复键、给出可重试提示，点击“重试”会真正重新请求', async () => {
    signIn(USER_A)
    localStorage.setItem(KEY_A, 'review-a')
    api.getReview.mockRejectedValueOnce(networkError())

    const wrapper = mountPage()
    await settle()

    expect(api.getReview).toHaveBeenCalledWith('review-a')
    // 键保留（未被删除）
    expect(localStorage.getItem(KEY_A)).toBe('review-a')
    expect(sessionStorage.getItem(TAB_KEY_A)).toBe('review-a')
    // 用户可见：任务编号仍在，状态未知，且有可重试提示
    expect(wrapper.text()).toContain('任务 review-a')
    expect(wrapper.text()).toContain('任务状态未知')
    expect(wrapper.text()).toContain('无法获取任务状态')
    expect(wrapper.text()).toContain('网络连接失败')
    expect(wrapper.text()).toContain('网络或服务端暂时不可用')

    // “重试”是真实行为：点击后再次调用 getReview，并恢复正常展示
    api.getReview.mockResolvedValueOnce(reviewData({ review_id: 'review-a', progress: 33 }))
    await buttonByText(wrapper, '重试').trigger('click')
    await settle()

    expect(api.getReview).toHaveBeenCalledTimes(2)
    expect(api.getReview).toHaveBeenLastCalledWith('review-a')
    expect(wrapper.text()).not.toContain('无法获取任务状态')
    expect(wrapper.text()).toContain('运行中')
    expect(wrapper.text()).toContain('33')
    wrapper.unmount()
  })

  it('恢复时 5xx：保留恢复键，不自动反复重试（等待用户重试）', async () => {
    signIn(USER_A)
    localStorage.setItem(KEY_A, 'review-a')
    api.getReview.mockRejectedValueOnce(new ApiError('内部服务器错误', { status: 500 }))

    const wrapper = mountPage()
    await settle()

    expect(localStorage.getItem(KEY_A)).toBe('review-a')
    expect(wrapper.text()).toContain('无法获取任务状态')
    expect(wrapper.text()).toContain('内部服务器错误')
    expect(api.getReview).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('恢复时请求超时：保留恢复键（超时同样属于瞬时错误）', async () => {
    signIn(USER_A)
    localStorage.setItem(KEY_A, 'review-a')
    api.getReview.mockRejectedValueOnce(new ApiError('请求超时，请检查后端服务', { status: 0, timedOut: true }))

    const wrapper = mountPage()
    await settle()

    expect(localStorage.getItem(KEY_A)).toBe('review-a')
    expect(wrapper.text()).toContain('无法获取任务状态')
    expect(wrapper.text()).toContain('请求超时')
    expect(api.getReview).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('恢复时 404：只清理该用户的恢复键，其它用户的键与其它标签页无影响', async () => {
    signIn(USER_A)
    localStorage.setItem(KEY_A, 'review-gone')
    localStorage.setItem(KEY_B, 'review-b')
    api.getReview.mockRejectedValueOnce(new ApiError('任务不存在', { status: 404 }))

    const wrapper = mountPage()
    await settle()

    expect(localStorage.getItem(KEY_A)).toBeNull()
    expect(sessionStorage.getItem(TAB_KEY_A)).toBeNull()
    expect(localStorage.getItem(KEY_B)).toBe('review-b')
    expect(wrapper.text()).toContain('无法获取任务状态')
    expect(wrapper.text()).toContain('任务不存在或已被删除')
    wrapper.unmount()
  })

  it('恢复时 403：清理该用户的恢复键（明确失效）', async () => {
    signIn(USER_A)
    localStorage.setItem(KEY_A, 'review-a')
    api.getReview.mockRejectedValueOnce(new ApiError('无权访问', { status: 403 }))

    const wrapper = mountPage()
    await settle()

    expect(localStorage.getItem(KEY_A)).toBeNull()
    expect(wrapper.text()).toContain('当前账号无权访问该任务')
    wrapper.unmount()
  })

  it('用户点击“放弃恢复”：清理该用户的恢复键并停止展示该任务', async () => {
    signIn(USER_A)
    localStorage.setItem(KEY_A, 'review-a')
    api.getReview.mockRejectedValueOnce(networkError())

    const wrapper = mountPage()
    await settle()
    expect(localStorage.getItem(KEY_A)).toBe('review-a')

    await buttonByText(wrapper, '放弃恢复').trigger('click')
    await settle()

    expect(localStorage.getItem(KEY_A)).toBeNull()
    expect(sessionStorage.getItem(TAB_KEY_A)).toBeNull()
    expect(wrapper.text()).not.toContain('任务 review-a')
    wrapper.unmount()
  })

  it('按用户隔离：用户 B 挂载只读取自己的键，用户 A 的键与任务都不被读取', async () => {
    signIn(USER_B)
    localStorage.setItem(KEY_A, 'review-a')
    localStorage.setItem(KEY_B, 'review-b')
    api.getReview.mockResolvedValueOnce(reviewData({ review_id: 'review-b', progress: 20 }))

    const wrapper = mountPage()
    await settle()

    expect(api.getReview).toHaveBeenCalledTimes(1)
    expect(api.getReview).toHaveBeenCalledWith('review-b')
    expect(localStorage.getItem(KEY_A)).toBe('review-a')
    expect(wrapper.text()).toContain('任务 review-b')
    expect(wrapper.text()).not.toContain('review-a')
    wrapper.unmount()
  })

  it('未登录会话使用 anonymous 键，不读取任何具名用户的恢复键', async () => {
    localStorage.setItem(KEY_A, 'review-a')
    api.getReview.mockResolvedValueOnce(reviewData({ review_id: 'review-anon' }))

    const wrapper = mountPage()
    await settle()

    expect(api.getReview).not.toHaveBeenCalled()
    expect(localStorage.getItem(KEY_A)).toBe('review-a')
    wrapper.unmount()
  })

  it('旧版无作用域键 activeReviewId 被清理且不会被恢复（避免打开上一账号的任务）', async () => {
    signIn(USER_A)
    localStorage.setItem(LEGACY_KEY, 'review-from-other-account')

    const wrapper = mountPage()
    await settle()

    expect(localStorage.getItem(LEGACY_KEY)).toBeNull()
    expect(api.getReview).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})

describe('R14-4 多标签页与账号切换', () => {
  it('多标签页：共享恢复键被其它标签页清理后，本标签页仍能用自己的会话副本恢复任务', async () => {
    signIn(USER_A)
    localStorage.setItem(KEY_A, 'review-a')
    api.getReview.mockResolvedValue(reviewData({ review_id: 'review-a', progress: 12 }))

    const first = mountPage()
    await settle()
    // 本标签页已把正在跟踪的任务记进自己的 sessionStorage 副本
    expect(sessionStorage.getItem(TAB_KEY_A)).toBe('review-a')
    first.unmount()

    // 模拟另一个标签页清理共享键（sessionStorage 各标签页独立，本标签页副本仍在）
    localStorage.removeItem(KEY_A)
    expect(sessionStorage.getItem(TAB_KEY_A)).toBe('review-a')

    const second = mountPage()
    await settle()

    expect(api.getReview).toHaveBeenLastCalledWith('review-a')
    expect(second.text()).toContain('任务 review-a')
    // 恢复后共享键回到本标签页自己的任务，不会被别的任务顶掉
    expect(localStorage.getItem(KEY_A)).toBe('review-a')
    second.unmount()
  })

  it('账号切换：A 退出后 B 登录，B 只恢复自己的任务，A 的键保持不动', async () => {
    signIn(USER_A)
    localStorage.setItem(KEY_A, 'review-a')
    api.getReview.mockResolvedValue(reviewData({ review_id: 'review-a' }))
    const first = mountPage()
    await settle()
    expect(first.text()).toContain('任务 review-a')
    first.unmount()

    // 换账号：会话换成用户 B（A 的恢复键仍在 localStorage 里，但不再属于当前会话）
    sessionStorage.clear()
    signIn(USER_B)
    localStorage.setItem(KEY_B, 'review-b')
    api.getReview.mockResolvedValue(reviewData({ review_id: 'review-b', progress: 7 }))

    const second = mountPage()
    await settle()

    expect(api.getReview).toHaveBeenLastCalledWith('review-b')
    expect(second.text()).toContain('任务 review-b')
    expect(second.text()).not.toContain('review-a')
    expect(localStorage.getItem(KEY_A)).toBe('review-a')
    second.unmount()
  })
})

describe('R14-5/6 迟到响应与卸载边界', () => {
  it('卸载后：在途的恢复请求与轮询响应都不会再排下一次轮询', async () => {
    vi.useFakeTimers()
    signIn(USER_A)
    localStorage.setItem(KEY_A, 'review-a')
    const resolvers = []
    api.getReview.mockImplementation(() => new Promise((resolve) => { resolvers.push(resolve) }))

    const wrapper = mountPage()
    await settle()

    // 挂载恢复：第一次请求仍在途
    expect(api.getReview).toHaveBeenCalledTimes(1)
    resolvers[0](reviewData({ review_id: 'review-a', status: 'running' }))
    await settle()

    // 运行中 → 500ms 后开始轮询，第二次请求在途
    await vi.advanceTimersByTimeAsync(500)
    expect(api.getReview).toHaveBeenCalledTimes(2)

    wrapper.unmount()
    resolvers[1](reviewData({ review_id: 'review-a', status: 'running', progress: 99 }))
    await settle()
    await vi.advanceTimersByTimeAsync(120000)

    expect(api.getReview).toHaveBeenCalledTimes(2)
  })

  it('卸载后：迟到的恢复响应不会启动轮询（先卸载再返回）', async () => {
    vi.useFakeTimers()
    signIn(USER_A)
    localStorage.setItem(KEY_A, 'review-a')
    let resolveRestore
    api.getReview.mockImplementationOnce(() => new Promise((resolve) => { resolveRestore = resolve }))

    const wrapper = mountPage()
    await settle()
    expect(api.getReview).toHaveBeenCalledTimes(1)

    wrapper.unmount()
    resolveRestore(reviewData({ review_id: 'review-a', status: 'running' }))
    await settle()
    await vi.advanceTimersByTimeAsync(120000)

    expect(api.getReview).toHaveBeenCalledTimes(1)
  })

  it('切换任务时旧响应晚到：不覆盖新任务的状态', async () => {
    vi.useFakeTimers()
    signIn(USER_A)
    localStorage.setItem(KEY_A, 'review-a')
    api.listReviews.mockResolvedValue([
      { review_id: 'review-a', ruleset_id: 'rs', status: 'running', progress: 10, created_at: '2026-01-01 00:00:00' },
      { review_id: 'review-b', ruleset_id: 'rs', status: 'running', progress: 5, created_at: '2026-01-02 00:00:00' },
    ])
    const pending = new Map()
    api.getReview.mockImplementation(id => new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject })
    }))

    const wrapper = mountPage()
    await settle()

    // 恢复任务 A 的请求在途
    expect(api.getReview).toHaveBeenCalledWith('review-a')
    expect(pending.has('review-a')).toBe(true)

    // 用户从“最近任务”里打开任务 B（真实页面路径）
    const openLinks = wrapper.findAll('.el-link').filter(link => link.text() === '打开')
    expect(openLinks.length).toBe(2)
    await openLinks[1].trigger('click')
    await settle()

    expect(api.getReview).toHaveBeenLastCalledWith('review-b')
    pending.get('review-b').resolve(reviewData({
      review_id: 'review-b', status: 'running', progress: 42, stage: 'rule_check', rules_completed: 4, rules_total: 10,
    }))
    await settle()

    expect(wrapper.text()).toContain('任务 review-b')
    expect(wrapper.text()).toContain('42')
    expect(wrapper.text()).toContain('已完成：4 / 10')

    // A 的响应现在才回来：状态与编号都不能被覆盖
    pending.get('review-a').resolve(reviewData({
      review_id: 'review-a', status: 'failed', progress: 100, error: 'A 任务失败了',
    }))
    await settle()

    expect(wrapper.text()).toContain('任务 review-b')
    expect(wrapper.text()).not.toContain('任务 review-a')
    expect(wrapper.text()).not.toContain('A 任务失败了')
    expect(wrapper.text()).toContain('已完成：4 / 10')
    wrapper.unmount()
  })
})

describe('R14-7/8/9/10 创建后失败提示、错误分类、Retry-After 与会话错误区分', () => {
  it('创建成功但首次状态请求失败：提示“任务已创建/状态获取失败”而不是“提交失败”，并继续轮询', async () => {
    vi.useFakeTimers()
    signIn(USER_A)
    api.getReview.mockRejectedValueOnce(networkError())
    api.getReview.mockResolvedValue(reviewData({ review_id: 'review-new', status: 'running', progress: 30 }))

    const wrapper = mountPage()
    await settle()
    await selectFileAndConsent(wrapper)
    await buttonByText(wrapper, '开始审查').trigger('click')
    await settle()

    expect(api.createReview).toHaveBeenCalledTimes(1)
    expect(api.getReview).toHaveBeenCalledWith('review-new')

    // 用户可见的两种提示（组件内告警 + ElMessage 吐司）都不能说“提交失败”
    const text = pageText()
    expect(text).toContain('任务已创建（review-new）')
    expect(text).toContain('状态获取失败')
    expect(text).toContain('将自动重试')
    expect(text).not.toContain('提交失败')
    // 已创建的任务必须保留恢复信息
    expect(localStorage.getItem(KEY_A)).toBe('review-new')

    // 确实还在继续轮询：错误间隔后再次请求并应用成功状态
    api.getReview.mockClear()
    await vi.advanceTimersByTimeAsync(5000)
    await settle()
    expect(api.getReview).toHaveBeenCalledWith('review-new')
    expect(wrapper.text()).toContain('运行中')
    expect(wrapper.text()).toContain('30')
    wrapper.unmount()
  })

  it('真正提交失败（创建请求就失败）：仍然提示“提交失败”，且不写恢复键', async () => {
    vi.useFakeTimers()
    signIn(USER_A)
    api.createReview.mockRejectedValueOnce(new ApiError('文件解析失败', { status: 422 }))

    const wrapper = mountPage()
    await settle()
    await selectFileAndConsent(wrapper)
    await buttonByText(wrapper, '开始审查').trigger('click')
    await settle()

    expect(api.createReview).toHaveBeenCalledTimes(1)
    expect(wrapper.text()).toContain('提交失败')
    expect(pageText()).toContain('提交失败')
    expect(pageText()).toContain('文件解析失败')
    expect(localStorage.getItem(KEY_A)).toBeNull()
    wrapper.unmount()
  })

  it('轮询中 401 用户会话失效：清理该用户的恢复键并停止轮询', async () => {
    vi.useFakeTimers()
    signIn(USER_A)
    localStorage.setItem(KEY_A, 'review-a')
    api.getReview.mockResolvedValueOnce(reviewData({ review_id: 'review-a', status: 'running' }))
    api.getReview.mockRejectedValueOnce(new ApiError('登录已过期', { status: 401, body: { detail: '登录已过期' } }))

    const wrapper = mountPage()
    await settle()
    await vi.advanceTimersByTimeAsync(500)
    await settle()

    expect(wrapper.text()).toContain('登录状态已失效')
    expect(localStorage.getItem(KEY_A)).toBeNull()

    api.getReview.mockClear()
    await vi.advanceTimersByTimeAsync(120000)
    expect(api.getReview).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('轮询中部署访问凭据错误（401 需要访问凭据）：保留恢复键，按凭据问题提示而非会话失效', async () => {
    vi.useFakeTimers()
    signIn(USER_A)
    localStorage.setItem(KEY_A, 'review-a')
    api.getReview.mockResolvedValueOnce(reviewData({ review_id: 'review-a', status: 'running' }))
    api.getReview.mockRejectedValueOnce(new ApiError('需要访问凭据', {
      status: 401, body: { detail: '需要访问凭据' },
    }))

    const wrapper = mountPage()
    await settle()
    await vi.advanceTimersByTimeAsync(500)
    await settle()

    const text = wrapper.text()
    expect(text).toContain('部署访问凭据无效或缺失')
    expect(text).not.toContain('登录状态已失效')
    expect(localStorage.getItem(KEY_A)).toBe('review-a')
    wrapper.unmount()
  })

  it('轮询中 404：清理恢复键并停止轮询', async () => {
    vi.useFakeTimers()
    signIn(USER_A)
    localStorage.setItem(KEY_A, 'review-a')
    api.getReview.mockResolvedValueOnce(reviewData({ review_id: 'review-a', status: 'running' }))
    api.getReview.mockRejectedValueOnce(new ApiError('任务不存在', { status: 404 }))

    const wrapper = mountPage()
    await settle()
    await vi.advanceTimersByTimeAsync(500)
    await settle()

    expect(localStorage.getItem(KEY_A)).toBeNull()
    api.getReview.mockClear()
    await vi.advanceTimersByTimeAsync(120000)
    expect(api.getReview).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('轮询中 429 带 Retry-After:30：下一次轮询等待不少于 30 秒，且恢复键保留', async () => {
    vi.useFakeTimers()
    signIn(USER_A)
    localStorage.setItem(KEY_A, 'review-a')
    api.getReview.mockResolvedValueOnce(reviewData({ review_id: 'review-a', status: 'running' }))
    api.getReview.mockRejectedValueOnce(new ApiError('请求过于频繁，请稍后再试', { status: 429, retryAfter: '30' }))
    api.getReview.mockResolvedValue(reviewData({ review_id: 'review-a', status: 'running', progress: 55 }))

    const wrapper = mountPage()
    await settle()
    await vi.advanceTimersByTimeAsync(500)
    await settle()
    expect(api.getReview).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('请求过于频繁')
    expect(localStorage.getItem(KEY_A)).toBe('review-a')

    api.getReview.mockClear()
    // 29 秒内不得再发请求（旧实现会在 1.5s/5s 后机械重试）
    await vi.advanceTimersByTimeAsync(29000)
    expect(api.getReview).not.toHaveBeenCalled()

    // 到 30 秒按服务端要求重试
    await vi.advanceTimersByTimeAsync(1000)
    await settle()
    expect(api.getReview).toHaveBeenCalledTimes(1)
    expect(wrapper.text()).toContain('55')
    wrapper.unmount()
  })

  it('轮询中 5xx：保留恢复键并按错误重试间隔自动重试', async () => {
    vi.useFakeTimers()
    signIn(USER_A)
    localStorage.setItem(KEY_A, 'review-a')
    api.getReview.mockResolvedValueOnce(reviewData({ review_id: 'review-a', status: 'running' }))
    api.getReview.mockRejectedValueOnce(new ApiError('内部服务器错误', { status: 502 }))
    api.getReview.mockResolvedValue(reviewData({ review_id: 'review-a', status: 'running', progress: 66 }))

    const wrapper = mountPage()
    await settle()
    await vi.advanceTimersByTimeAsync(500)
    await settle()
    expect(wrapper.text()).toContain('内部服务器错误')
    expect(localStorage.getItem(KEY_A)).toBe('review-a')

    api.getReview.mockClear()
    await vi.advanceTimersByTimeAsync(5000)
    await settle()
    expect(api.getReview).toHaveBeenCalledWith('review-a')
    expect(wrapper.text()).toContain('66')
    wrapper.unmount()
  })
})
