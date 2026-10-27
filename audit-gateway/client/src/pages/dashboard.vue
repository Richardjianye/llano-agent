<template>
  <v-row v-if="canViewUsage" class="mb-1" dense>
    <v-col cols="12" lg="3" sm="6">
      <DashboardMetricCard
        hint="按真实模型请求计算，不含客户端心跳"
        icon="mdi-account-clock-outline"
        title="今日活跃人员"
        :value="usageLoading ? '—' : (todayUsage?.active_users ?? '—')"
      />
    </v-col>

    <v-col cols="12" lg="3" sm="6">
      <DashboardMetricCard
        compact
        hint="今天 00:00 至当前"
        icon="mdi-counter"
        title="今日 Token"
        :value="usageLoading ? '—' : (todayUsage?.total.total_tokens ?? '—')"
      />
    </v-col>

    <v-col cols="12" lg="3" sm="6">
      <DashboardMetricCard
        compact
        hint="含今天的最近 7 个自然日"
        icon="mdi-chart-bar"
        title="近 7 天 Token"
        :value="usageLoading ? '—' : (weekUsage?.total.total_tokens ?? '—')"
      />
    </v-col>

    <v-col cols="12" lg="3" sm="6">
      <DashboardMetricCard
        hint="仅显示范围内总量，不提供人员榜单"
        icon="mdi-message-processing-outline"
        title="近 7 天请求"
        :value="usageLoading ? '—' : (weekUsage?.total.requests.toLocaleString('zh-CN') ?? '—')"
      />
    </v-col>
  </v-row>

  <v-alert
    v-if="canViewUsage && usageUnavailable"
    class="mb-4"
    density="compact"
    text="用量指标暂时不可用，其他主页功能不受影响。"
    type="warning"
    variant="tonal"
  />

  <v-row>
    <v-col cols="12" md="7">
      <v-card border flat rounded="lg">
        <v-card-item>
          <template #prepend>
            <v-avatar color="primary" size="48">
              <span class="text-h6">{{ initial }}</span>
            </v-avatar>
          </template>

          <v-card-title>{{ auth.displayName }}</v-card-title>

          <v-card-subtitle>
            {{ auth.user?.username }} · {{ auth.user?.dept_name ?? '未分配部门' }}
          </v-card-subtitle>

          <template #append>
            <v-chip :color="roleColor(auth.user?.permission)" label>
              {{ auth.roleText }}
            </v-chip>
          </template>
        </v-card-item>

        <v-divider />

        <v-card-text>
          <div class="mb-2 text-caption text-medium-emphasis">当前角色可访问的功能</div>

          <div class="flex flex-wrap gap-2">
            <v-chip
              v-for="item in menus"
              :key="item.name"
              :prepend-icon="item.icon"
              size="small"
              variant="tonal"
            >
              {{ item.title }}
            </v-chip>
          </div>
        </v-card-text>
      </v-card>
    </v-col>

    <v-col cols="12" md="5">
      <v-card border flat rounded="lg">
        <v-card-item>
          <v-card-title class="text-subtitle-1">网关状态</v-card-title>

          <template #append>
            <v-btn
              icon="mdi-refresh"
              :loading="loading"
              size="small"
              variant="text"
              @click="refresh"
            />
          </template>
        </v-card-item>

        <v-divider />

        <v-list density="comfortable">
          <v-list-item title="服务">
            <template #append>
              <v-chip
                :color="health ? 'success' : 'error'"
                size="small"
                :text="health ? '运行中' : '无响应'"
                variant="tonal"
              />
            </template>
          </v-list-item>

          <v-list-item v-if="health" title="已运行">
            <template #append>
              <span class="text-body-2 text-medium-emphasis">{{ uptimeText }}</span>
            </template>
          </v-list-item>

          <v-list-item
            v-for="(state, name) in readiness?.checks ?? {}"
            :key="name"
            :title="name.toUpperCase()"
          >
            <template #append>
              <v-chip
                :color="state === 'ok' ? 'success' : 'error'"
                size="small"
                :text="state === 'ok' ? '正常' : state"
                variant="tonal"
              />
            </template>
          </v-list-item>
        </v-list>

        <v-card-text v-if="!health" class="text-body-2 text-medium-emphasis">
          连不上网关。确认 <code>audit-gateway/server</code> 已启动，
          且依赖已通过 <code>docker compose up -d</code> 起好。
        </v-card-text>
      </v-card>
    </v-col>
  </v-row>
</template>

<script lang="ts" setup>
  import type { UsageSummary } from '@/types/api'
  import { computed, onMounted, ref } from 'vue'
  import { fetchHealth, fetchReadiness, type HealthInfo, type ReadinessInfo } from '@/api/system'
  import { fetchUsageSummary } from '@/api/usage'
  import DashboardMetricCard from '@/components/DashboardMetricCard.vue'
  import { useMenus } from '@/composables/useMenus'
  import { usePermission } from '@/composables/usePermission'
  import { roleColor } from '@/constants/roles'
  import { useAuthStore } from '@/stores/auth'

  const auth = useAuthStore()
  const { menus } = useMenus()
  const { can } = usePermission()

  const health = ref<HealthInfo | null>(null)
  const readiness = ref<ReadinessInfo | null>(null)
  const loading = ref(false)
  const usageLoading = ref(false)
  const usageUnavailable = ref(false)
  const todayUsage = ref<UsageSummary | null>(null)
  const weekUsage = ref<UsageSummary | null>(null)
  const canViewUsage = computed(() => can('usage:view'))

  const initial = computed(() => auth.displayName.slice(0, 1).toUpperCase())

  const uptimeText = computed(() => {
    const seconds = health.value?.uptime_seconds ?? 0
    if (seconds < 60) return `${seconds} 秒`
    if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟`
    return `${Math.floor(seconds / 3600)} 小时 ${Math.floor((seconds % 3600) / 60)} 分钟`
  })

  function localDate (value: Date): string {
    const year = value.getFullYear()
    const month = String(value.getMonth() + 1).padStart(2, '0')
    const day = String(value.getDate()).padStart(2, '0')
    return `${year}-${month}-${day}`
  }

  async function refreshUsage () {
    if (!canViewUsage.value) return
    usageLoading.value = true
    usageUnavailable.value = false
    const today = new Date()
    const sevenDaysAgo = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 6)
    try {
      const [todayResult, weekResult] = await Promise.all([
        fetchUsageSummary({ start: localDate(today), end: localDate(today), granularity: 'day' }),
        fetchUsageSummary({ start: localDate(sevenDaysAgo), end: localDate(today), granularity: 'day' }),
      ])
      todayUsage.value = todayResult
      weekUsage.value = weekResult
    } catch {
      usageUnavailable.value = true
    } finally {
      usageLoading.value = false
    }
  }

  async function refresh () {
    loading.value = true
    const [healthResult, readinessResult] = await Promise.allSettled([
      fetchHealth(),
      fetchReadiness(),
      refreshUsage(),
    ])
    health.value = healthResult.status === 'fulfilled' ? healthResult.value : null
    readiness.value = readinessResult.status === 'fulfilled' ? readinessResult.value : null
    loading.value = false
  }

  onMounted(refresh)
</script>
