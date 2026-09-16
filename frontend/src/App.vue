<template>
  <div id="app">
    <el-container>
      <el-header class="app-header">
        <div class="brand">AI 项目申报书智能形式审查系统</div>
        <el-menu mode="horizontal" :default-active="$route.path" router>
          <el-menu-item v-if="currentUser" index="/dashboard">工作台</el-menu-item>
          <el-menu-item v-if="can('review.create')" index="/review">单份审查</el-menu-item>
          <el-menu-item v-if="can('review.batch')" index="/batch">批量审查</el-menu-item>
          <el-menu-item v-if="can('ruleset.view')" index="/rules">规则集管理</el-menu-item>
          <el-menu-item v-if="can('users.manage')" index="/users">用户管理</el-menu-item>
        </el-menu>
        <div class="system-actions">
          <el-tag size="small" :type="healthType">{{ healthText }}</el-tag>
          <el-tag v-if="currentUser" size="small" type="success">{{ currentUser.role_label }}</el-tag>
          <el-button v-if="currentUser" size="small" plain @click="logoutCurrent">
            退出
          </el-button>
          <el-button v-else size="small" plain @click="$router.push('/login')">
            登录
          </el-button>
          <el-button v-if="accessControlled" size="small" plain @click="tokenDialog = true">
            访问凭据
          </el-button>
        </div>
      </el-header>
      <el-main>
        <router-view />
      </el-main>
    </el-container>
    <el-dialog v-model="tokenDialog" title="访问凭据" width="min(460px, 92vw)">
      <el-alert title="凭据只保存在当前浏览器会话，关闭窗口后自动清除。" type="info" :closable="false" />
      <el-input v-model="tokenInput" type="password" show-password placeholder="输入 ACCESS_TOKEN" class="token-input" />
      <template #footer>
        <el-button @click="clearToken">清除</el-button>
        <el-button type="primary" @click="saveToken">保存并验证</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  getAccessToken, getCurrentUser, getHealth, getMe, getRulesets, logout,
  setAccessToken,
} from './services/api'
import { clearActiveReview, userIdOf } from './services/reviewRecovery'

const router = useRouter()
const health = ref(null)
const currentUser = ref(getCurrentUser())
const tokenDialog = ref(false)
const tokenInput = ref(getAccessToken())
const accessControlled = computed(() => Boolean(health.value?.checks?.access_control))
const healthType = computed(() => health.value?.status === 'ok' ? 'success' : health.value ? 'warning' : 'info')
const healthText = computed(() => health.value?.status === 'ok'
  ? `服务正常 · ${health.value.version}`
  : health.value ? '服务降级' : '检测服务')

onMounted(async () => {
  window.addEventListener('auth-changed', syncCurrentUser)
  window.addEventListener('access-invalid', onAccessInvalid)
  try {
    health.value = await getHealth()
    if (accessControlled.value && !getAccessToken()) tokenDialog.value = true
  } catch {
    health.value = { status: 'degraded', checks: {} }
  }
  try {
    currentUser.value = await getMe()
  } catch {
    currentUser.value = null
  }
})

onBeforeUnmount(() => {
  window.removeEventListener('auth-changed', syncCurrentUser)
  window.removeEventListener('access-invalid', onAccessInvalid)
})

function syncCurrentUser() {
  currentUser.value = getCurrentUser()
}

// B7a：部署访问凭据错误单独提示，不影响有效登录会话
function onAccessInvalid(event) {
  if (!accessControlled.value) return
  tokenDialog.value = true
  const detail = event?.detail
  ElMessage.error(`访问凭据无效或缺失${detail?.message ? `：${detail.message}` : ''}`)
}

function can(capability) {
  return currentUser.value?.capabilities?.includes(capability)
}

async function logoutCurrent() {
  // R14：登出属于「用户主动放弃」——清理当前用户的任务恢复键，
  // 避免换账号后打开上一账号的任务（api.logout 内也会兜底清理）
  clearActiveReview(userIdOf(getCurrentUser()) || userIdOf(currentUser.value))
  await logout()
  currentUser.value = null
  ElMessage.success('已退出登录')
  router.push('/login')
}

async function saveToken() {
  setAccessToken(tokenInput.value)
  try {
    await getRulesets()
    tokenDialog.value = false
    ElMessage.success('访问凭据有效，正在重新加载')
    window.location.reload()
  } catch (error) {
    ElMessage.error(`验证失败：${error.message}`)
  }
}

function clearToken() {
  tokenInput.value = ''
  setAccessToken('')
}
</script>

<style>
body { margin: 0; font-family: 'Microsoft YaHei', 'PingFang SC', sans-serif; background: #f4f7fb; color: #303133; }
.app-header { display: flex; align-items: center; gap: 22px; background: #155b97; min-height: 64px; height: auto; padding: 0 24px; }
.brand { color: #fff; font-size: 17px; font-weight: 650; white-space: nowrap; }
.app-header .el-menu { flex: 1; background: transparent; border: none; }
.app-header .el-menu-item { color: #d0e4f5; }
.app-header .el-menu-item.is-active { color: #fff; border-bottom-color: #fff; }
.system-actions { display: flex; align-items: center; gap: 8px; white-space: nowrap; }
.token-input { margin-top: 16px; }
.el-main { padding: 24px; }
@media (max-width: 820px) {
  .app-header { flex-wrap: wrap; gap: 4px 12px; padding: 10px 14px; }
  .brand { width: 100%; }
  .app-header .el-menu { order: 3; width: 100%; overflow-x: auto; }
  .system-actions { margin-left: auto; }
  .el-main { padding: 14px; }
}
</style>
