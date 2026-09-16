// R14：登出/会话失效时清理「该用户」的任务恢复键（只清当前用户，不动其它账号的键）
import { clearActiveReview, userIdOf } from './reviewRecovery.js'

const BASE = '/api/v1'
const DEFAULT_TIMEOUT = 15000

export class ApiError extends Error {
  constructor(message, { status = 0, body = null, retryAfter = '', timedOut = false } = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
    this.retryAfter = retryAfter
    this.timedOut = timedOut
  }
}

// 部署级访问凭据（X-Access-Token）校验失败的固定提示（main.py 中间件）
const DEPLOYMENT_ACCESS_DETAIL = '需要访问凭据'

export function isDeploymentAccessDenied(status, body) {
  return status === 401
    && body
    && typeof body === 'object'
    && body.detail === DEPLOYMENT_ACCESS_DETAIL
}

// 错误分类（B7a）：页面据此决定“停止、提示、重试还是重新登录”。
// kind ∈ access-denied | auth | forbidden | notfound | conflict | validation
//        | rate-limited | server | timeout | network | unknown
export function classifyError(error) {
  if (!(error instanceof ApiError)) return 'network'
  const status = error.status
  if (status === 0) return error.timedOut ? 'timeout' : 'network'
  if (status === 401) {
    return isDeploymentAccessDenied(status, error.body) ? 'access-denied' : 'auth'
  }
  if (status === 403) return 'forbidden'
  if (status === 404) return 'notfound'
  if (status === 409) return 'conflict'
  if (status === 422) return 'validation'
  if (status === 429) return 'rate-limited'
  if (status >= 500) return 'server'
  return 'unknown'
}

// 轮询/请求是否值得自动重试：瞬时性错误（网络/超时/5xx/429）才重试。
export function shouldRetry(error) {
  return ['network', 'timeout', 'server', 'rate-limited', 'unknown'].includes(classifyError(error))
}

async function request(url, options = {}, timeout = DEFAULT_TIMEOUT) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeout)
  try {
    const headers = { ...accessHeaders(), ...(options.headers || {}) }
    let res
    try {
      res = await fetch(url, { ...options, headers, signal: controller.signal })
    } catch (error) {
      if (error.name === 'AbortError') {
        throw new ApiError('请求超时，请检查后端服务', { status: 0, timedOut: true })
      }
      // 网络层失败（非超时）：包装为可识别错误，保留原始信息
      throw new ApiError(
        `网络连接失败：${error.message || '无法连接后端服务'}`,
        { status: 0 },
      )
    }
    if (!res.ok) {
      const { body, message } = await readError(res)
      const error = new ApiError(message, {
        status: res.status,
        body,
        retryAfter: res.headers?.get?.('retry-after') || '',
      })
      handleHttpError(url, error)
      throw error
    }
    return res
  } finally {
    clearTimeout(timer)
  }
}

export function setAccessToken(token) {
  const value = String(token || '').trim()
  if (value) sessionStorage.setItem('accessToken', value)
  else sessionStorage.removeItem('accessToken')
}

export function setAuthToken(token) {
  const value = String(token || '').trim()
  if (value) sessionStorage.setItem('authToken', value)
  else sessionStorage.removeItem('authToken')
}

export function getAuthToken() {
  return sessionStorage.getItem('authToken') || ''
}

export function getCurrentUser() {
  try { return JSON.parse(sessionStorage.getItem('currentUser') || 'null') }
  catch { return null }
}

export function setCurrentUser(user) {
  if (user) sessionStorage.setItem('currentUser', JSON.stringify(user))
  else sessionStorage.removeItem('currentUser')
  dispatchBrowserEvent('auth-changed', user || null)
}

export function getAccessToken() {
  return sessionStorage.getItem('accessToken') || ''
}

export function accessHeaders() {
  const token = getAccessToken()
  const authToken = getAuthToken()
  return {
    ...(token ? { 'X-Access-Token': token } : {}),
    ...(authToken ? { Authorization: `Bearer ${authToken}` } : {}),
  }
}

export async function login(username, password) {
  const data = await (await request(`${BASE}/auth/login`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })).json()
  setAuthToken(data.token)
  setCurrentUser(data.user)
  return data.user
}

export async function getMe() {
  const data = await (await request(`${BASE}/auth/me`)).json()
  setCurrentUser(data.user)
  return data.user
}

export async function logout() {
  // R14：用户主动放弃 —— 先取用户标识（清理会话后就拿不到了），登出后清理其恢复键
  const userId = userIdOf(getCurrentUser())
  try { await request(`${BASE}/auth/logout`, { method: 'POST' }) } catch {}
  setAuthToken('')
  setCurrentUser(null)
  clearActiveReview(userId)
}

// B7b：自助改密（验证原密码；成功后服务端撤销其它会话）
export async function changePassword(oldPassword, newPassword) {
  return (await request(`${BASE}/auth/change-password`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
  })).json()
}

export async function listUsers() {
  const data = await (await request(`${BASE}/auth/users`)).json()
  return data.users || []
}

export async function listRoles() {
  const data = await (await request(`${BASE}/auth/roles`)).json()
  return data.roles || []
}

export async function listReviewers() {
  const data = await (await request(`${BASE}/auth/reviewers`)).json()
  return data.reviewers || []
}

export async function createUser(payload) {
  const data = await (await request(`${BASE}/auth/users`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(payload),
  })).json()
  return data.user
}

export async function updateUser(userId, payload) {
  const data = await (await request(`${BASE}/auth/users/${encodeURIComponent(userId)}`, {
    method: 'PATCH',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(payload),
  })).json()
  return data.user
}

export async function resetUserPassword(userId, password) {
  return (await request(`${BASE}/auth/users/${encodeURIComponent(userId)}/reset-password`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ password }),
  })).json()
}

export async function getHealth() {
  return (await request('/api/health')).json()
}

export async function getRulesets() {
  const res = await request(`${BASE}/rulesets`)
  const data = await res.json()
  return data.rulesets || []
}

export async function getRuleset(id) {
  return (await request(`${BASE}/rulesets/${encodeURIComponent(id)}`)).json()
}

export async function saveRuleset(id, yamlText, adminToken = '') {
  const headers = { 'content-type': 'text/plain' }
  if (adminToken) headers['X-Admin-Token'] = adminToken
  return (await request(`${BASE}/rulesets/${encodeURIComponent(id)}`, {
    method: 'PUT', headers, body: yamlText,
  })).json()
}

export async function generateRulesetFromGuideline(file, adminToken = '') {
  const form = new FormData()
  form.append('file', file)
  const headers = adminToken ? { 'X-Admin-Token': adminToken } : {}
  return (await request(`${BASE}/rulesets/from-guideline`, {
    method: 'POST', headers, body: form,
  }, 180000)).json()
}

function adminHeaders(adminToken = '', json = false) {
  const headers = { ...accessHeaders() }
  if (adminToken) headers['X-Admin-Token'] = adminToken
  if (json) headers['content-type'] = 'application/json'
  return headers
}

export async function getCheckerCatalog(adminToken = '') {
  const data = await (await request(`${BASE}/rule-admin/checkers`, {
    headers: adminHeaders(adminToken),
  })).json()
  return data.checkers || []
}

export async function getManagedRulesets(adminToken = '') {
  const data = await (await request(`${BASE}/rule-admin/rulesets`, {
    headers: adminHeaders(adminToken),
  })).json()
  return data.rulesets || []
}

export async function getManagedRuleset(id, adminToken = '') {
  return (await request(`${BASE}/rule-admin/rulesets/${encodeURIComponent(id)}`, {
    headers: adminHeaders(adminToken),
  })).json()
}

export async function createManagedRuleset(data, adminToken = '') {
  return (await request(`${BASE}/rule-admin/rulesets`, {
    method: 'POST', headers: adminHeaders(adminToken, true), body: JSON.stringify({ data }),
  })).json()
}

export async function cloneManagedRuleset(id, sourceVersionId, adminToken = '') {
  return (await request(`${BASE}/rule-admin/rulesets/${encodeURIComponent(id)}/clone`, {
    method: 'POST', headers: adminHeaders(adminToken, true),
    body: JSON.stringify({ source_version_id: sourceVersionId || undefined }),
  })).json()
}

export async function setManagedRulesetEnabled(id, enabled, adminToken = '') {
  const action = enabled ? 'enable' : 'disable'
  return (await request(`${BASE}/rule-admin/rulesets/${encodeURIComponent(id)}/${action}`, {
    method: 'POST', headers: adminHeaders(adminToken, true), body: '{}',
  })).json()
}

export async function deleteManagedRuleset(id, adminToken = '') {
  return (await request(`${BASE}/rule-admin/rulesets/${encodeURIComponent(id)}`, {
    method: 'DELETE', headers: adminHeaders(adminToken),
  })).json()
}

export async function restoreManagedRuleset(id, sourceVersionId, adminToken = '') {
  return (await request(`${BASE}/rule-admin/rulesets/${encodeURIComponent(id)}/restore`, {
    method: 'POST', headers: adminHeaders(adminToken, true),
    body: JSON.stringify({ source_version_id: sourceVersionId }),
  })).json()
}

export async function validateManagedRuleset(data, adminToken = '') {
  return (await request(`${BASE}/rule-admin/validate`, {
    method: 'POST', headers: adminHeaders(adminToken, true), body: JSON.stringify({ data }),
  })).json()
}

export async function saveManagedVersion(versionId, data, revision, adminToken = '') {
  return (await request(`${BASE}/rule-admin/versions/${encodeURIComponent(versionId)}`, {
    method: 'PUT', headers: adminHeaders(adminToken, true),
    body: JSON.stringify({ data, revision }),
  })).json()
}

export async function saveManagedYaml(versionId, yaml, revision, adminToken = '') {
  return (await request(`${BASE}/rule-admin/versions/${encodeURIComponent(versionId)}/yaml`, {
    method: 'PUT', headers: adminHeaders(adminToken, true),
    body: JSON.stringify({ yaml, revision }),
  })).json()
}

export async function deleteManagedVersion(versionId, adminToken = '') {
  return (await request(`${BASE}/rule-admin/versions/${encodeURIComponent(versionId)}`, {
    method: 'DELETE', headers: adminHeaders(adminToken),
  })).json()
}

export async function actOnManagedVersion(versionId, action, adminToken = '', payload = {}) {
  return (await request(`${BASE}/rule-admin/versions/${encodeURIComponent(versionId)}/${action}`, {
    method: 'POST', headers: adminHeaders(adminToken, true), body: JSON.stringify(payload),
  }, action === 'test' ? 180000 : DEFAULT_TIMEOUT)).json()
}

export async function uploadDocument(file) {
  const form = new FormData()
  form.append('file', file)
  return (await request(`${BASE}/documents`, { method: 'POST', body: form }, 120000)).json()
}

export async function listDocuments(limit = 50) {
  const data = await (await request(`${BASE}/documents?limit=${encodeURIComponent(limit)}`)).json()
  return data.documents || []
}

export async function createReview(
  documentId,
  rulesetId = 'campus_general_v1',
  { useAi = true, privacyConsent = false, idempotencyKey = '' } = {},
) {
  const form = new FormData()
  form.append('document_id', documentId)
  form.append('ruleset_id', rulesetId)
  form.append('use_ai', String(useAi))
  form.append('privacy_consent', String(privacyConsent))
  const headers = idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : {}
  return (await request(`${BASE}/reviews`, { method: 'POST', headers, body: form })).json()
}

export async function getReview(reviewId) {
  return (await request(`${BASE}/reviews/${encodeURIComponent(reviewId)}`)).json()
}

export async function listReviews(limit = 20) {
  const res = await request(`${BASE}/reviews?limit=${encodeURIComponent(limit)}`)
  const data = await res.json()
  return data.reviews || []
}

export async function cancelReview(reviewId) {
  return (await request(`${BASE}/reviews/${encodeURIComponent(reviewId)}/cancel`, {
    method: 'POST',
  })).json()
}

export async function getReviewIssues(reviewId) {
  return (await request(`${BASE}/reviews/${encodeURIComponent(reviewId)}/issues`)).json()
}

export function getDocumentFileUrl(documentId) {
  return `${BASE}/documents/${encodeURIComponent(documentId)}/file`
}

export function getAnnotatedPdfUrl(reviewId) {
  return `${BASE}/reviews/${encodeURIComponent(reviewId)}/annotated.pdf`
}

export function getReviewReportUrl(reviewId) {
  return `${BASE}/reviews/${encodeURIComponent(reviewId)}/report.pdf`
}

export async function createBatch(
  files,
  rulesetId,
  { useAi = true, privacyConsent = false } = {},
) {
  const form = new FormData()
  for (const file of files) form.append('files', file)
  form.append('ruleset_id', rulesetId)
  form.append('use_ai', String(useAi))
  form.append('privacy_consent', String(privacyConsent))
  return (await request(`${BASE}/batches`, { method: 'POST', body: form }, 180000)).json()
}

export async function getBatch(batchId) {
  return (await request(`${BASE}/batches/${encodeURIComponent(batchId)}`)).json()
}

export function getBatchSummaryUrl(batchId) {
  return `${BASE}/batches/${encodeURIComponent(batchId)}/summary.xlsx`
}

export async function downloadFile(url, filename) {
  const response = await request(url, {}, 180000)
  const blob = await response.blob()
  const objectUrl = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = objectUrl
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(objectUrl)
}

function isLoginRequest(url) {
  return String(url || '').split(/[?#]/, 1)[0].endsWith(`${BASE}/auth/login`)
}

function handleHttpError(url, error) {
  if (error.status !== 401) return
  if (isLoginRequest(url)) return // 登录失败不清空既有会话
  if (isDeploymentAccessDenied(error.status, error.body)) {
    // B7a/C15：部署访问凭据错误 ≠ 用户会话失效——不清空有效登录状态，
    // 通知上层（App 提示重新输入 ACCESS_TOKEN）
    dispatchBrowserEvent('access-invalid', {
      status: error.status,
      message: error.message,
    })
    return
  }
  // 其余 401 = 用户会话失效（过期/被撤销/需重新登录）
  expireAuthSession(error)
}

function expireAuthSession(error) {
  // R14：401 会话失效属于「明确失效」，清理该用户的恢复键（在清空会话前取用户标识）
  const userId = userIdOf(getCurrentUser())
  setAuthToken('')
  setCurrentUser(null)
  clearActiveReview(userId)
  dispatchBrowserEvent('auth-expired', {
    status: error.status,
    message: error.message,
  })
}

function dispatchBrowserEvent(type, detail) {
  if (typeof window === 'undefined' || typeof window.dispatchEvent !== 'function') return
  const event = typeof CustomEvent === 'function'
    ? new CustomEvent(type, { detail })
    : { type, detail }
  window.dispatchEvent(event)
}

async function readError(res) {
  const text = await res.text()
  if (!text) return { body: null, message: `HTTP ${res.status}` }
  try {
    const body = JSON.parse(text)
    return { body, message: errorMessage(body, res.status) }
  } catch {
    return { body: text, message: text }
  }
}

function errorMessage(body, status) {
  if (typeof body === 'string') return body || `HTTP ${status}`
  if (body && typeof body === 'object') {
    if (Object.prototype.hasOwnProperty.call(body, 'detail')) {
      return formatDetail(body.detail) || `HTTP ${status}`
    }
    if (typeof body.message === 'string' && body.message) return body.message
    try { return JSON.stringify(body) }
    catch { return `HTTP ${status}` }
  }
  return body === undefined || body === null ? `HTTP ${status}` : String(body)
}

function formatDetail(detail) {
  if (Array.isArray(detail)) {
    return detail.map(formatDetail).filter(Boolean).join('；')
  }
  if (detail && typeof detail === 'object') {
    const message = detail.msg || detail.message
    const location = Array.isArray(detail.loc) ? detail.loc.map(String).join('.') : ''
    if (message) return location ? `${location}: ${message}` : String(message)
    try { return JSON.stringify(detail) }
    catch { return String(detail) }
  }
  return detail === undefined || detail === null ? '' : String(detail)
}
