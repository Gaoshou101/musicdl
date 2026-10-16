<template>
  <div class="page-stack">
    <header class="page-heading"><p class="eyebrow">账号与安全</p><h1>设置</h1><p class="muted">管理管理员账号凭据。</p></header>
    <div v-if="mustChange" class="notice notice-warning"><strong>需要修改默认密码</strong><p>当前部署仍在使用默认密码，其他接口在修改之前都会拒绝访问。请先在这里设置新的用户名与密码。</p></div>
    <section class="panel settings-panel"><div class="section-heading"><span class="summary-icon">⚿</span><div><h2>修改用户名与密码</h2><p class="muted">服务端要求同时提交用户名、当前密码与新密码；保存后所有旧会话都会失效。</p></div></div>
      <form class="form-stack settings-form" @submit.prevent="submit">
        <label class="field"><span>用户名</span><input v-model="username" autocomplete="username" required :disabled="loading" /></label>
        <label class="field"><span>当前密码</span><span class="password-control"><input v-model="currentPassword" :type="showCurrent ? 'text' : 'password'" autocomplete="current-password" required :disabled="loading" /><button type="button" :disabled="loading" :aria-label="showCurrent ? '隐藏当前密码' : '显示当前密码'" @click="showCurrent = !showCurrent">{{ showCurrent ? '隐藏' : '显示' }}</button></span></label>
        <label class="field"><span>新密码</span><span class="password-control"><input v-model="newPassword" :type="showNew ? 'text' : 'password'" autocomplete="new-password" minlength="8" required :disabled="loading" /><button type="button" :disabled="loading" :aria-label="showNew ? '隐藏新密码' : '显示新密码'" @click="showNew = !showNew">{{ showNew ? '隐藏' : '显示' }}</button></span><small>至少 8 个字符，且不能是默认密码 password</small></label>
        <label class="field"><span>确认新密码</span><span class="password-control"><input v-model="confirmPassword" :type="showConfirm ? 'text' : 'password'" autocomplete="new-password" required :disabled="loading" /><button type="button" :disabled="loading" :aria-label="showConfirm ? '隐藏确认密码' : '显示确认密码'" @click="showConfirm = !showConfirm">{{ showConfirm ? '隐藏' : '显示' }}</button></span></label>
        <p v-if="message" :role="message.type === 'error' ? 'alert' : 'status'" class="notice" :class="message.type === 'error' ? 'notice-error' : 'notice-success'">{{ message.text }}</p>
        <button class="primary-button" type="submit" :disabled="loading"><span v-if="loading" class="spinner" />{{ loading ? '正在保存…' : '保存修改' }}</button>
      </form>
    </section>
    <section class="panel settings-panel"><h2>关于登录状态</h2><p class="muted">会话保存在服务进程内存中，可以从侧栏退出；“记住我”由服务端延长登录有效期。服务重启后需要重新登录，修改凭据会让此前发出的所有会话立即失效。</p></section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { USERNAME_STORAGE_KEY, changeCredentials, errorMessage } from '@/lib/api'
import { useSessionStore } from '@/stores/session'
const route = useRoute(); const router = useRouter(); const session = useSessionStore()
const username = ref('admin'); const currentPassword = ref(''); const newPassword = ref(''); const confirmPassword = ref('')
const showCurrent = ref(false); const showNew = ref(false); const showConfirm = ref(false); const loading = ref(false)
const message = ref<{ type: 'success' | 'error'; text: string } | null>(null)
const mustChange = computed(() => route.query.must_change === '1' || session.mustChange)
onMounted(() => { try { username.value = window.localStorage.getItem(USERNAME_STORAGE_KEY) || 'admin' } catch { /* Storage is optional. */ } })
async function submit() {
  if (loading.value) return
  message.value = null
  if (!username.value.trim()) { message.value = { type: 'error', text: '用户名不能为空' }; return }
  if (newPassword.value !== confirmPassword.value) { message.value = { type: 'error', text: '新密码两次输入不一致' }; return }
  if (newPassword.value.length < 8) { message.value = { type: 'error', text: '新密码至少需要 8 个字符' }; return }
  loading.value = true
  try {
    await changeCredentials({ username: username.value.trim(), password: currentPassword.value, newPassword: newPassword.value })
    try { window.localStorage.setItem(USERNAME_STORAGE_KEY, username.value.trim()) } catch { /* Do not fail a successful update over storage. */ }
    session.credentialsChanged(); message.value = { type: 'success', text: '凭据已更新，当前浏览器已换用新的登录会话' }
    currentPassword.value = ''; newPassword.value = ''; confirmPassword.value = ''
    await router.replace('/dashboard/settings')
  } catch (cause) { message.value = { type: 'error', text: errorMessage(cause) } }
  finally { loading.value = false }
}
</script>
