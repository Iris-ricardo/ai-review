<template>
  <div class="upload-page">
    <div class="page-heading">
      <div>
        <h2>申报书形式审查</h2>
        <p>任务在后台执行；页面刷新后仍可恢复，不会重复创建相同审查。</p>
      </div>
    </div>

    <!-- R14-7：提交阶段（任务尚未创建）的失败在页面上也要可见，不能只靠吐司 -->
    <el-alert
      v-if="reviewError && !reviewId"
      :title="reviewError"
      type="error"
      :closable="false"
      show-icon
      class="submit-error"
    />

    <el-card shadow="never" class="submit-card">
      <el-form label-position="top">
        <el-form-item label="规则集">
          <el-select v-model="ruleset" placeholder="请选择规则集" style="width: 100%">
            <el-option
              v-for="item in rulesets"
              :key="item.id"
              :label="`${item.name}（${item.rule_count} 条）`"
              :value="item.id"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="申报书文件">
          <el-upload
            class="upload-box"
            drag
            :auto-upload="false"
            :on-change="handleFile"
            :on-remove="clearFile"
            :limit="1"
            accept=".pdf,.docx"
            :disabled="isActive"
          >
            <el-icon :size="44"><UploadFilled /></el-icon>
            <div>将 PDF/DOCX 拖到此处，或点击选择</div>
            <template #tip>
              <div class="upload-tip">最大 50MB；扫描版 PDF 请先完成 OCR。</div>
            </template>
          </el-upload>
        </el-form-item>

        <div class="ai-options">
          <el-switch v-model="useAi" :disabled="isActive" />
          <div>
            <div class="option-title">启用 AI 语义检查</div>
            <div class="option-note">关闭后只运行本地规则，结论会标记为“不完整”。</div>
          </div>
        </div>
        <el-checkbox v-if="useAi" v-model="privacyConsent" :disabled="isActive" class="consent">
          我已获得授权，同意将申报书正文发送至所配置的外部模型服务进行检查
        </el-checkbox>

        <div class="actions">
          <el-button
            type="primary"
            size="large"
            :disabled="!canSubmit"
            :loading="submitting"
            @click="startReview"
          >
            {{ isActive ? '审查正在运行' : '开始审查' }}
          </el-button>
          <el-button v-if="isActive" type="danger" plain @click="requestCancel">
            取消任务
          </el-button>
        </div>
      </el-form>
    </el-card>

    <el-card v-if="reviewId" shadow="never" class="progress-card">
      <div class="status-header">
        <div>
          <div class="status-title">{{ statusTitle }}</div>
          <div class="task-id">任务 {{ reviewId }}</div>
        </div>
        <el-tag :type="statusTagType">{{ statusLabel }}</el-tag>
      </div>

      <el-alert
        v-if="restoreError"
        :title="restoreError"
        type="warning"
        :closable="false"
        show-icon
      >
        <div class="restore-actions">
          <el-button size="small" type="primary" plain @click="retryRestore">重试</el-button>
          <el-button size="small" plain @click="abandonRestore">放弃恢复</el-button>
        </div>
      </el-alert>
      <el-alert
        v-if="reviewError"
        :title="reviewError"
        type="error"
        :closable="false"
        show-icon
      />
      <el-alert
        v-else-if="stale && isActive"
        title="任务长时间没有新心跳，可能正在等待模型响应；可取消后重新提交。"
        type="warning"
        :closable="false"
        show-icon
      />

      <el-progress :percentage="progress" :status="progressStatus" :stroke-width="12" />
      <div class="stage-line">
        <span>阶段：{{ stageLabel }}</span>
        <span v-if="currentRule">当前规则：{{ currentRule }}</span>
        <span>已完成：{{ rulesCompleted }} / {{ rulesTotal || '—' }}</span>
      </div>

      <div v-if="ruleRows.length" class="rule-grid">
        <div v-for="rule in ruleRows" :key="rule.id" class="rule-row">
          <span class="rule-id">{{ rule.id }}</span>
          <span class="rule-description">{{ rule.description || rule.type }}</span>
          <el-tag size="small" :type="ruleTagType(rule.status)">
            {{ ruleStatusLabel(rule.status) }}
          </el-tag>
        </div>
      </div>

      <div class="result-actions" v-if="reviewStatus === 'done'">
        <el-alert
          v-if="conclusion === 'incomplete'"
          title="审查已结束，但存在未执行或需人工确认的检查，不能判定为通过。"
          type="warning"
          :closable="false"
        />
        <el-button type="success" @click="goResult">查看审查结果</el-button>
      </div>
    </el-card>

    <el-card shadow="never" class="history-card">
      <template #header><span>最近任务</span></template>
      <el-empty v-if="history.length === 0" description="暂无任务" :image-size="70" />
      <el-table v-else :data="history" size="small">
        <el-table-column prop="review_id" label="任务 ID" min-width="130" />
        <el-table-column prop="ruleset_id" label="规则集" min-width="150" />
        <el-table-column prop="status" label="状态" width="100" />
        <el-table-column prop="progress" label="进度" width="80" />
        <el-table-column prop="created_at" label="创建时间" min-width="160" />
        <el-table-column label="操作" width="90">
          <template #default="scope">
            <el-link type="primary" @click="restoreReview(scope.row.review_id)">打开</el-link>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { UploadFilled } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import {
  cancelReview,
  classifyError,
  createReview,
  getCurrentUser,
  getReview,
  getRulesets,
  listReviews,
  uploadDocument,
} from '../services/api'
import {
  ERROR_INTERVAL,
  POLL_INTERVAL,
  STALE_INTERVAL,
  clearActiveReview,
  clearLegacyRecoveryKey,
  decideRecoveryAction,
  loadActiveReview,
  nextPollDelay,
  saveActiveReview,
  userIdOf,
} from '../services/reviewRecovery'

const router = useRouter()
const rulesets = ref([])
const ruleset = ref('campus_general_v1')
const file = ref(null)
const useAi = ref(true)
const privacyConsent = ref(false)
const submitting = ref(false)
const reviewId = ref('')
const reviewStatus = ref('')
const progress = ref(0)
const stage = ref('')
const currentRule = ref('')
const rulesCompleted = ref(0)
const rulesTotal = ref(0)
const ruleStatuses = ref({})
const reviewError = ref('')
const restoreError = ref('')
const conclusion = ref('')
const lastActivityAt = ref('')
const stale = ref(false)
const history = ref([])
// R14：恢复键按用户隔离，用户标识在挂载时从已登录会话取一次并留存，
// 便于会话失效/登出后仍能清理「该用户」的恢复信息
const sessionUserId = ref(userIdOf(getCurrentUser()))
let pendingRestoreId = ''
let pollTimer = null
// 每次停止轮询/卸载都自增，用来判定迟到的响应是否已过期
let pollSession = 0
let isUnmounted = false

function activeUserId() {
  return userIdOf(getCurrentUser()) || sessionUserId.value
}

const activeStatuses = ['queued', 'running', 'cancelling']
const isActive = computed(() => activeStatuses.includes(reviewStatus.value))
const canSubmit = computed(() => (
  file.value && ruleset.value && !submitting.value && !isActive.value
  && (!useAi.value || privacyConsent.value)
))
const ruleRows = computed(() => Object.entries(ruleStatuses.value || {}).map(([id, value]) => ({ id, ...value })))
const progressStatus = computed(() => {
  if (['failed', 'timed_out', 'cancelled'].includes(reviewStatus.value)) return 'exception'
  if (reviewStatus.value === 'done') return conclusion.value === 'pass' ? 'success' : 'warning'
  return ''
})
const statusTagType = computed(() => {
  if (reviewStatus.value === 'done') return conclusion.value === 'pass' ? 'success' : 'warning'
  if (['failed', 'timed_out', 'cancelled'].includes(reviewStatus.value)) return 'danger'
  return 'primary'
})
const statusLabel = computed(() => ({
  queued: '排队中', running: '运行中', cancelling: '取消中', cancelled: '已取消',
  timed_out: '已超时', failed: '失败', done: '已完成',
}[reviewStatus.value] || reviewStatus.value || '等待提交'))
const statusTitle = computed(() => {
  if (reviewStatus.value === 'done') {
    return conclusion.value === 'pass' ? '审查通过' : conclusion.value === 'needs_revision' ? '发现需要修改的问题' : '审查不完整'
  }
  if (!reviewStatus.value) return restoreError.value ? '任务状态未知' : '等待提交'
  return isActive.value ? '任务正在后台执行' : '任务已结束'
})
const stageLabel = computed(() => ({
  queued: '等待工作线程', recovered: '服务重启后恢复', parsing: '解析文档', rules: '准备规则',
  rule_check: '本地规则检查', ai_check: 'AI 语义检查', llm_attempt_1: '模型请求（第 1 次）',
  llm_attempt_2: '模型请求（重试）', llm_request_started: '等待模型响应',
  llm_response_received: '处理模型响应', finalizing: '汇总结果', completed: '完成',
  cancelling: '正在取消', cancelled: '已取消', failed: '失败', timed_out: '超时',
}[stage.value] || stage.value || '—'))

onMounted(async () => {
  sessionUserId.value = userIdOf(getCurrentUser())
  try {
    rulesets.value = await getRulesets()
    if (!rulesets.value.some(item => item.id === ruleset.value) && rulesets.value.length) {
      ruleset.value = rulesets.value[0].id
    }
    await refreshHistory()
  } catch (error) {
    ElMessage.error(`初始化失败：${error.message}`)
  }
  if (isUnmounted) return
  // R14-3：旧版无作用域键无法判断归属，直接清理；只恢复「当前用户」的键
  clearLegacyRecoveryKey()
  const savedId = loadActiveReview(activeUserId())
  if (savedId) await restoreReview(savedId, false)
})

onUnmounted(() => {
  isUnmounted = true
  stopPolling()
})

function handleFile(uploadFile) { file.value = uploadFile.raw || null }
function clearFile() { file.value = null }
function stopPolling() {
  if (pollTimer) clearTimeout(pollTimer)
  pollTimer = null
  // 使已经发出的请求在返回时失效：卸载/切换任务后迟到响应不得重启轮询
  pollSession += 1
}

function applyReview(data) {
  reviewId.value = data.review_id
  reviewStatus.value = data.status || ''
  progress.value = Number(data.progress || 0)
  stage.value = data.stage || ''
  currentRule.value = data.current_rule || ''
  rulesCompleted.value = Number(data.rules_completed || 0)
  rulesTotal.value = Number(data.rules_total || 0)
  ruleStatuses.value = data.rule_statuses || {}
  reviewError.value = data.error || ''
  conclusion.value = data.conclusion || ''
  lastActivityAt.value = data.last_activity_at || ''
  stale.value = isStale(data.last_activity_at)
  // R14-1/2/3：运行中的任务只写「本用户」的恢复键；任务进入终态才清理它
  if (activeStatuses.includes(data.status)) saveActiveReview(data.review_id, activeUserId())
  else clearActiveReview(activeUserId())
}

function isStale(value) {
  if (!value) return false
  const parsed = Date.parse(value.replace(' ', 'T'))
  return Number.isFinite(parsed) && Date.now() - parsed > 180000
}

async function startReview() {
  if (!canSubmit.value) return
  submitting.value = true
  stopPolling()
  reviewError.value = ''
  restoreError.value = ''
  pendingRestoreId = ''
  let created
  try {
    const uploaded = await uploadDocument(file.value)
    progress.value = 3
    created = await createReview(uploaded.document_id, ruleset.value, {
      useAi: useAi.value,
      privacyConsent: privacyConsent.value,
    })
  } catch (error) {
    // 到这里任务确实还没创建，才是「提交失败」
    reviewError.value = `提交失败：${error.message}`
    ElMessage.error(`提交失败：${error.message}`)
    submitting.value = false
    return
  }
  if (isUnmounted) return
  // R14-7：任务已经创建——之后的任何失败都不能再提示「提交失败」
  reviewId.value = created.review_id
  reviewStatus.value = created.status || 'queued'
  saveActiveReview(reviewId.value, activeUserId())
  if (created.reused) ElMessage.info('已复用相同文件正在运行的审查任务')
  try {
    await pollOnce(reviewId.value)
  } catch (error) {
    if (!isUnmounted) handlePollFailure(error, reviewId.value, { created: true })
    await refreshHistorySafe()
    submitting.value = false
    return
  }
  if (!isUnmounted) schedulePoll()
  await refreshHistorySafe()
  submitting.value = false
}

async function pollOnce(targetId) {
  const data = await getReview(targetId)
  // R14-5：任务已被切换/组件已卸载时，迟到响应不得覆盖新任务状态
  if (isUnmounted || reviewId.value !== targetId) return
  applyReview(data)
}

// R14-8：按错误类型决定停止、提示还是重试；顺带给出是否清理恢复键
function handlePollFailure(error, targetId, { created = false } = {}) {
  const kind = classifyError(error)
  const decision = decideRecoveryAction(kind)
  const lead = created ? `任务已创建（${targetId}），状态获取失败` : '获取任务状态失败'
  const retryNote = decision.action === 'retry' ? '，将自动重试' : ''
  reviewError.value = `${lead}：${error.message}（${decision.reason}${retryNote}）`
  if (decision.clear) clearActiveReview(activeUserId())
  // 已创建的任务即便状态获取失败也要明确提示「任务已创建」，不能只说请求失败
  if (created) ElMessage.warning(`${lead}：${error.message}${retryNote}`)
  if (decision.action === 'retry') {
    schedulePoll(nextPollDelay(kind, error, ERROR_INTERVAL))
    return true
  }
  if (!created) ElMessage.error(`${lead}：${error.message}`)
  return false
}

function schedulePoll(delay = POLL_INTERVAL) {
  if (isUnmounted) return
  stopPolling()
  if (!isActive.value || !reviewId.value) return
  const targetId = reviewId.value
  const session = pollSession
  pollTimer = setTimeout(async () => {
    pollTimer = null
    try {
      await pollOnce(targetId)
      // R14-6：卸载或任务已切换时，不得由迟到响应重新排轮询
      if (isUnmounted || session !== pollSession) return
      if (!isActive.value) await refreshHistorySafe()
      if (isUnmounted || session !== pollSession) return
      schedulePoll(stale.value ? STALE_INTERVAL : POLL_INTERVAL)
    } catch (error) {
      if (isUnmounted || session !== pollSession) return
      handlePollFailure(error, targetId)
    }
  }, delay)
}

async function requestCancel() {
  try {
    applyReview(await cancelReview(reviewId.value))
    if (isUnmounted) return
    schedulePoll(500)
  } catch (error) {
    ElMessage.error(`取消失败：${error.message}`)
  }
}

async function restoreReview(id, notify = true) {
  stopPolling()
  const targetId = id
  pendingRestoreId = targetId
  restoreError.value = ''
  reviewError.value = ''
  // 先展示任务编号：即使状态获取失败，可恢复信息也不会丢
  reviewId.value = targetId
  try {
    const data = await getReview(targetId)
    if (isUnmounted || reviewId.value !== targetId) return
    applyReview(data)
    pendingRestoreId = ''
    if (isActive.value) schedulePoll(500)
    if (notify) ElMessage.success('已打开任务')
  } catch (error) {
    if (isUnmounted || reviewId.value !== targetId) return
    // R14-1/2/8：瞬时错误（断网/超时/5xx/429）保留恢复键并给可重试提示；
    // 只有任务不存在(404)、无权限(403)、会话失效(401) 才清理该用户的恢复键
    const kind = classifyError(error)
    const decision = decideRecoveryAction(kind)
    if (decision.clear) clearActiveReview(activeUserId())
    restoreError.value = `无法获取任务状态：${error.message}（${decision.reason}）`
    if (notify) ElMessage.error(`打开任务失败：${error.message}`)
  }
}

function retryRestore() {
  const targetId = pendingRestoreId || reviewId.value
  if (!targetId) return
  return restoreReview(targetId, true)
}

// R14-2：用户主动放弃恢复 → 清理该用户的恢复信息
function abandonRestore() {
  clearActiveReview(activeUserId())
  pendingRestoreId = ''
  restoreError.value = ''
  reviewError.value = ''
  stopPolling()
  reviewId.value = ''
  reviewStatus.value = ''
  ElMessage.info('已放弃恢复该任务')
}

async function refreshHistory() {
  history.value = await listReviews(20)
}

async function refreshHistorySafe() {
  try {
    await refreshHistory()
  } catch (error) {
    ElMessage.warning(`任务列表刷新失败：${error.message}`)
  }
}

function goResult() { router.push(`/result/${reviewId.value}`) }
function ruleTagType(status) {
  if (status === 'done') return 'success'
  if (['failed', 'timed_out', 'cancelled'].includes(status)) return 'danger'
  if (['degraded', 'skipped'].includes(status)) return 'warning'
  return 'info'
}
function ruleStatusLabel(status) {
  return { pending: '等待', running: '执行中', done: '完成', degraded: '降级', skipped: '已跳过', failed: '失败', timed_out: '超时', cancelled: '取消' }[status] || status
}
</script>

<style scoped>
.upload-page { max-width: 920px; margin: 0 auto; text-align: left; }
.page-heading p { margin: 4px 0 18px; color: #667085; font-size: 14px; }
.submit-card, .progress-card, .history-card { margin-bottom: 18px; }
.submit-error { margin-bottom: 14px; }
.upload-box { width: 100%; }
.upload-tip { color: #667085; }
.ai-options { display: flex; align-items: flex-start; gap: 12px; padding: 12px; background: #f7f9fc; border-radius: 8px; }
.option-title { font-weight: 600; color: #303133; }
.option-note, .task-id { color: #909399; font-size: 12px; }
.consent { margin: 12px 0; white-space: normal; height: auto; }
.actions { display: flex; gap: 10px; margin-top: 8px; }
.status-header, .stage-line { display: flex; justify-content: space-between; gap: 14px; align-items: center; }
.status-header { margin-bottom: 18px; }
.status-title { font-size: 18px; font-weight: 650; color: #303133; }
.stage-line { margin-top: 10px; color: #606266; font-size: 13px; flex-wrap: wrap; }
.rule-grid { margin-top: 16px; border: 1px solid #ebeef5; border-radius: 8px; overflow: hidden; }
.rule-row { display: grid; grid-template-columns: 64px 1fr 74px; gap: 10px; align-items: center; padding: 8px 12px; border-bottom: 1px solid #ebeef5; font-size: 13px; }
.rule-row:last-child { border-bottom: 0; }
.rule-id { font-family: Consolas, monospace; color: #409eff; }
.rule-description { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.result-actions { margin-top: 16px; display: grid; gap: 12px; justify-items: start; }
.restore-actions { margin-top: 6px; display: flex; gap: 8px; }
@media (max-width: 700px) { .stage-line { display: grid; } .rule-row { grid-template-columns: 56px 1fr; } .rule-row .el-tag { grid-column: 2; width: fit-content; } }
</style>
