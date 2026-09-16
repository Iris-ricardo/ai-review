<template>
  <div class="users-page">
    <div class="page-heading">
      <div>
        <h2>用户与角色管理</h2>
        <p>账号在这里创建和停用；角色决定登录后能看到的工作台入口与可调用接口。</p>
      </div>
      <div class="heading-actions">
        <el-button @click="loadData" :loading="loading">刷新</el-button>
        <el-button type="primary" @click="openCreate">新增用户</el-button>
      </div>
    </div>

    <el-card shadow="never" class="role-guide">
      <div v-for="role in visibleRoles" :key="role.id" class="role-item">
        <strong>{{ role.label }}</strong>
        <span>{{ roleSummary[role.id] }}</span>
      </div>
    </el-card>

    <el-card shadow="never">
      <el-table :data="users" v-loading="loading">
        <el-table-column prop="username" label="用户名" min-width="120" />
        <el-table-column prop="display_name" label="显示名称" min-width="130" />
        <el-table-column prop="role_label" label="角色" width="130">
          <template #default="{ row }"><el-tag>{{ row.role_label }}</el-tag></template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="row.status === 'active' ? 'success' : 'danger'">
              {{ row.status === 'active' ? '启用' : '已停用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="last_login_at" label="最后登录" min-width="170">
          <template #default="{ row }">{{ formatTime(row.last_login_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="290" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" :disabled="cannotManageUser(row)" @click="openEdit(row)">编辑</el-button>
            <el-button link type="warning" :disabled="cannotManageUser(row)" @click="openPassword(row)">重置密码</el-button>
            <el-button
              link
              :type="row.status === 'active' ? 'danger' : 'success'"
              :disabled="row.id === currentUser?.id || cannotManageUser(row)"
              @click="toggleStatus(row)"
            >{{ row.status === 'active' ? '停用' : '启用' }}</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="userDialog" :title="editing ? '编辑用户' : '新增用户'" width="min(520px, 94vw)">
      <el-form label-position="top">
        <el-form-item label="用户名" required>
          <el-input v-model="form.username" :disabled="editing" placeholder="3-32 位字母、数字、点、横线或下划线" />
        </el-form-item>
        <el-form-item label="显示名称" required><el-input v-model="form.display_name" /></el-form-item>
        <el-form-item label="角色" required>
          <el-select v-model="form.role" style="width: 100%">
            <el-option v-for="role in assignableRoles" :key="role.id" :label="role.label" :value="role.id" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="!editing" label="初始密码" required>
          <el-input v-model="form.password" type="password" show-password placeholder="至少 8 位" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="userDialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveUser">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="passwordDialog" title="重置密码" width="min(460px, 94vw)">
      <p>为用户 <strong>{{ selected?.username }}</strong> 设置新密码。</p>
      <el-input v-model="newPassword" type="password" show-password placeholder="至少 8 位" />
      <template #footer>
        <el-button @click="passwordDialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="savePassword">确认重置</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  createUser, getCurrentUser, listRoles, listUsers, resetUserPassword, updateUser,
} from '../services/api'

const users = ref([])
const roles = ref([])
const loading = ref(false)
const saving = ref(false)
const userDialog = ref(false)
const passwordDialog = ref(false)
const editing = ref(false)
const selected = ref(null)
const newPassword = ref('')
const currentUser = ref(getCurrentUser())
const form = reactive({ username: '', display_name: '', role: 'user', password: '' })

const roleSummary = {
  user: '提交单份审查，仅查看本人任务',
  rule_maintainer: '维护、测试并提交规则草稿',
  rule_reviewer: '查看全部任务，审核并发布规则',
  admin: '批量审查、用户管理和平台日常管理',
  super_admin: '全部权限，包括最高级账号和规则删除',
}
const visibleRoles = computed(() => roles.value)
const assignableRoles = computed(() => roles.value.filter(role =>
  currentUser.value?.role === 'super_admin' || role.id !== 'super_admin'))

onMounted(loadData)

function cannotManageUser(row) {
  return row.role === 'super_admin' && currentUser.value?.role !== 'super_admin'
}

async function loadData() {
  loading.value = true
  try { [users.value, roles.value] = await Promise.all([listUsers(), listRoles()]) }
  catch (error) { ElMessage.error(error.message) }
  finally { loading.value = false }
}

function openCreate() {
  editing.value = false
  selected.value = null
  Object.assign(form, { username: '', display_name: '', role: 'user', password: '' })
  userDialog.value = true
}

function openEdit(row) {
  editing.value = true
  selected.value = row
  Object.assign(form, { username: row.username, display_name: row.display_name, role: row.role, password: '' })
  userDialog.value = true
}

async function saveUser() {
  if (!form.username.trim() || !form.display_name.trim() || (!editing.value && form.password.length < 8)) {
    ElMessage.warning('请完整填写用户信息，密码至少 8 位')
    return
  }
  saving.value = true
  try {
    if (editing.value) await updateUser(selected.value.id, { display_name: form.display_name, role: form.role })
    else await createUser({ ...form })
    userDialog.value = false
    ElMessage.success(editing.value ? '用户信息已更新' : '用户已创建')
    await loadData()
  } catch (error) { ElMessage.error(error.message) }
  finally { saving.value = false }
}

async function toggleStatus(row) {
  const next = row.status === 'active' ? 'disabled' : 'active'
  try {
    await ElMessageBox.confirm(`${next === 'disabled' ? '停用' : '启用'}用户 ${row.username}？`, '确认操作')
    await updateUser(row.id, { status: next })
    ElMessage.success(next === 'disabled' ? '用户已停用' : '用户已启用')
    await loadData()
  } catch (error) { if (error !== 'cancel') ElMessage.error(error.message || String(error)) }
}

function openPassword(row) {
  selected.value = row
  newPassword.value = ''
  passwordDialog.value = true
}

async function savePassword() {
  if (newPassword.value.length < 8) return ElMessage.warning('密码至少需要 8 位')
  saving.value = true
  try {
    await resetUserPassword(selected.value.id, newPassword.value)
    passwordDialog.value = false
    ElMessage.success('密码已重置')
  } catch (error) { ElMessage.error(error.message) }
  finally { saving.value = false }
}

function formatTime(value) {
  return value ? new Date(value).toLocaleString() : '从未登录'
}
</script>

<style scoped>
.users-page { max-width: 1320px; margin: 0 auto; }
.page-heading { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-bottom: 16px; }
.page-heading h2 { margin: 0; color: #17324d; }
.page-heading p { margin: 6px 0 0; color: #6b7f92; }
.heading-actions { display: flex; gap: 8px; }
.role-guide { margin-bottom: 16px; }
.role-guide :deep(.el-card__body) { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 12px; }
.role-item { display: grid; gap: 5px; padding: 10px; border-left: 3px solid #4c9ac5; background: #f5f9fc; }
.role-item span { color: #718397; font-size: 12px; line-height: 1.5; }
@media (max-width: 900px) { .role-guide :deep(.el-card__body) { grid-template-columns: 1fr 1fr; } }
@media (max-width: 760px) { .page-heading { align-items: flex-start; flex-direction: column; } .role-guide :deep(.el-card__body) { grid-template-columns: 1fr; } }
</style>
