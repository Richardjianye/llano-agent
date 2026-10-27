<template>
  <!--
    应用根节点只负责三件事：v-app 上下文、路由出口、全局提示条。
    页面外壳（侧边栏 / 顶栏 / 主内容区）由 AdminLayout 提供，登录页自带 v-main，
    这里不要再套 v-app-bar / v-main / v-container，否则会出现双层顶栏和双层内边距。
  -->
  <v-app>
    <router-view />

    <!-- 全局提示条：所有页面共用一个实例 -->
    <v-snackbar
      v-model="toast.visible"
      :color="toast.color"
      location="top right"
      :timeout="3500"
    >
      {{ toast.message }}

      <template #actions>
        <v-btn icon="mdi-close" size="small" variant="text" @click="toast.visible = false" />
      </template>
    </v-snackbar>
  </v-app>
</template>

<script lang="ts" setup>
  import { useToastStore } from '@/stores/toast'

  const toast = useToastStore()
</script>
