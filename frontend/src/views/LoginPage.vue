<template>
  <div class="login-page">
    <el-card shadow="never" class="login-card">
      <template v-if="!forcedChange">
        <h2>登录系统</h2>
        <p>本地演示账号用于区分普通用户、规则维护、审核和管理权限。</p>
        <el-form label-position="top" @submit.prevent>
          <el-form-item label="用户名">
            <el-input v-model="username" autocomplete="username" />
          </el-form-item>
          <el-form-item label="密码">
            <el-input v-model="password" type="password" show-password autocomplete="current-password" />
          </el-form-item>
          <el-button type="primary" :loading="submitting" @click="submit">登录</el-button>
        </el-form>
        <div class="demo-users">
          <div class="demo-title">演示账号</div>
          <el-tag v-for="item in demoUsers" :key="item.username" @click="fill(item)">
            {{ item.label }}：{{ item.username }}
          </el-tag>
        </div>
      </template>

      <template v-else>
        <h2>请修改初始密码</h2>
        <p>账号 {{ username }} 的密码由管理员重置或属于初始密码，必须先修改后才能使用系统。</p>
        <el-form label-position="top" @submit.prevent>
          <el-form-item label="原密码（临时密码）">
            <el-input v-model="oldPassword" type="password" show-password autocomplete="current-password" />
          </el-form-item>
          <el-form-item label="新密码（至少 8 位）">
            <el-input v-model="newPassword" type="password" show-password autocomplete="new-password" />
          </el-form-item>
          <el-form-item label="确认新密码">
            <el-input v-model="confirmPassword" type="password" show-password autocomplete="new-password" />
          </el-form-item>
          <el-button type="primary" :loading="submitting" @click="submitChange">修改并继续</el-button>
        </el-form>
      </template>
    </el-card>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { changePassword, login, logout } from '../services/api'

const router = useRouter()
const route = useRoute()
const username = ref('admin')
const password = ref('admin123456')
const oldPassword = ref('')
const newPassword = ref('')
const confirmPassword = ref('')
const submitting = ref(false)
const forcedChange = ref(route.query.mustChange === '1')
const demoUsers = [
  { label: '普通用户', username: 'user', password: 'user123456' },
  { label: '规则维护员', username: 'maintainer', password: 'maintainer123456' },
  { label: '规则审核员', username: 'reviewer', password: 'reviewer123456' },
  { label: '管理员', username: 'admin', password: 'admin123456' },
  { label: '超级管理员', username: 'superadmin', password: 'superadmin123456' },
]

function fill(item) {
  username.value = item.username
  password.value = item.password
}

async function submit() {
  submitting.value = true
  try {
    const user = await login(username.value, password.value)
    if (user.must_change_password) {
      // B7b：登录成功但服务端要求强制改密（能力受限）→ 进入改密界面
      oldPassword.value = password.value
      forcedChange.value = true
      ElMessage.warning('该账号需要先修改初始密码')
      return
    }
    ElMessage.success(`已登录：${user.display_name || user.username}`)
    router.push(String(route.query.redirect || '/'))
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    submitting.value = false
  }
}

async function submitChange() {
  if (newPassword.value.length < 8) {
    ElMessage.error('新密码至少需要 8 位')
    return
  }
  if (newPassword.value !== confirmPassword.value) {
    ElMessage.error('两次输入的新密码不一致')
    return
  }
  submitting.value = true
  try {
    await changePassword(oldPassword.value, newPassword.value)
    // 改密后服务端撤销了全部会话 → 用新密码重新登录
    await logout()
    password.value = newPassword.value
    oldPassword.value = ''
    newPassword.value = ''
    confirmPassword.value = ''
    forcedChange.value = false
    ElMessage.success('密码已修改，请用新密码登录')
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    submitting.value = false
  }
}
</script>

<style scoped>
.login-page { min-height: calc(100vh - 120px); display: grid; place-items: center; }
.login-card { width: min(440px, 94vw); border-radius: 8px; }
.login-card h2 { margin: 0 0 6px; color: #17324d; }
.login-card p { margin: 0 0 18px; color: #6f8498; line-height: 1.6; }
.login-card .el-button { width: 100%; }
.demo-users { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 18px; }
.demo-title { width: 100%; color: #6f8498; font-size: 13px; }
.demo-users .el-tag { cursor: pointer; }
</style>
