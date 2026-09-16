<template>
  <div class="result-page" v-loading="loading">
    <el-alert
      v-if="loadError"
      :title="loadError"
      type="error"
      :closable="false"
      show-icon
    />

    <template v-else-if="review">
      <div class="result-header">
        <el-alert
          :title="conclusionText"
          :description="conclusionDescription"
          :type="conclusionType"
          :closable="false"
          show-icon
        />
        <div class="download-actions" v-if="review.status === 'done'">
          <el-button @click="downloadAnnotated">下载批注 PDF</el-button>
          <el-button type="primary" @click="downloadReport">下载审查报告</el-button>
        </div>
      </div>

      <el-row :gutter="16" class="stats-row">
        <el-col :xs="12" :sm="6"><el-statistic title="总问题" :value="issues.length" /></el-col>
        <el-col :xs="12" :sm="6"><el-statistic title="错误" :value="errorCount" /></el-col>
        <el-col :xs="12" :sm="6"><el-statistic title="警告" :value="warnCount" /></el-col>
        <el-col :xs="12" :sm="6"><el-statistic title="提示/人工确认" :value="infoCount" /></el-col>
      </el-row>

      <el-row :gutter="18" class="main-row">
        <el-col :xs="24" :lg="13">
          <el-card shadow="never" class="pdf-card">
            <template #header>
              <div class="pdf-toolbar">
                <span>原文定位</span>
                <el-button-group>
                  <el-button :disabled="currentPage <= 1" @click="changePage(-1)">上一页</el-button>
                  <el-button disabled>{{ currentPage }} / {{ totalPages || '—' }}</el-button>
                  <el-button :disabled="currentPage >= totalPages" @click="changePage(1)">下一页</el-button>
                </el-button-group>
              </div>
            </template>
            <div class="pdf-container">
              <canvas ref="pdfCanvas" />
              <el-empty v-if="!pdfDoc && !pdfError" description="正在加载 PDF" />
              <el-alert v-if="pdfError" :title="pdfError" type="error" :closable="false" />
            </div>
          </el-card>
        </el-col>

        <el-col :xs="24" :lg="11">
          <el-card shadow="never" class="issues-card">
            <template #header><span>问题清单</span></template>
            <el-tabs v-model="activeTab">
              <el-tab-pane :label="`全部 ${issues.length}`" name="all" />
              <el-tab-pane :label="`错误 ${errorCount}`" name="error" />
              <el-tab-pane :label="`警告 ${warnCount}`" name="warning" />
              <el-tab-pane :label="`提示 ${infoCount}`" name="info" />
            </el-tabs>
            <el-empty v-if="filteredIssues.length === 0" description="当前分类没有问题" />
            <div v-else class="issue-list">
              <div
                v-for="(issue, index) in filteredIssues"
                :key="`${issue.rule_id}-${index}-${issue.message}`"
                class="issue-item"
                :class="`severity-${issue.severity}`"
                @click="goToPage(issue.page)"
              >
                <div class="issue-title-row">
                  <el-tag :type="severityTag(issue.severity)" size="small">{{ severityLabel(issue.severity) }}</el-tag>
                  <span class="rule-label">{{ issue.rule_id }} · {{ sourceLabel(issue) }}</span>
                  <span v-if="issue.page" class="issue-page">第 {{ issue.page }} 页</span>
                </div>
                <div class="issue-message">{{ issue.message }}</div>
                <div v-if="issue.evidence" class="issue-evidence">原文：{{ issue.evidence }}</div>
                <div v-if="issue.suggestion" class="issue-suggestion">建议：{{ issue.suggestion }}</div>
              </div>
            </div>
          </el-card>
        </el-col>
      </el-row>
    </template>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, ref, shallowRef } from 'vue'
import { ElMessage } from 'element-plus'
import * as pdfjsLib from 'pdfjs-dist'
import { accessHeaders, downloadFile, getAnnotatedPdfUrl, getDocumentFileUrl, getReview, getReviewIssues, getReviewReportUrl } from '../services/api'

pdfjsLib.GlobalWorkerOptions.workerSrc = new URL(
  'pdfjs-dist/build/pdf.worker.min.mjs',
  import.meta.url,
).toString() + '?v=20260719'

const props = defineProps({ taskId: String })
const loading = ref(true)
const loadError = ref('')
const pdfError = ref('')
const review = ref(null)
const issues = ref([])
const currentPage = ref(1)
const totalPages = ref(0)
// PDF.js instances contain native private fields and must not be wrapped by
// Vue's deep reactive proxy.
const pdfDoc = shallowRef(null)
const pdfCanvas = ref(null)
const activeTab = ref('all')
const renderTask = shallowRef(null)
const renderScale = 1.15

const errorCount = computed(() => issues.value.filter(item => item.severity === 'error').length)
const warnCount = computed(() => issues.value.filter(item => item.severity === 'warning').length)
const infoCount = computed(() => issues.value.filter(item => item.severity === 'info').length)
const filteredIssues = computed(() => activeTab.value === 'all'
  ? issues.value
  : issues.value.filter(item => item.severity === activeTab.value))
const annotatedUrl = computed(() => getAnnotatedPdfUrl(props.taskId))
const reportUrl = computed(() => getReviewReportUrl(props.taskId))
const conclusionType = computed(() => {
  if (review.value?.conclusion === 'pass') return 'success'
  if (review.value?.conclusion === 'needs_revision') return 'error'
  return 'warning'
})
const conclusionText = computed(() => ({
  pass: '审查结论：通过',
  needs_revision: '审查结论：需要修改',
  incomplete: '审查结论：不完整，不能判定通过',
}[review.value?.conclusion] || `任务状态：${review.value?.status || '未知'}`))
const conclusionDescription = computed(() => {
  if (review.value?.conclusion === 'incomplete') return '存在 AI 降级、规则失败、跳过项或必须人工确认的项目。'
  return `耗时 ${review.value?.duration_seconds ?? '—'} 秒，共发现 ${issues.value.length} 项。`
})

onMounted(async () => {
  try {
    review.value = await getReview(props.taskId)
    if (review.value.status !== 'done') {
      throw new Error(`任务尚未完成，当前状态：${review.value.status}`)
    }
    const result = await getReviewIssues(props.taskId)
    issues.value = result.issues || []
    review.value.conclusion = result.conclusion || review.value.conclusion
    await nextTick()
    await loadPdf(getDocumentFileUrl(review.value.document_id))
  } catch (error) {
    loadError.value = error.message
  } finally {
    loading.value = false
  }
})

async function loadPdf(url) {
  try {
    pdfDoc.value = await pdfjsLib.getDocument({ url, httpHeaders: accessHeaders() }).promise
    totalPages.value = pdfDoc.value.numPages
    await renderPage()
  } catch (error) {
    pdfError.value = `PDF 加载失败：${error.message}`
  }
}

async function downloadAnnotated() {
  try { await downloadFile(annotatedUrl.value, `${props.taskId}_批注.pdf`) }
  catch (error) { ElMessage.error(`下载失败：${error.message}`) }
}
async function downloadReport() {
  try { await downloadFile(reportUrl.value, `${props.taskId}_审查报告.pdf`) }
  catch (error) { ElMessage.error(`下载失败：${error.message}`) }
}

async function renderPage() {
  if (!pdfDoc.value || !pdfCanvas.value) return
  if (renderTask.value) renderTask.value.cancel()
  try {
    const page = await pdfDoc.value.getPage(currentPage.value)
    const viewport = page.getViewport({ scale: renderScale })
    const canvas = pdfCanvas.value
    const context = canvas.getContext('2d')
    canvas.width = viewport.width
    canvas.height = viewport.height
    renderTask.value = page.render({ canvasContext: context, viewport })
    await renderTask.value.promise
    drawHighlights(context)
  } catch (error) {
    if (error?.name !== 'RenderingCancelledException') pdfError.value = `页面渲染失败：${error.message}`
  }
}

function drawHighlights(context) {
  const pageIssues = issues.value.filter(item => item.page === currentPage.value && Array.isArray(item.bbox) && item.bbox.length === 4)
  for (const issue of pageIssues) {
    const [x0, y0, x1, y1] = issue.bbox.map(value => Number(value) * renderScale)
    context.save()
    context.fillStyle = issue.severity === 'error' ? 'rgba(245, 108, 108, .20)' : 'rgba(230, 162, 60, .20)'
    context.strokeStyle = issue.severity === 'error' ? '#f56c6c' : '#e6a23c'
    context.lineWidth = 2
    context.fillRect(x0, y0, x1 - x0, y1 - y0)
    context.strokeRect(x0, y0, x1 - x0, y1 - y0)
    context.restore()
  }
}

function goToPage(page) {
  if (!page || page < 1 || page > totalPages.value) return
  currentPage.value = page
  renderPage()
}
function changePage(delta) { goToPage(currentPage.value + delta) }
function severityTag(value) { return value === 'error' ? 'danger' : value === 'warning' ? 'warning' : 'info' }
function severityLabel(value) { return { error: '错误', warning: '警告', info: '提示' }[value] || value }
function sourceLabel(issue) {
  if (issue.confidence === 'manual_required') return '需人工确认'
  if (issue.layer === 'llm' || issue.confidence === 'ai') return 'AI'
  if (issue.layer === 'system') return '系统'
  return '规则'
}
</script>

<style scoped>
.result-page { max-width: 1480px; margin: 0 auto; text-align: left; }
.result-header { display: grid; grid-template-columns: 1fr auto; gap: 16px; align-items: center; }
.download-actions { display: flex; gap: 8px; }
.stats-row { margin: 18px 0; }
.main-row { align-items: stretch; }
.pdf-card, .issues-card { height: 760px; }
.pdf-toolbar { display: flex; align-items: center; justify-content: space-between; }
.pdf-container { height: 675px; overflow: auto; background: #eef1f5; text-align: center; border-radius: 6px; padding: 12px; }
.pdf-container canvas { max-width: 100%; box-shadow: 0 3px 14px rgba(0,0,0,.16); background: white; }
.issue-list { max-height: 620px; overflow-y: auto; }
.issue-item { padding: 12px; margin-bottom: 10px; border: 1px solid #ebeef5; border-left-width: 4px; border-radius: 6px; cursor: pointer; }
.issue-item:hover { background: #f7f9fc; }
.severity-error { border-left-color: #f56c6c; }
.severity-warning { border-left-color: #e6a23c; }
.severity-info { border-left-color: #909399; }
.issue-title-row { display: flex; align-items: center; gap: 8px; }
.rule-label { font-size: 12px; color: #909399; }
.issue-page { margin-left: auto; color: #409eff; font-size: 12px; }
.issue-message { margin-top: 9px; color: #303133; font-weight: 600; }
.issue-evidence, .issue-suggestion { margin-top: 7px; color: #606266; font-size: 13px; line-height: 1.55; overflow-wrap: anywhere; }
.issue-suggestion { color: #337ecc; }
@media (max-width: 1000px) { .result-header { grid-template-columns: 1fr; } .pdf-card, .issues-card { height: auto; margin-bottom: 16px; } .pdf-container { height: 600px; } }
</style>
