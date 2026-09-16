// R14-2/4/10：登出与会话失效时必须清理「当前用户」的任务恢复键。
// 这里走的是真实组件路径：挂载 App.vue，点击顶部“退出”按钮（logoutCurrent）。
// 只把 api 模块的网络函数换成 mock；恢复键清理逻辑（reviewRecovery）为真实实现。
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import ElementPlus from 'element-plus'
import App from '../App.vue'

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal()
  return {
    ...actual,
    getHealth: vi.fn(),
    getMe: vi.fn(),
    getRulesets: vi.fn(),
    logout: vi.fn(),
  }
})

const api = await import('../services/api')

const USER_A = 'user-a'
const USER_B = 'user-b'
const KEY_A = `activeReviewId:${USER_A}`
const KEY_B = `activeReviewId:${USER_B}`
const TAB_KEY_A = `${KEY_A}:tab`

function signIn(userId) {
  sessionStorage.setItem('authToken', 'session-token')
  sessionStorage.setItem('currentUser', JSON.stringify({
    id: userId, username: userId, role_label: '普通用户', capabilities: [],
  }))
}

function buildRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/login', component: { template: '<div>登录页</div>' } },
    ],
  })
}

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  api.getHealth.mockReset()
  api.getMe.mockReset()
  api.getRulesets.mockReset()
  api.logout.mockReset()
  api.getHealth.mockResolvedValue({ status: 'ok', version: '1.4.0', checks: {} })
  api.getMe.mockResolvedValue({ id: USER_A, role_label: '普通用户', capabilities: [] })
  api.getRulesets.mockResolvedValue([])
  api.logout.mockResolvedValue(undefined)
})

afterEach(() => {
  document.body.innerHTML = ''
})

describe('R14-2/4 登出清理当前用户的恢复键（App.vue 真实路径）', () => {
  it('点击“退出”后：清理当前用户的恢复键，其它账号的恢复键不受影响', async () => {
    signIn(USER_A)
    localStorage.setItem(KEY_A, 'review-a')
    localStorage.setItem(KEY_B, 'review-b')
    api.getMe.mockResolvedValue({ id: USER_A, role_label: '普通用户', capabilities: [] })

    const router = buildRouter()
    await router.push('/')
    await router.isReady()
    const wrapper = mount(App, { global: { plugins: [ElementPlus, router] }, attachTo: document.body })
    await flushPromises()

    const logoutButton = wrapper.findAll('button').find(item => item.text() === '退出')
    expect(logoutButton).toBeTruthy()
    expect(localStorage.getItem(KEY_A)).toBe('review-a')

    await logoutButton.trigger('click')
    await flushPromises()

    expect(api.logout).toHaveBeenCalledTimes(1)
    expect(localStorage.getItem(KEY_A)).toBeNull()
    expect(sessionStorage.getItem(TAB_KEY_A)).toBeNull()
    expect(localStorage.getItem(KEY_B)).toBe('review-b')
    wrapper.unmount()
  })

  it('不同账号登录时互不干扰：用户 B 的会话不会读到用户 A 的恢复键', async () => {
    signIn(USER_B)
    localStorage.setItem(KEY_A, 'review-a')

    const router = buildRouter()
    await router.push('/')
    await router.isReady()
    const wrapper = mount(App, { global: { plugins: [ElementPlus, router] }, attachTo: document.body })
    await flushPromises()

    expect(localStorage.getItem(KEY_A)).toBe('review-a')
    await wrapper.findAll('button').find(item => item.text() === '退出').trigger('click')
    await flushPromises()

    // 登出的是 B：A 的恢复键必须留着，B 自己没有键可清
    expect(localStorage.getItem(KEY_A)).toBe('review-a')
    wrapper.unmount()
  })
})
