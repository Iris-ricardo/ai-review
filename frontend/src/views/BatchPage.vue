<template>
  <div class="batch-page">
    <h2>批量审查</h2>
    <p class="page-note">最多 50 份；相同文件和规则会自动复用正在运行的任务。</p>

    <el-card shadow="never" class="batch-form-card">
      <el-form label-position="top">
        <el-form-item label="规则集">
          <el-select v-model="rulesetId" style="width: 100%" :disabled="batchRunning">
            <el-option v-for="item in rulesets" :key="item.id" :label="item.name" :value="item.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="申报书文件">
          <el-upload
            drag multiple :auto-upload="false" :limit="50" accept=".pdf,.docx"
            :on-change="handleChange" :on-remove="handleRemove" :disabled="batchRunning"
          >
            <el-icon :size="40"><UploadFilled /></el-icon>
            <div>拖拽文件到此处，或点击选择</div>
          </el-upload>
        </el-form-item>
        <div class="ai-options">
          <el-switch v-model="useAi" :disabled="batchRunning" />
          <span>启用 AI 语义检查</span>
        </div>
        <el-checkbox v-if="useAi" v-model="privacyConsent" :disabled="batchRunning" class="consent">
          我已获得全部材料的授权，同意发送至所配置的外部模型服务
        </el-checkbox>
        <el-button type="primary" :loading="submitting" :disabled="!canSubmit" @click="submitBatch">
          {{ batchRunning ? '批次正在运行' : '开始批量审查' }}
        </el-button>
      </el-form>
    </el-card>

    <el-card v-if="batch" shadow="never" class="batch-results">
      <template #header>
        <div class="batch-toolbar">
          <span>完成 {{ batch.done_count }} / {{ batch.total }}</span>
          <el-button v-if="batch.done_count === batch.total" :icon="Download" @click="exportSummary">导出汇总</el-button>
        </div>
      </template>
      <el-progress :percentage="batchPercent" :status="batch.done_count === batch.total ? 'success' : ''" />
      <el-table :data="batch.files" border class="batch-table">
        <el-table-column prop="filename" label="文件名" min-width="220" />
        <el-table-column prop="status" label="状态" width="100" />
        <el-table-column label="进度" width="130">
          <template #default="scope"><el-progress :percentage="scope.row.progress || 0" :show-text="false" /></template>
        </el-table-column>
        <el-table-column prop="current_rule" label="当前规则" width="100" />
        <el-table-column prop="errors" label="错误" width="70" />
        <el-table-column prop="warnings" label="警告" width="70" />
        <el-table-column prop="conclusion" label="结论" width="120" />
        <el-table-column label="操作" width="140">
          <template #default="scope">
            <el-link v-if="scope.row.status === 'done'" type="primary" @click="router.push(`/result/${scope.row.review_id}`)">查看</el-link>
            <el-link v-else-if="['queued','running','cancelling'].includes(scope.row.status)" type="danger" @click="cancelItem(scope.row)">取消</el-link>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Download, UploadFilled } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { cancelReview, createBatch, downloadFile, getBatch, getBatchSummaryUrl, getRulesets } from '../services/api'

const router = useRouter()
const rulesets = ref([])
const rulesetId = ref('campus_general_v1')
const files = ref([])
const useAi = ref(true)
const privacyConsent = ref(false)
const submitting = ref(false)
const batchId = ref('')
const batch = ref(null)
let pollTimer = null

const batchRunning = computed(() => batch.value && batch.value.done_count !== batch.value.total)
const canSubmit = computed(() => files.value.length > 0 && rulesetId.value && !submitting.value && !batchRunning.value && (!useAi.value || privacyConsent.value))
const batchPercent = computed(() => batch.value?.total ? Math.round(batch.value.done_count * 100 / batch.value.total) : 0)

onMounted(async () => {
  try {
    rulesets.value = await getRulesets()
    const saved = localStorage.getItem('activeBatchId')
    if (saved) {
      batchId.value = saved
      await refreshBatch()
      schedulePoll()
    }
  } catch (error) { ElMessage.error(`初始化失败：${error.message}`) }
})
onUnmounted(stopPolling)

function handleChange(_, uploadFiles) { files.value = uploadFiles.map(item => item.raw).filter(Boolean) }
function handleRemove(_, uploadFiles) { files.value = uploadFiles.map(item => item.raw).filter(Boolean) }
function stopPolling() { if (pollTimer) clearTimeout(pollTimer); pollTimer = null }

async function refreshBatch() {
  batch.value = await getBatch(batchId.value)
  if (batch.value.done_count === batch.value.total) localStorage.removeItem('activeBatchId')
}
function schedulePoll(delay = 2500) {
  stopPolling()
  if (!batchRunning.value) return
  pollTimer = setTimeout(async () => {
    pollTimer = null
    try { await refreshBatch(); schedulePoll() }
    catch (error) { ElMessage.error(`批次状态获取失败：${error.message}`); schedulePoll(5000) }
  }, delay)
}
async function submitBatch() {
  if (!canSubmit.value) return
  submitting.value = true
  stopPolling()
  try {
    const created = await createBatch(files.value, rulesetId.value, { useAi: useAi.value, privacyConsent: privacyConsent.value })
    batchId.value = created.batch_id
    localStorage.setItem('activeBatchId', batchId.value)
    await refreshBatch()
    schedulePoll()
  } catch (error) { ElMessage.error(`批量审查失败：${error.message}`) }
  finally { submitting.value = false }
}
async function cancelItem(row) {
  try { await cancelReview(row.review_id); await refreshBatch() }
  catch (error) { ElMessage.error(`取消失败：${error.message}`) }
}
async function exportSummary() {
  try { await downloadFile(getBatchSummaryUrl(batchId.value), `${batchId.value}_批量汇总.xlsx`) }
  catch (error) { ElMessage.error(`导出失败：${error.message}`) }
}
</script>

<style scoped>
.batch-page { max-width: 1180px; margin: 0 auto; text-align: left; }
.page-note { color: #667085; font-size: 14px; }
.batch-form-card { max-width: 760px; margin-bottom: 18px; }
.ai-options { display: flex; gap: 10px; align-items: center; margin-bottom: 8px; }
.consent { white-space: normal; height: auto; margin-bottom: 14px; }
.batch-toolbar { display: flex; justify-content: space-between; align-items: center; }
.batch-table { margin-top: 16px; }
</style>
