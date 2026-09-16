import assert from 'node:assert/strict'
import test, { afterEach, beforeEach } from 'node:test'

import {
  ApiError,
  changePassword,
  classifyError,
  getMe,
  getRulesets,
  isDeploymentAccessDenied,
  listDocuments,
  login,
} from '../src/services/api.js'

const originalFetch = globalThis.fetch
const originalSessionStorage = globalThis.sessionStorage
const originalWindow = globalThis.window

let dispatchedEvents

beforeEach(() => {
  globalThis.sessionStorage = memoryStorage()
  dispatchedEvents = []
  globalThis.window = {
    dispatchEvent(event) {
      dispatchedEvents.push(event)
      return true
    },
  }
})

afterEach(() => {
  globalThis.fetch = originalFetch
  if (originalSessionStorage === undefined) delete globalThis.sessionStorage
  else globalThis.sessionStorage = originalSessionStorage
  if (originalWindow === undefined) delete globalThis.window
  else globalThis.window = originalWindow
})

test('formats FastAPI validation details and preserves the response body', async () => {
  const detail = [
    { loc: ['body', 'file'], msg: 'Field required' },
    { loc: ['query', 'limit'], msg: 'Input should be greater than or equal to 1' },
  ]
  globalThis.fetch = async () => response(422, { detail })

  await assert.rejects(listDocuments(0), (error) => {
    assert.ok(error instanceof ApiError)
    assert.equal(error.status, 422)
    assert.deepEqual(error.body, { detail })
    assert.equal(
      error.message,
      'body.file: Field required；query.limit: Input should be greater than or equal to 1',
    )
    return true
  })
})

test('preserves Retry-After on rate-limit errors', async () => {
  globalThis.fetch = async () => response(
    429,
    { detail: '请求过于频繁，请稍后再试' },
    { 'Retry-After': '60' },
  )

  await assert.rejects(getRulesets(), (error) => {
    assert.ok(error instanceof ApiError)
    assert.equal(error.status, 429)
    assert.equal(error.retryAfter, '60')
    assert.deepEqual(error.body, { detail: '请求过于频繁，请稍后再试' })
    return true
  })
})

test('clears and announces an expired authenticated session on 401', async () => {
  sessionStorage.setItem('authToken', 'expired-token')
  sessionStorage.setItem('currentUser', JSON.stringify({ id: 'user-1', role: 'admin' }))
  sessionStorage.setItem('accessToken', 'perimeter-token')
  globalThis.fetch = async () => response(401, { detail: '登录已过期' })

  await assert.rejects(getMe(), (error) => {
    assert.ok(error instanceof ApiError)
    assert.equal(error.status, 401)
    return true
  })

  assert.equal(sessionStorage.getItem('authToken'), null)
  assert.equal(sessionStorage.getItem('currentUser'), null)
  assert.equal(sessionStorage.getItem('accessToken'), 'perimeter-token')
  assert.deepEqual(
    dispatchedEvents.map(event => event.type),
    ['auth-changed', 'auth-expired'],
  )
})

test('does not clear an existing session for a rejected login attempt', async () => {
  sessionStorage.setItem('authToken', 'existing-token')
  sessionStorage.setItem('currentUser', JSON.stringify({ id: 'user-1', role: 'user' }))
  globalThis.fetch = async () => response(401, { detail: '用户名或密码错误' })

  await assert.rejects(login('invalid', 'invalid'), (error) => {
    assert.ok(error instanceof ApiError)
    assert.equal(error.status, 401)
    return true
  })

  assert.equal(sessionStorage.getItem('authToken'), 'existing-token')
  assert.deepEqual(JSON.parse(sessionStorage.getItem('currentUser')), {
    id: 'user-1',
    role: 'user',
  })
  assert.equal(dispatchedEvents.length, 0)
})

test('deployment access-token 401 does NOT clear the authenticated session (C15/B7a)', async () => {
  sessionStorage.setItem('authToken', 'still-valid-session')
  sessionStorage.setItem('currentUser', JSON.stringify({ id: 'user-1', role: 'user' }))
  sessionStorage.setItem('accessToken', 'stale-perimeter-token')
  globalThis.fetch = async () => response(401, { detail: '需要访问凭据' })

  await assert.rejects(getMe(), (error) => {
    assert.equal(error.status, 401)
    return true
  })

  // 用户会话必须保留，仅通知“部署访问凭据”错误
  assert.equal(sessionStorage.getItem('authToken'), 'still-valid-session')
  assert.deepEqual(JSON.parse(sessionStorage.getItem('currentUser')), {
    id: 'user-1',
    role: 'user',
  })
  assert.equal(sessionStorage.getItem('accessToken'), 'stale-perimeter-token')
  assert.deepEqual(
    dispatchedEvents.map(event => event.type),
    ['access-invalid'],
  )
})

test('wraps network failures into a classified ApiError', async () => {
  globalThis.fetch = async () => { throw new TypeError('Failed to fetch') }

  await assert.rejects(listDocuments(), (error) => {
    assert.ok(error instanceof ApiError)
    assert.equal(error.status, 0)
    assert.equal(classifyError(error), 'network')
    assert.match(error.message, /网络连接失败/)
    return true
  })
})

test('classifyError maps statuses to actions', () => {
  const cases = [
    [new ApiError('需要访问凭据', { status: 401, body: { detail: '需要访问凭据' } }), 'access-denied'],
    [new ApiError('登录已过期', { status: 401, body: { detail: '登录已过期' } }), 'auth'],
    [new ApiError('无权限', { status: 403 }), 'forbidden'],
    [new ApiError('不存在', { status: 404 }), 'notfound'],
    [new ApiError('冲突', { status: 409 }), 'conflict'],
    [new ApiError('过快', { status: 429, retryAfter: '60' }), 'rate-limited'],
    [new ApiError('服务错误', { status: 500 }), 'server'],
    [new ApiError('超时', { status: 0, timedOut: true }), 'timeout'],
    [new ApiError('断网', { status: 0 }), 'network'],
    [new ApiError('未知', { status: 302 }), 'unknown'],
  ]
  for (const [error, expected] of cases) {
    assert.equal(classifyError(error), expected, error.message)
  }
  assert.equal(isDeploymentAccessDenied(401, { detail: '需要访问凭据' }), true)
  assert.equal(isDeploymentAccessDenied(401, { detail: '登录已过期' }), false)
})

test('changePassword posts old/new password to the change endpoint', async () => {
  sessionStorage.setItem('authToken', 'session-token')
  let captured
  globalThis.fetch = async (url, options) => {
    captured = { url, options }
    return response(200, { ok: true })
  }

  await changePassword('old-pass', 'new-pass-123')

  assert.equal(captured.url, '/api/v1/auth/change-password')
  assert.equal(captured.options.method, 'POST')
  const headers = captured.options.headers
  assert.equal(headers['Authorization'], 'Bearer session-token')
  assert.equal(JSON.parse(captured.options.body).old_password, 'old-pass')
  assert.equal(JSON.parse(captured.options.body).new_password, 'new-pass-123')
})

function memoryStorage() {
  const values = new Map()
  return {
    getItem(key) {
      return values.has(key) ? values.get(key) : null
    },
    setItem(key, value) {
      values.set(key, String(value))
    },
    removeItem(key) {
      values.delete(key)
    },
    clear() {
      values.clear()
    },
  }
}

function response(status, body, headers = {}) {
  const normalizedHeaders = new Map(
    Object.entries(headers).map(([key, value]) => [key.toLowerCase(), String(value)]),
  )
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: {
      get(name) {
        return normalizedHeaders.get(String(name).toLowerCase()) || null
      },
    },
    async text() {
      return JSON.stringify(body)
    },
    async json() {
      return body
    },
  }
}
