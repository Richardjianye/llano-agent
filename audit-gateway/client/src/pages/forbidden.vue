<template>
  <v-card border flat rounded="lg">
    <v-empty-state
      headline="无权访问"
      icon="mdi-lock-outline"
      :text="`当前角色（${auth.roleText}）没有该页面的权限，如需开通请联系系统管理员。`"
    >
      <template #actions>
        <v-btn color="primary" :to="{ name: 'dashboard' }" variant="tonal">返回概览</v-btn>
      </template>
    </v-empty-state>
  </v-card>
</template>

<script lang="ts" setup>
  import { useAuthStore } from '@/stores/auth'

  const auth = useAuthStore()
</script>
