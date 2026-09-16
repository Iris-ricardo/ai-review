// R14：审查任务恢复键与轮询错误决策。
// 键按用户隔离：localStorage `activeReviewId:<userId>`（未登录 :anonymous）+ sessionStorage 标签页副本 `...:tab`；
// 读取以共享键为准、副本兜底；旧的无作用域键 `activeReviewId` 直接清理不迁移。
// 是否清键只由错误类型决定：仅 404/403/401/主动放弃清理，断网/超时/5xx/429 一律保留。
//
// 本模块不 import api.js（api.js 反向依赖它做登出清理，避免循环依赖），故所有函数接收显式 userId。
export const RECOVERY_KEY_PREFIX = 'activeReviewId'
export const LEGACY_RECOVERY_KEY = 'activeReviewId'
export const ANONYMOUS_USER_ID = 'anonymous'
export const TAB_SUFFIX = ':tab'

// 轮询节奏（毫秒）
export const POLL_INTERVAL = 1500
export const STALE_INTERVAL = 5000
export const ERROR_INTERVAL = 5000

function storageOf(kind) {
  try {
    const store = kind === 'session' ? globalThis.sessionStorage : globalThis.localStorage
    return store && typeof store.getItem === 'function' ? store : null
  } catch {
    return null
  }
}

function readKey(kind, key) {
  try {
    return storageOf(kind)?.getItem(key) || ''
  } catch {
    return ''
  }
}

function writeKey(kind, key, value) {
  try {
    const value2 = String(value ?? '')
    if (value2) storageOf(kind)?.setItem(key, value2)
    else storageOf(kind)?.removeItem(key)
  } catch {
    // 隐私模式/配额不足：恢复键失效不影响主流程
  }
}

// 解析用户标识：既接受已登录会话里的用户对象，也接受已经是标识的字符串/数字
export function userIdOf(user) {
  if (user === null || user === undefined) return ''
  if (typeof user === 'string' || typeof user === 'number') return String(user).trim()
  if (typeof user !== 'object') return ''
  const raw = user.id ?? user.user_id ?? user.username ?? user.name ?? ''
  return String(raw || '').trim()
}

export function recoveryKey(userId) {
  return `${RECOVERY_KEY_PREFIX}:${userIdOf(userId) || ANONYMOUS_USER_ID}`
}

export function tabRecoveryKey(userId) {
  return `${recoveryKey(userId)}${TAB_SUFFIX}`
}

// 恢复键里的任务 ID（不存在返回 ''）。读到共享键时同步记入本标签页副本，
// 这样即使共享键随后被其它标签页改写/清理，本标签页自己的任务仍可恢复。
export function loadActiveReview(userId) {
  const shared = readKey('local', recoveryKey(userId))
  if (shared) {
    writeKey('session', tabRecoveryKey(userId), shared)
    return shared
  }
  return readKey('session', tabRecoveryKey(userId))
}

export function saveActiveReview(reviewId, userId) {
  const id = String(reviewId || '').trim()
  if (!id) return
  writeKey('local', recoveryKey(userId), id)
  writeKey('session', tabRecoveryKey(userId), id)
}

// 只清理「该用户」的恢复信息（含本标签页副本）——其它用户/其它标签页的键不受影响
export function clearActiveReview(userId) {
  writeKey('local', recoveryKey(userId), '')
  writeKey('session', tabRecoveryKey(userId), '')
}

// 旧版本的无作用域键：无法判断归属，删除以免串号
export function clearLegacyRecoveryKey() {
  writeKey('local', LEGACY_RECOVERY_KEY, '')
}

// 错误类型 → 处置决策：action ∈ stop|retry；clear 表示是否清理恢复键
export function decideRecoveryAction(kind) {
  switch (kind) {
    case 'notfound':
      return { action: 'stop', clear: true, reason: '任务不存在或已被删除，已清除本机保存的恢复信息' }
    case 'forbidden':
      return { action: 'stop', clear: true, reason: '当前账号无权访问该任务，已清除本机保存的恢复信息' }
    case 'auth':
      return { action: 'stop', clear: true, reason: '登录状态已失效，请重新登录后查看' }
    case 'access-denied':
      return {
        action: 'stop',
        clear: false,
        reason: '部署访问凭据无效或缺失；登录状态与任务信息均已保留，补齐凭据后刷新页面可继续',
      }
    case 'conflict':
      return { action: 'stop', clear: false, reason: '任务状态冲突，已停止自动查询' }
    case 'validation':
      return { action: 'stop', clear: false, reason: '请求被服务端拒绝，已停止自动查询' }
    default:
      // network / timeout / server / rate-limited / unknown：保留恢复键并重试
      return { action: 'retry', clear: false, reason: '网络或服务端暂时不可用' }
  }
}

// 解析 Retry-After（秒数或 HTTP 日期），返回毫秒；无法解析返回 0
export function parseRetryAfter(value, now = Date.now()) {
  const raw = String(value ?? '').trim()
  if (!raw) return 0
  if (/^\d+(\.\d+)?$/.test(raw)) {
    const seconds = Number(raw)
    return Number.isFinite(seconds) && seconds > 0 ? Math.round(seconds * 1000) : 0
  }
  const at = Date.parse(raw)
  if (!Number.isFinite(at)) return 0
  return Math.max(0, at - now)
}

// 下一次轮询等待时间：遵守服务端 Retry-After，只放大不缩小（不设上限，
// 避免机械缩短服务端要求的等待时间；组件卸载/切换任务会清掉计时器）
export function nextPollDelay(kind, error, baseDelay = ERROR_INTERVAL) {
  const base = Number(baseDelay) > 0 ? Number(baseDelay) : ERROR_INTERVAL
  if (kind !== 'rate-limited') return base
  return Math.max(base, parseRetryAfter(error && error.retryAfter))
}
