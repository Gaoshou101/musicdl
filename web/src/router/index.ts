import { createRouter, createWebHistory } from 'vue-router'
import { useSessionStore } from '@/stores/session'
import AppShell from '@/layouts/AppShell.vue'
import LoginPage from '@/pages/LoginPage.vue'
import { sessionRedirect } from './guard'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'login', component: LoginPage },
    {
      path: '/dashboard', component: AppShell, meta: { requiresAuth: true },
      children: [
        { path: '', name: 'overview', component: () => import('@/pages/OverviewPage.vue'), meta: { title: '仪表板' } },
        { path: 'search', name: 'search', component: () => import('@/pages/SearchPage.vue'), meta: { title: '音乐搜索' } },
        { path: 'downloads', name: 'downloads', component: () => import('@/pages/DownloadsPage.vue'), meta: { title: '下载历史' } },
        { path: 'sources', name: 'sources', component: () => import('@/pages/SourcesPage.vue'), meta: { title: '音源管理' } },
        { path: 'bots', name: 'bots', component: () => import('@/pages/BotsPage.vue'), meta: { title: 'Bot 管理' } },
        { path: 'config', name: 'config', component: () => import('@/pages/ConfigPage.vue'), meta: { title: '服务配置' } },
        { path: 'health', name: 'health', component: () => import('@/pages/HealthPage.vue'), meta: { title: '健康监控' } },
        { path: 'logs', name: 'logs', component: () => import('@/pages/LogsPage.vue'), meta: { title: '日志查看' } },
        { path: 'settings', name: 'settings', component: () => import('@/pages/SettingsPage.vue'), meta: { title: '设置' } },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

router.beforeEach(async (to) => {
  const session = useSessionStore()
  if (!session.initialized) await session.bootstrap()
  const requiresAuth = to.matched.some((record) => record.meta.requiresAuth === true)
  return sessionRedirect({ requiresAuth, path: to.path, fullPath: to.fullPath }, session)
})

export default router
