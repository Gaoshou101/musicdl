<template>
  <main class="login-page">
    <div class="login-orbit login-orbit-one" aria-hidden="true" /><div class="login-orbit login-orbit-two" aria-hidden="true" />
    <section class="login-intro" aria-label="musicdl 简介">
      <div class="brand-lockup login-brand"><img src="/brand/icon.svg" alt="" class="brand-icon" /><span><strong>musicdl</strong><small>Telegram 音乐服务</small></span></div>
      <p class="eyebrow">音乐服务控制台</p>
      <h1>把音乐服务，<span>交给清晰的控制面板。</span></h1>
      <p class="login-description">管理音源、Telegram Bot 与后台运行状态。从一个安静、可靠的工作台开始。</p>
      <div class="intro-facts"><div><span>音源与 Bot</span><strong>集中管理</strong></div><div><span>运行依赖</span><strong>实时可见</strong></div></div>
    </section>
    <section class="login-card" aria-label="管理员登录">
      <div class="login-card-toolbar"><div class="brand-lockup mobile-login-brand"><img src="/brand/icon.svg" alt="" class="brand-icon" /><span><strong>musicdl</strong><small>管理后台</small></span></div><ThemeControl /></div>
      <div class="login-card-body">
        <p class="eyebrow">管理员入口</p>
        <h2>欢迎回来</h2>
        <p class="muted">登录以查看服务状态与管理设置。</p>
        <form class="form-stack" @submit.prevent="submit">
          <label class="field"><span>用户名</span><input v-model="username" name="username" autocomplete="username" placeholder="输入用户名" required :disabled="loading" /></label>
          <label class="field"><span>密码</span><input v-model="password" name="password" type="password" autocomplete="current-password" placeholder="输入密码" required :disabled="loading" /></label>
          <label class="check-line"><input v-model="remember" type="checkbox" :disabled="loading" /><span>记住我，保持登录 30 天</span></label>
          <p v-if="error" role="alert" class="notice notice-error">{{ error }}</p>
          <button class="primary-button full-button" type="submit" :disabled="loading"><span v-if="loading" class="spinner" />{{ loading ? '正在登录…' : '登录' }}</button>
        </form>
        <p class="login-footnote">使用部署时设置的管理员账号登录；若仍在使用默认密码，登录后会先要求修改。未勾选时登录有效期为 12 小时。</p>
      </div>
    </section>
  </main>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { USERNAME_STORAGE_KEY, errorMessage } from '@/lib/api'
import ThemeControl from '@/components/ThemeControl.vue'
import { useSessionStore } from '@/stores/session'

const router = useRouter()
const route = useRoute()
const session = useSessionStore()
const username = ref('admin')
const password = ref('')
const remember = ref(false)
const loading = ref(false)
const error = ref(session.error)
onMounted(() => {
  try { username.value = window.localStorage.getItem(USERNAME_STORAGE_KEY) || 'admin' } catch { /* Storage is optional. */ }
})
async function submit() {
  if (loading.value) return
  loading.value = true
  error.value = ''
  const trimmed = username.value.trim()
  try {
    const result = await session.signIn(trimmed, password.value, remember.value)
    password.value = ''
    try { window.localStorage.setItem(USERNAME_STORAGE_KEY, trimmed) } catch { /* Do not fail a successful login over storage. */ }
    if (result.must_change) await router.replace({ path: '/dashboard/settings', query: { must_change: '1' } })
    else {
      const next = typeof route.query.next === 'string' && route.query.next.startsWith('/dashboard') ? route.query.next : '/dashboard'
      await router.replace(next)
    }
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}
</script>
