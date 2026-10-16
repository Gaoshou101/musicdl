<template>
  <div class="app-shell">
    <div v-if="menuOpen" class="nav-backdrop" @click="menuOpen = false" />
    <aside class="sidebar" :class="{ 'sidebar-open': menuOpen }" aria-label="主导航">
      <div class="sidebar-main">
        <RouterLink to="/dashboard" class="brand-lockup" @click="menuOpen = false">
          <img src="/brand/icon.svg" alt="" class="brand-icon" />
          <span><strong>musicdl</strong><small>Telegram 音乐服务</small></span>
        </RouterLink>
        <p class="nav-heading">工作台</p>
        <nav class="nav-list" aria-label="后台导航">
          <RouterLink v-for="item in mainLinks" :key="item.to" :to="item.to" class="nav-link" :class="{ active: route.path === item.to }" @click="menuOpen = false">
            <component :is="item.icon" class="nav-glyph" aria-hidden="true" /><span>{{ item.label }}</span>
          </RouterLink>
        </nav>
        <p class="nav-heading nav-heading-secondary">管理</p>
        <nav class="nav-list" aria-label="管理导航">
          <RouterLink v-for="item in adminLinks" :key="item.to" :to="item.to" class="nav-link" :class="{ active: route.path === item.to }" @click="menuOpen = false">
            <component :is="item.icon" class="nav-glyph" aria-hidden="true" /><span>{{ item.label }}</span>
          </RouterLink>
        </nav>
      </div>
      <div class="sidebar-bottom">
        <span class="console-label">服务控制台</span>
        <button class="quiet-button" type="button" @click="signOut">退出登录</button>
        <p v-if="logoutError" role="alert" class="sidebar-error">{{ logoutError }}</p>
      </div>
    </aside>

    <div class="workspace">
      <header class="topbar">
        <button class="mobile-menu" type="button" aria-label="打开后台导航" :aria-expanded="menuOpen" @click="menuOpen = !menuOpen">☰</button>
        <div class="breadcrumb"><span>musicdl</span><span class="breadcrumb-sep">/</span><strong>{{ pageTitle }}</strong></div>
        <div class="toolbar">
          <ThemeControl />
        </div>
      </header>
      <main class="page-content"><RouterView /></main>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'
import { useSessionStore } from '@/stores/session'
import ThemeControl from '@/components/ThemeControl.vue'
import { PhHouse, PhMagnifyingGlass, PhDownloadSimple, PhPulse, PhListBullets, PhMusicNotes, PhRobot, PhSlidersHorizontal, PhGear } from '@phosphor-icons/vue'
import { errorMessage } from '@/lib/api'

const route = useRoute()
const router = useRouter()
const session = useSessionStore()
const menuOpen = ref(false)
const logoutError = ref('')
const pageTitle = computed(() => String(route.meta.title ?? '仪表板'))
const mainLinks = [
  { to: '/dashboard', label: '仪表板', icon: PhHouse },
  { to: '/dashboard/search', label: '音乐搜索', icon: PhMagnifyingGlass },
  { to: '/dashboard/downloads', label: '下载历史', icon: PhDownloadSimple },
  { to: '/dashboard/health', label: '健康监控', icon: PhPulse },
  { to: '/dashboard/logs', label: '日志查看', icon: PhListBullets },
]
const adminLinks = [
  { to: '/dashboard/sources', label: '音源管理', icon: PhMusicNotes },
  { to: '/dashboard/bots', label: 'Bot 管理', icon: PhRobot },
  { to: '/dashboard/config', label: '运行配置', icon: PhSlidersHorizontal },
  { to: '/dashboard/settings', label: '设置', icon: PhGear },
]
async function signOut() {
  logoutError.value = ''
  try { await session.signOut(); await router.replace('/') }
  catch (cause) { logoutError.value = errorMessage(cause) }
}
</script>
