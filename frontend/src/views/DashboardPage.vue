<template>
  <div class="dashboard">
    <section class="welcome">
      <div>
        <div class="eyebrow">{{ user?.role_label || '用户' }}工作台</div>
        <h1>{{ user?.display_name || user?.username }}，欢迎回来</h1>
        <p>{{ roleDescription }}</p>
      </div>
      <el-tag size="large" effect="dark">{{ user?.role_label }}</el-tag>
    </section>

    <el-alert
      v-if="!availableModules.length"
      title="当前账号尚未分配可用功能，请联系管理员调整角色。"
      type="warning"
      :closable="false"
    />

    <section class="module-grid">
      <button
        v-for="item in availableModules"
        :key="item.path"
        class="module-card"
        type="button"
        @click="router.push(item.path)"
      >
        <span class="module-icon">{{ item.icon }}</span>
        <span class="module-body">
          <strong>{{ item.title }}</strong>
          <small>{{ item.description }}</small>
        </span>
        <span class="arrow">→</span>
      </button>
    </section>

    <el-card shadow="never" class="scope-card">
      <template #header><strong>当前身份权限边界</strong></template>
      <div class="scope-list">
        <el-tag v-for="item in permissionLabels" :key="item" type="info">{{ item }}</el-tag>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { getCurrentUser } from '../services/api'

const router = useRouter()
const user = ref(getCurrentUser())

const modules = [
  { capability: 'review.create', path: '/review', icon: '审', title: '单份材料审查', description: '上传申报书并查看属于自己的审查任务' },
  { capability: 'review.batch', path: '/batch', icon: '批', title: '批量审查', description: '一次提交多份材料并导出汇总结果' },
  { capability: 'ruleset.view', path: '/rules', icon: '规', title: '规则集中心', description: '按角色执行维护、测试、送审或发布' },
  { capability: 'users.manage', path: '/users', icon: '人', title: '用户与角色', description: '创建账号、分配角色、停用账号和重置密码' },
]

const roleDescriptions = {
  user: '你可以提交申报材料，并且只能查看自己创建的文档与审查结果。',
  rule_maintainer: '你负责维护、测试和提交规则草稿，不能发布规则。',
  rule_reviewer: '你负责查看全部审查任务，并审核、驳回或发布规则版本。',
  admin: '你负责用户账号、批量审查和平台日常管理。',
  super_admin: '你拥有平台全部功能和最高管理权限。',
}

const capabilityLabels = {
  'review.create': '提交审查',
  'review.view_own': '查看本人任务',
  'review.view_all': '查看全部任务',
  'review.batch': '批量审查',
  'ruleset.view': '查看规则集',
  'ruleset.edit': '编辑规则',
  'ruleset.test': '样例测试',
  'ruleset.submit': '提交规则审核',
  'ruleset.approve': '审核规则版本',
  'ruleset.publish': '发布/驳回规则',
  'ruleset.disable': '停用规则集',
  'ruleset.delete_draft': '删除草稿',
  'ruleset.delete': '删除未发布规则集',
  'users.manage': '管理用户',
  'system.manage': '系统管理',
}

const availableModules = computed(() => modules.filter(item => user.value?.capabilities?.includes(item.capability)))
const roleDescription = computed(() => roleDescriptions[user.value?.role] || '请从下方进入已授权功能。')
const permissionLabels = computed(() => (user.value?.capabilities || [])
  .filter(item => item !== 'dashboard.view')
  .map(item => capabilityLabels[item] || item))
</script>

<style scoped>
.dashboard { max-width: 1180px; margin: 0 auto; }
.welcome { display: flex; justify-content: space-between; align-items: center; gap: 24px; padding: 30px; border-radius: 16px; color: #fff; background: linear-gradient(135deg, #155b97, #2686bb); box-shadow: 0 12px 32px rgba(21, 91, 151, .18); }
.eyebrow { font-size: 13px; opacity: .8; letter-spacing: .08em; }
.welcome h1 { margin: 8px 0; font-size: 28px; }
.welcome p { margin: 0; opacity: .88; line-height: 1.7; }
.module-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; margin: 22px 0; }
.module-card { display: flex; align-items: center; gap: 16px; width: 100%; padding: 22px; text-align: left; border: 1px solid #dfe8f1; border-radius: 12px; background: #fff; color: #263b4d; cursor: pointer; transition: .18s ease; }
.module-card:hover { transform: translateY(-2px); border-color: #6caed3; box-shadow: 0 8px 24px rgba(40, 87, 120, .1); }
.module-icon { display: grid; place-items: center; width: 46px; height: 46px; flex: 0 0 46px; border-radius: 12px; background: #e8f3fb; color: #155b97; font-size: 20px; font-weight: 700; }
.module-body { display: grid; gap: 6px; flex: 1; }
.module-body strong { font-size: 17px; }
.module-body small { color: #71869a; line-height: 1.5; }
.arrow { color: #5599c2; font-size: 22px; }
.scope-card { border-radius: 12px; }
.scope-list { display: flex; flex-wrap: wrap; gap: 8px; }
@media (max-width: 720px) { .module-grid { grid-template-columns: 1fr; } .welcome { align-items: flex-start; flex-direction: column; } }
</style>
