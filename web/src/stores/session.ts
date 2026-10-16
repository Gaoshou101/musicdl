import { ref } from 'vue'
import { defineStore } from 'pinia'
import { bootstrapAdminSession, errorMessage, login as apiLogin, logoutAdmin, type AdminSessionReport } from '@/lib/api'

export const useSessionStore = defineStore('session', () => {
  const initialized = ref(false)
  const authenticated = ref(false)
  const mustChange = ref(false)
  const userId = ref('')
  const error = ref('')
  let bootstrapping: Promise<AdminSessionReport | null> | undefined

  async function bootstrap(force = false): Promise<AdminSessionReport | null> {
    if (bootstrapping) return bootstrapping
    if (initialized.value && !force) return authenticated.value ? { authenticated: true, must_change: mustChange.value, user_id: userId.value } : { authenticated: false }
    bootstrapping = bootstrapAdminSession().then((session) => {
      authenticated.value = session.authenticated
      mustChange.value = Boolean(session.must_change)
      userId.value = session.user_id ?? ''
      error.value = ''
      initialized.value = true
      return session
    }).catch((cause: unknown) => {
      authenticated.value = false
      mustChange.value = false
      error.value = errorMessage(cause)
      initialized.value = true
      return null
    }).finally(() => { bootstrapping = undefined })
    return bootstrapping
  }

  async function signIn(username: string, password: string, remember: boolean) {
    const result = await apiLogin(username, password, remember)
    authenticated.value = true
    mustChange.value = result.must_change
    initialized.value = true
    error.value = ''
    return result
  }

  function credentialsChanged() { mustChange.value = false; authenticated.value = true; initialized.value = true }

  async function signOut() {
    await logoutAdmin()
    authenticated.value = false
    mustChange.value = false
    initialized.value = true
    userId.value = ''
  }

  return { initialized, authenticated, mustChange, userId, error, bootstrap, signIn, credentialsChanged, signOut }
})
