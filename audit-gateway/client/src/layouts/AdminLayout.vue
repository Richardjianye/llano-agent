<template>
  <!-- ============ 侧边导航 ============ -->
  <v-navigation-drawer
    v-model="drawer"
    border="e"
    color="surface"
    :rail="rail && !mobile"
    rail-width="60"
    width="248"
  >
    <!-- 品牌区：和顶栏同高，让侧边栏与顶栏的分隔线在视觉上连成一条 -->
    <div class="flex px-4 items-center" style="height: 64px">
      <img src="@/assets/logo.png" width="36px">
      <span v-show="!isRail" class="text-h6 font-bold ms-2 whitespace-nowrap"> LLANO Agent 管理后台 </span>
    </div>

    <v-divider />

    <v-list class="p-2" density="comfortable" nav>
      <template v-for="(group, gi) in groups" :key="group.title">
        <!--
          收起（rail）时不显示分组标题：一个 56px 宽的栏里放不下「日常」两个字，
          硬放会挤掉图标。收起态本来就是靠图标认页面的，标题在那里没有信息量。
        -->
        <v-list-subheader v-if="!isRail" class="text-caption font-weight-medium">
          {{ group.title }}
        </v-list-subheader>

        <!-- 收起时用分隔线代替标题，否则三组图标会连成一片认不出边界 -->
        <v-divider v-else-if="gi > 0" class="my-2" />

        <v-list-item
          v-for="item in group.items"
          :key="item.name"
          :active="route.name === item.name"
          active-class="nav-item--active"
          color="primary"
          :prepend-icon="item.icon"
          rounded="lg"
          :title="item.title"
          :to="{ name: item.name }"
        />
      </template>
    </v-list>

    <template #append>
      <v-divider />

      <v-list class="p-2" density="comfortable" nav>
        <v-list-item
          :prepend-icon="isRail ? 'mdi-chevron-right' : 'mdi-chevron-left'"
          rounded="lg"
          :title="isRail ? undefined : '收起导航'"
          @click="rail = !rail"
        />
      </v-list>
    </template>
  </v-navigation-drawer>

  <!-- ============ 顶栏 ============ -->
  <v-app-bar border="b" color="surface" flat :height="64">
    <!-- 抽屉在桌面端常驻，开关只在移动端有意义；收起用侧边栏底部的按钮 -->
    <v-app-bar-nav-icon v-if="mobile" @click="drawer = !drawer" />

    <v-app-bar-title class="text-h6 font-weight-medium ps-2">
      {{ pageTitle }}
    </v-app-bar-title>

    <v-spacer />

    <v-tooltip location="bottom" :text="isDark ? '切换到浅色' : '切换到深色'">
      <template #activator="{ props }">
        <v-btn
          v-bind="props"
          :icon="isDark ? 'mdi-weather-night' : 'mdi-weather-sunny'"
          variant="text"
          @click="theme.toggle()"
        />
      </template>
    </v-tooltip>

    <v-menu location="bottom end" :offset="8">
      <template #activator="{ props }">
        <v-btn class="ms-1 me-2 text-none" rounded="pill" v-bind="props" variant="text">
          <v-avatar color="primary" size="32">
            <span class="text-caption font-weight-medium">{{ initial }}</span>
          </v-avatar>

          <span class="hidden d-sm-inline ms-2 text-body-2">{{ auth.displayName }}</span>
          <v-icon end icon="mdi-chevron-down" size="small" />
        </v-btn>
      </template>

      <v-list density="comfortable" min-width="248" rounded="lg">
        <v-list-item :subtitle="auth.user?.username ?? ''" :title="auth.displayName">
          <template #append>
            <v-chip :color="roleColor(auth.user?.permission)" label size="small">
              {{ auth.roleText }}
            </v-chip>
          </template>
        </v-list-item>

        <v-list-item v-if="auth.user?.dept_name" prepend-icon="mdi-sitemap-outline" :title="auth.user.dept_name" />

        <v-divider class="my-1" />

        <!--
          修改密码放在这里而不是「用户与角色」页：那一页的「重置密码」按钮
          由 item.manageable 控制，而后端对自我操作一律拒绝，所以自己那一行
          永远没有按钮 —— 包括总裁。首次登录时那句「请立即修改密码」在这之前
          是一句无处可去的话。
        -->
        <v-list-item
          prepend-icon="mdi-lock-reset"
          rounded="lg"
          title="修改密码"
          @click="passwordDialog = true"
        />

        <v-list-item
          v-if="dingtalkStatus.enabled"
          :disabled="dingtalkStatus.bound"
          prepend-icon="mdi-account-check-outline"
          rounded="lg"
          :title="dingtalkStatus.bound ? '钉钉已绑定' : '绑定钉钉账号'"
          @click="startDingtalkBind"
        />

        <v-list-item
          base-color="error"
          prepend-icon="mdi-logout"
          rounded="lg"
          title="退出登录"
          @click="handleLogout"
        />
      </v-list>
    </v-menu>
  </v-app-bar>

  <ChangePasswordDialog v-model="passwordDialog" />

  <!-- ============ 主内容区 ============ -->
  <v-main class="bg-background">
    <!--
      宽屏下不让内容一路铺到 4K 屏两侧，表格行会长到眼睛跟不上；
      1600px 之后居中留白，读起来更稳。
    -->
    <v-container class="page-container p-4 pa-md-6" fluid>
      <router-view />
    </v-container>
  </v-main>
</template>

<script lang="ts" setup>
  import { computed, onMounted, ref, watch } from 'vue'
  import { useRoute, useRouter } from 'vue-router'
  import { useDisplay, useTheme } from 'vuetify'
  import { fetchDingtalkStatus } from '@/api/auth'
  import ChangePasswordDialog from '@/components/ChangePasswordDialog.vue'
  import { useMenus } from '@/composables/useMenus'
  import { roleColor } from '@/constants/roles'
  import { useAuthStore } from '@/stores/auth'
  import { useToastStore } from '@/stores/toast'

  const auth = useAuthStore()
  const toast = useToastStore()
  const passwordDialog = ref(false)
  const dingtalkStatus = ref({ enabled: false, bound: false })
  const route = useRoute()
  const router = useRouter()
  const theme = useTheme()
  const { mobile } = useDisplay()
  const { groups } = useMenus()

  function startDingtalkBind () {
    window.location.assign('/admin/user/dingtalk/bind')
  }

  onMounted(async () => {
    try {
      dingtalkStatus.value = await fetchDingtalkStatus()
    } catch {
      dingtalkStatus.value = { enabled: false, bound: false }
    }
    if (route.query.dingtalk === 'bound') {
      dingtalkStatus.value.bound = true
      toast.success('钉钉账号绑定成功')
      void router.replace({ path: route.path })
    } else if (typeof route.query.dingtalk_error === 'string') {
      toast.error('钉钉账号绑定失败，请检查登录状态或联系管理员')
      void router.replace({ path: route.path })
    }
  })

  const drawer = ref(!mobile.value)
  const rail = ref(false)

  /** rail 只在桌面端生效，移动端抽屉是全宽浮层，收窄没有意义 */
  const isRail = computed(() => rail.value && !mobile.value)

  // 切到移动端时收起浮层抽屉，切回桌面端时重新常驻
  watch(mobile, isMobile => {
    drawer.value = !isMobile
  })

  const isDark = computed(() => theme.current.value.dark)
  const pageTitle = computed(() => route.meta.title ?? '')
  const initial = computed(() => auth.displayName.slice(0, 1).toUpperCase())

  async function handleLogout () {
    await auth.logout()
    toast.success('已退出登录')
    router.push({ name: 'login' })
  }
</script>

<style scoped>
.page-container {
  max-width: 1600px;
}

/* 选中项用 primary 的浅色底 + 加粗，比默认的一层淡淡的 overlay 好认 */
.nav-item--active {
  font-weight: 600;
}
</style>
