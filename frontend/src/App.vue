<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { Activity, FileSearch, Menu, RadioTower, ServerCog, Ship, X } from 'lucide-vue-next'
import { getHealth, getStreams, type Health, type StreamItem } from './api'

const route = useRoute()
const mobileNavOpen = ref(false)
const health = ref<Health | null>(null)
const stream = ref<StreamItem | null>(null)
let statusTimer: number | undefined
const pageTitle = computed(() => route.meta.title || '监测平台')
const navItems = [
  { to: '/', label: '实时监测', icon: Activity },
  { to: '/offline', label: '离线分析', icon: FileSearch },
  { to: '/system', label: '系统状态', icon: ServerCog },
]
const healthy = computed(() => health.value?.status === 'healthy')
async function loadStatus() {
  try {
    const [healthValue, streams] = await Promise.all([getHealth(), getStreams()])
    health.value = healthValue
    stream.value = streams[0] || null
  } catch {
    health.value = null
    stream.value = null
  }
}
onMounted(() => { loadStatus(); statusTimer = window.setInterval(loadStatus, 15000) })
onBeforeUnmount(() => clearInterval(statusTimer))
</script>

<template>
  <a class="skip-link" href="#main-content">跳到主要内容</a>
  <div class="app-shell">
    <aside class="sidebar" :class="{ 'is-open': mobileNavOpen }" aria-label="主导航">
      <div class="brand-lockup">
        <div class="brand-mark" aria-hidden="true"><Ship :size="22" /></div>
        <div><strong>VesselSight</strong><span>船舶智能监测平台</span></div>
        <button class="icon-button sidebar-close" aria-label="关闭导航" @click="mobileNavOpen = false"><X :size="20" /></button>
      </div>
      <nav class="primary-nav">
        <RouterLink v-for="item in navItems" :key="item.to" :to="item.to" @click="mobileNavOpen = false">
          <component :is="item.icon" :size="18" />
          <span>{{ item.label }}</span>
        </RouterLink>
      </nav>
      <div class="sidebar-status">
        <div class="status-row"><span class="status-dot" :class="healthy ? 'online' : 'offline'"></span><span>API / 模型</span><strong>{{ health ? (healthy ? '就绪' : '降级') : '离线' }}</strong></div>
        <div class="status-row"><RadioTower :size="15" /><span>{{ stream?.id || '实时流' }}</span><strong>{{ stream?.status === 'configured' ? '已配置' : '未配置' }}</strong></div>
      </div>
      <div class="sidebar-footer"><span>推理设备</span><strong>{{ health?.device?.toUpperCase() || '--' }}</strong><small>VesselSight v0.2.0</small></div>
    </aside>
    <div v-if="mobileNavOpen" class="nav-scrim" @click="mobileNavOpen = false"></div>

    <section class="workspace">
      <header class="topbar">
        <button class="icon-button mobile-menu" aria-label="打开导航" @click="mobileNavOpen = true"><Menu :size="21" /></button>
        <div><span class="topbar-kicker">平陆运河 · 智能监测中心</span><h1>{{ pageTitle }}</h1></div>
        <div class="topbar-meta">
          <span class="system-state"><span class="status-dot" :class="healthy ? 'online' : 'offline'"></span>{{ health ? (healthy ? '模型就绪' : '模型配置异常') : '服务未连接' }}</span>
          <time>{{ new Date().toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' }) }}</time>
        </div>
      </header>
      <main id="main-content" tabindex="-1"><RouterView /></main>
    </section>
  </div>
</template>
