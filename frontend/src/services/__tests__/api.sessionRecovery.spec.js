// R14-1/2/10（api.js 层）：真实 api 模块 + mock fetch，验证
// 「登出/401 会话失效 → 清理该用户恢复键」「部署访问凭据错误 → 不清理」
// 「网络失败 → 不清理」。
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, getMe, logout } from '../api'

const USER_A = 'user-a'
const USER_B = 'user-b'
const KEY_A = `activeReviewId:${USER_A}`
const KEY_B = `activeReviewId:${USER_B}`
const TAB_KEY_A = `${KEY_A}:tab`

function signIn(userId) {
  sessionStorage.setItem('authToken', 'session-token')
  sessionStorage.setItem('currentUser', JSON.stringify({ id: userId, username: userId }))
}

function response(status, body, headers = {}) {
  const normalized = new Map(Object.entries(headers).map(([key, value]) => [key.toLowerCase(), String(value)]))
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: name => normalized.get(String(name).toLowerCase()) || null },
    async text() { return body === undefined ? '' : JSON.stringify(body) },
    async json() { return body },
  }
}

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  signIn(USER_A)
  localStorage.setItem(KEY_A, 'review-a')
  localStorage.setItem(KEY_B, 'review-b')
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('api.js 会话边界与恢复键（R14-2/10）', () => {
  it('logout()：清理当前用户的恢复键，其它账号的键保留', async () => {
    const events = []
    window.addEventListener('auth-changed', event => events.push(event.type), { once: true })
    vi.stubGlobal('fetch', vi.fn(async () => response(200, { ok: true })))

    await logout()

    expect(localStorage.getItem(KEY_A)).toBeNull()
    expect(sessionStorage.getItem(TAB_KEY_A)).toBeNull()
    expect(localStorage.getItem(KEY_B)).toBe('review-b')
    expect(sessionStorage.getItem('authToken')).toBeNull()
    expect(sessionStorage.getItem('currentUser')).toBeNull()
  })

  it('401 用户会话失效：清理该用户恢复键并广播 auth-expired', async () => {
    const events = []
    window.addEventListener('auth-expired', event => events.push(event), { once: true })
    vi.stubGlobal('fetch', vi.fn(async () => response(401, { detail: '登录已过期' })))

    await expect(getMe()).rejects.toBeInstanceOf(ApiError)

    expect(localStorage.getItem(KEY_A)).toBeNull()
    expect(sessionStorage.getItem(TAB_KEY_A)).toBeNull()
    expect(localStorage.getItem(KEY_B)).toBe('review-b')
    expect(sessionStorage.getItem('authToken')).toBeNull()
    expect(events.length).toBe(1)
  })

  it('401 部署访问凭据错误：保留恢复键与登录会话，只广播 access-invalid（≠ 会话失效）', async () => {
    const expired = []
    const accessInvalid = []
    window.addEventListener('auth-expired', event => expired.push(event), { once: true })
    window.addEventListener('access-invalid', event => accessInvalid.push(event), { once: true })
    vi.stubGlobal('fetch', vi.fn(async () => response(401, { detail: '需要访问凭据' })))

    await expect(getMe()).rejects.toBeInstanceOf(ApiError)

    expect(localStorage.getItem(KEY_A)).toBe('review-a')
    expect(sessionStorage.getItem('authToken')).toBe('session-token')
    expect(sessionStorage.getItem('currentUser')).not.toBeNull()
    expect(expired.length).toBe(0)
    expect(accessInvalid.length).toBe(1)
  })

  it('网络失败：不清会话也不清恢复键（可稍后重试恢复）', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch') }))

    await expect(getMe()).rejects.toBeInstanceOf(ApiError)

    expect(localStorage.getItem(KEY_A)).toBe('review-a')
    expect(sessionStorage.getItem('authToken')).toBe('session-token')
  })
})
