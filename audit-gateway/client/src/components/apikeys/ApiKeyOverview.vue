<template>
  <div>
    <div class="flex flex-wrap items-center gap-3 px-4 py-3">
      <v-select
        v-model="states"
        chips
        closable-chips
        density="compact"
        hide-details
        item-title="label"
        item-value="value"
        :items="stateOptions"
        label="状态"
        multiple
        style="max-width: 360px"
        variant="outlined"
        @update:model-value="reload"
      />

      <v-text-field
        v-model="keyword"
        clearable
        density="compact"
        hide-details
        label="持有人 / Key 名称 / 前缀"
        prepend-inner-icon="mdi-magnify"
        style="max-width: 280px"
        variant="outlined"
        @update:model-value="reloadSoon"
      />

      <v-spacer />

      <v-btn
        v-if="selected.length > 0"
        color="error"
        prepend-icon="mdi-key-remove"
        :text="`吊销选中的 ${selected.length} 把`"
        variant="tonal"
        @click="revokeSelected"
      />

      <v-btn
        color="warning"
        prepend-icon="mdi-broom"
        text="清理闲置"
        variant="tonal"
        @click="previewIdleCleanup"
      />
    </div>

    <div class="px-4 pb-2 text-caption text-medium-emphasis">
      超过 {{ idleDays }} 天没有任何调用的个人 Key 会被自动吊销（后台每小时巡检一次；不会早于登录续期期限，正在用的客户端不受影响）。
      「清理闲置」是立即执行一次。
    </div>

    <v-data-table-server
      v-model="selected"
      v-model:items-per-page="pageSize"
      v-model:page="page"
      density="comfortable"
      :headers="headers"
      item-value="id"
      :items="items"
      :items-length="total"
      :loading="loading"
      no-data-text="没有符合条件的 Key"
      show-select
      @update:options="load"
    >
      <template #[`item.owner`]="{ item }">
        <div>{{ item.owner.name }}</div>
        <div class="text-caption text-medium-emphasis">{{ item.owner.username }}</div>
      </template>

      <template #[`item.name`]="{ item }">
        <div>{{ item.name }}</div>
        <code class="text-caption">{{ item.key_prefix }}••••</code>
      </template>

      <template #[`item.state`]="{ item }">
        <v-chip
          :color="KEY_STATE_META[keyState(item)].color"
          size="small"
          :text="KEY_STATE_META[keyState(item)].label"
          variant="tonal"
        />

        <div v-if="keyState(item) === 'revoked'" class="text-caption text-medium-emphasis">
          {{ revokeReasonText(item.revoke_reason) }}
        </div>

        <div v-else-if="idleHint(item, new Date())" class="text-caption text-medium-emphasis">
          {{ idleHint(item, new Date()) }}
        </div>
      </template>

      <template #[`item.last_used_at`]="{ item }">
        <span class="text-caption">{{ formatTime(item.last_used_at) ?? '从未使用' }}</span>
      </template>

      <template #[`item.created_at`]="{ item }">
        <span class="text-caption text-medium-emphasis">{{ formatTime(item.created_at) ?? '—' }}</span>
      </template>
    </v-data-table-server>

    <v-dialog v-model="cleanupDialog" max-width="460">
      <v-card title="清理闲置 Key">
        <v-card-text>
          将立即吊销 {{ cleanupCount }} 把超过 {{ idleDays }} 天未使用的 Key。
          它们在认证时已经被拒绝，这一步不会让任何还在续期的客户端掉线。
          <div v-if="cleanupDenied > 0" class="mt-2 text-medium-emphasis">
            另有 {{ cleanupDenied }} 把属于你无权管理的账号，会跳过。
          </div>
        </v-card-text>

        <v-card-actions>
          <v-spacer />
          <v-btn text="取消" @click="cleanupDialog = false" />

          <v-btn
            color="warning"
            :disabled="cleanupCount === 0"
            :loading="busy"
            text="确认清理"
            variant="tonal"
            @click="runIdleCleanup"
          />
        </v-card-actions>
      </v-card>
    </v-dialog>
  </div>
</template>

<script setup lang="ts">
  /** 全公司设备 Key 总览：按最后使用从久到近排，可勾选批量吊销、一键清理闲置 */
  import type { ApiKeyOverviewRecord, ApiKeyState } from '@/types/api'
  import { ref } from 'vue'
  import { apiKeyOverview, batchRevokeApiKeys, revokeIdleApiKeys } from '@/api/apikey'
  import { idleHint, KEY_STATE_META, keyState, revokeReasonText } from '@/pages/apikey-lifecycle'
  import { useToastStore } from '@/stores/toast'

  const toast = useToastStore()

  const items = ref<ApiKeyOverviewRecord[]>([])
  const total = ref(0)
  const idleDays = ref(30)
  const page = ref(1)
  const pageSize = ref(50)
  const loading = ref(false)
  const busy = ref(false)
  const selected = ref<number[]>([])
  const states = ref<ApiKeyState[]>(['active', 'idle'])
  const keyword = ref('')
  const cleanupDialog = ref(false)
  const cleanupCount = ref(0)
  const cleanupDenied = ref(0)

  const stateOptions = (Object.keys(KEY_STATE_META) as ApiKeyState[])
    .map(value => ({ value, label: KEY_STATE_META[value].label }))

  const headers = [
    { title: '持有人', key: 'owner', sortable: false },
    { title: '设备 / 用途', key: 'name', sortable: false },
    { title: '状态', key: 'state', sortable: false, width: 170 },
    { title: '最后使用', key: 'last_used_at', sortable: false, width: 180 },
    { title: '签发时间', key: 'created_at', sortable: false, width: 180 },
  ]

  function formatTime (value: string | null | undefined): string | null {
    return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : null
  }

  async function load () {
    loading.value = true
    try {
      const result = await apiKeyOverview({
        states: states.value,
        keyword: keyword.value ?? '',
        limit: pageSize.value,
        offset: (page.value - 1) * pageSize.value,
      })
      items.value = result.items
      total.value = result.total
      idleDays.value = result.idle_days
      selected.value = selected.value.filter(id => result.items.some(i => i.id === id))
    } catch (error) {
      toast.error(error, '加载 Key 总览失败')
    } finally {
      loading.value = false
    }
  }

  function reload () {
    page.value = 1
    void load()
  }

  let timer: ReturnType<typeof setTimeout> | undefined
  function reloadSoon () {
    clearTimeout(timer)
    timer = setTimeout(reload, 300)
  }

  async function revokeSelected () {
    busy.value = true
    try {
      const { revoked, denied_ids: denied } = await batchRevokeApiKeys(selected.value)
      toast.success(denied.length > 0 ? `已吊销 ${revoked} 把，${denied.length} 把无权操作已跳过` : `已吊销 ${revoked} 把`)
      selected.value = []
      await load()
    } catch (error) {
      toast.error(error, '批量吊销失败')
    } finally {
      busy.value = false
    }
  }

  async function previewIdleCleanup () {
    try {
      const result = await revokeIdleApiKeys(true)
      cleanupCount.value = result.would_revoke ?? 0
      cleanupDenied.value = result.denied_ids.length
      idleDays.value = result.idle_days
      cleanupDialog.value = true
    } catch (error) {
      toast.error(error, '无法预览清理')
    }
  }

  async function runIdleCleanup () {
    busy.value = true
    try {
      const result = await revokeIdleApiKeys(false)
      toast.success(`已清理 ${result.revoked ?? 0} 把闲置 Key`)
      cleanupDialog.value = false
      await load()
    } catch (error) {
      toast.error(error, '清理失败')
    } finally {
      busy.value = false
    }
  }
</script>
