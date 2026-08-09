import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'dashboard', component: () => import('./views/DashboardView.vue'), meta: { title: '实时监测' } },
    { path: '/offline', name: 'offline', component: () => import('./views/OfflineAnalysisView.vue'), meta: { title: '离线分析' } },
    { path: '/system', name: 'system', component: () => import('./views/SystemView.vue'), meta: { title: '系统状态' } },
  ],
  scrollBehavior: () => ({ top: 0 }),
})

router.afterEach((to) => {
  document.title = `${String(to.meta.title)} | VesselSight`
  document.querySelector<HTMLElement>('#main-content')?.focus()
})

export default router
