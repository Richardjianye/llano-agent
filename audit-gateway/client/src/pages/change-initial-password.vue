<template>
  <v-main><v-container class="flex justify-center items-center" style="min-height: 100vh">
    <v-card max-width="480" title="修改初始密码" width="100%">
      <v-card-text>
        <p class="mb-4">此账号由管理员批量创建，请先设置个人密码。</p>

        <v-form @submit.prevent="submit">
          <v-text-field
            v-model="current"
            autocomplete="current-password"
            label="初始密码"
            type="password"
            variant="outlined"
          />

          <v-text-field
            v-model="password"
            autocomplete="new-password"
            label="新密码（8–64 位）"
            type="password"
            variant="outlined"
          />

          <v-text-field
            v-model="confirmation"
            autocomplete="new-password"
            label="确认新密码"
            type="password"
            variant="outlined"
          />

          <v-alert v-if="error" class="mb-3" type="error" variant="tonal">{{ error }}</v-alert>
          <v-btn block color="primary" :loading="busy" type="submit">保存密码并重新登录</v-btn>
        </v-form>

        <v-btn
          block
          class="mt-3"
          :disabled="busy"
          text="退出登录"
          @click="logout"
        />
      </v-card-text>
    </v-card>
  </v-container></v-main>
</template>
<script setup lang="ts">
  import { ref } from 'vue'
  import { useRouter } from 'vue-router'
  import { changeOwnPassword } from '@/api/account'
  import { useAuthStore } from '@/stores/auth'
  const auth = useAuthStore()
  const router = useRouter()
  const current = ref('')
  const password = ref('')
  const confirmation = ref('')
  const error = ref('')
  const busy = ref(false)
  async function logout () {
    await auth.logout()
    await router.replace('/login')
  }
  async function submit () {
    error.value = ''
    if (password.value.length < 8 || password.value.length > 64 || password.value !== confirmation.value) {
      error.value = '新密码须为 8–64 位，并与确认密码一致'
      return
    }
    busy.value = true
    try {
      await changeOwnPassword({ current_password: current.value, new_password: password.value })
      await logout()
    } catch (error_) {
      error.value = error_ instanceof Error ? error_.message : '修改失败'
    } finally {
      busy.value = false
    }
  }
</script>
