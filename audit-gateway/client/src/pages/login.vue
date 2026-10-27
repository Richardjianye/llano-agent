<template>
  <v-main class="login-bg">
    <v-container class="max-w-lg  flex items-center justify-center h-screen">
      <!--
            登录卡是全屏唯一的主体，用阴影浮起来比描边更合适。
            全局 VCard 默认是 border + flat（列表页的卡片风格），这里显式关掉，
            否则 .v-card--border 会把 box-shadow 清成 none。
          -->
      <v-card
        :border="false"
        class="p-2 w-5xl "
        elevation="8"
        :flat="false"
        rounded="lg"
      >
        <div class="my-8 text-center flex flex-col items-center">
          <img v-if="isDark" alt="LLANO AI" class="w-32" src="@/assets/llano-logo-dark.png">
          <img v-else alt="LLANO AI" class="w-32" src="@/assets/llano-logo.png">
          <p class="mt-2 text-body-2 text-medium-emphasis">AI 审计网关管理后台</p>
        </div>

        <v-card-text>
          <v-form ref="formRef" @submit.prevent="handleSubmit">
            <v-alert
              v-if="errorMessage || dingtalkFeedback"
              aria-live="assertive"
              class="mb-4"
              role="alert"
              :title="dingtalkFeedback?.title || '登录未完成'"
              :type="dingtalkReason === 'unbound' ? 'warning' : 'error'"
              variant="tonal"
            >
              {{ errorMessage || dingtalkFeedback?.message }}
              <v-btn
                v-if="dingtalkReason === 'unbound'"
                class="mt-3"
                text="使用账号密码登录"
                variant="outlined"
                @click="usernameInput?.focus()"
              />
            </v-alert>

            <v-btn
              v-if="dingtalkEnabled"
              block
              class="mb-4"
              color="primary"
              prepend-icon="mdi-account-check-outline"
              size="large"
              type="button"
              @click="startDingtalkLogin"
            >
              使用钉钉登录
            </v-btn>

            <p v-if="dingtalkEnabled" class="mb-4 text-center text-medium-emphasis text-body-2">或使用账号密码登录</p>

            <v-text-field
              ref="usernameInput"
              v-model="username"
              autocomplete="username"
              autofocus
              density="comfortable"
              label="用户名"
              prepend-inner-icon="mdi-account-outline"
              :rules="[required]"
              variant="outlined"
            />

            <v-text-field
              v-model="password"
              :append-inner-icon="showPassword ? 'mdi-eye-off-outline' : 'mdi-eye-outline'"
              autocomplete="current-password"
              class="mt-2"
              density="comfortable"
              label="密码"
              prepend-inner-icon="mdi-lock-outline"
              :rules="[required]"
              :type="showPassword ? 'text' : 'password'"
              variant="outlined"
              @click:append-inner="showPassword = !showPassword"
            />

            <v-btn
              block
              class="mt-5"
              color="primary"
              :loading="submitting"
              size="large"
              type="submit"
              :variant="dingtalkEnabled ? 'outlined' : 'flat'"
            >
              登录
            </v-btn>

          </v-form>
        </v-card-text>

        <p class="my-2 text-center text-gray-400 text-body-2">
          登录后的操作与会话内容会被留存审计
        </p>
      </v-card>

    </v-container>
  </v-main>
</template>

<script lang="ts" setup>
  import { computed, onMounted, ref, watch } from 'vue'
  import { useRoute, useRouter } from 'vue-router'
  import { useTheme } from 'vuetify'
  import { fetchDingtalkConfig } from '@/api/auth'
  import { ApiError } from '@/api/http'
  import { useAuthStore } from '@/stores/auth'
  import { useToastStore } from '@/stores/toast'

  const auth = useAuthStore()
  const toast = useToastStore()
  const route = useRoute()
  const router = useRouter()
  const theme = useTheme()

  const formRef = ref<{ validate: () => Promise<{ valid: boolean }> } | null>(null)
  const usernameInput = ref<{ focus: () => void } | null>(null)
  const dingtalkReason = ref(typeof route.query.dingtalk_error === 'string' ? route.query.dingtalk_error : '')
  watch(() => route.query.dingtalk_error, reason => {
    dingtalkReason.value = typeof reason === 'string' ? reason : ''
  })
  const dingtalkFeedback = computed(() => {
    if (!dingtalkReason.value) return null
    if (dingtalkReason.value === 'unbound') return { title: '钉钉账号尚未绑定', message: '此钉钉身份尚未关联工作台账号。请使用账号密码登录，然后在右上角账号菜单中选择“绑定钉钉账号”。' }
    if (dingtalkReason.value === 'disabled') return { title: '账号不可用', message: '关联的工作台账号已停用，请联系管理员。' }
    return { title: '钉钉登录未完成', message: '授权状态可能已过期或身份校验未完成，请重新发起钉钉登录，或使用账号密码登录。' }
  })
  const username = ref('')
  const password = ref('')
  const showPassword = ref(false)
  const submitting = ref(false)
  const errorMessage = ref('')
  const dingtalkEnabled = ref(false)
  const isDark = computed(() => theme.current.value.dark)

  const required = (v: string) => (v && v.trim() !== '') || '必填'

  function startDingtalkLogin () {
    const redirect = route.query.redirect
    const continuation = typeof redirect === 'string' && redirect.startsWith('/sso/authorize?') ? redirect : ''
    window.location.assign('/admin/user/dingtalk/login' + (continuation ? '?continue=' + encodeURIComponent(continuation) : ''))
  }

  onMounted(async () => {
    try {
      dingtalkEnabled.value = (await fetchDingtalkConfig()).enabled
    } catch {
      dingtalkEnabled.value = false
    }
  })

  async function handleSubmit () {
    const result = await formRef.value?.validate()
    if (!result?.valid) return

    submitting.value = true
    errorMessage.value = ''
    dingtalkReason.value = ''
    try {
      await auth.login(username.value.trim(), password.value)
      toast.success(`欢迎回来，${auth.displayName}`)

      const redirect = route.query.redirect
      if (typeof redirect === 'string' && redirect.startsWith('/sso/authorize?')) {
        window.location.assign(redirect)
        return
      }
      router.push(
        typeof redirect === 'string'
          ? redirect
          : { name: auth.user?.default_route || 'dashboard' },
      )
    } catch (error) {
      // 登录失败就地展示，不用飘一下就没的 snackbar
      errorMessage.value = error instanceof ApiError ? error.message : '登录失败，请重试'
    } finally {
      submitting.value = false
    }
  }
</script>

<style scoped>
.login-bg {
  background: linear-gradient(160deg, rgb(var(--v-theme-surface)) 0%, rgb(var(--v-theme-background)) 100%);
}
</style>
