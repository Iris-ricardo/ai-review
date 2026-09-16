import { createRouter, createWebHashHistory } from 'vue-router'
import { ElMessage } from 'element-plus'
import { getAuthToken, getMe } from './services/api'
const UploadPage = () => import('./views/UploadPage.vue')
const ResultPage = () => import('./views/ResultPage.vue')
const RulesPage = () => import('./views/RulesPage.vue')
const BatchPage = () => import('./views/BatchPage.vue')
const LoginPage = () => import('./views/LoginPage.vue')
const UsersPage = () => import('./views/UsersPage.vue')
const DashboardPage = () => import('./views/DashboardPage.vue')

const routes = [
  { path: '/', redirect: '/dashboard' },
  { path: '/login', component: LoginPage },
  { path: '/dashboard', component: DashboardPage, meta: { capability: 'dashboard.view' } },
  { path: '/review', component: UploadPage, meta: { capability: 'review.create' } },
  { path: '/result/:taskId', component: ResultPage, props: true, meta: { capability: 'review.view_own' } },
  { path: '/rules', component: RulesPage, meta: { capability: 'ruleset.view' } },
  { path: '/users', component: UsersPage, meta: { capability: 'users.manage' } },
  { path: '/batch', component: BatchPage, meta: { capability: 'review.batch' } },
]

const router = createRouter({ history: createWebHashHistory(), routes })

router.beforeEach(async (to) => {
  if (to.path === '/login' && getAuthToken()) {
    try {
      await getMe()
      return '/dashboard'
    } catch {
      return true
    }
  }
  const required = to.meta.capability
  if (!required) return true
  if (!getAuthToken()) return { path: '/login', query: { redirect: to.fullPath } }
  let user
  try { user = await getMe() }
  catch { return { path: '/login', query: { redirect: to.fullPath } } }
  if (user.must_change_password) {
    // B7b：能力受限（强制改密）→ 一律进改密界面，避免菜单守卫死循环
    return { path: '/login', query: { mustChange: '1' } }
  }
  if (!user.capabilities?.includes(required)) {
    ElMessage.warning('当前账号没有访问该功能的权限')
    return '/dashboard'
  }
  return true
})

export default router
