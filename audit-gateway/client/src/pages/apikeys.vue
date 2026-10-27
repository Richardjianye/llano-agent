<template>
  <v-card border flat rounded="lg">
    <v-card-item>
      <v-card-title class="text-subtitle-1">API 密钥</v-card-title>

      <v-card-subtitle>
        用于桌面客户端接入审计网关；密钥明文只在创建时显示一次
      </v-card-subtitle>

      <template #append>
        <v-btn
          color="primary"
          prepend-icon="mdi-key-plus"
          variant="tonal"
          @click="openCreate"
        >
          新建密钥
        </v-btn>
      </template>
    </v-card-item>

    <v-divider />

    <v-tabs v-if="canSeeOverview" v-model="view" color="primary">
      <v-tab text="按账号查看" value="account" />
      <v-tab text="全公司总览" value="overview" />
    </v-tabs>

    <ApiKeyOverview v-if="view === 'overview'" />

    <v-card-text v-if="view === 'account' && canManageOthers" class="flex items-center gap-3">
      <MemberPicker
        v-model="targetUid"
        clearable
        hide-details
        include-disabled
        label="查看账号（留空为本人）"
        style="max-width: 420px"
        @update:model-value="loadKeys"
      />
    </v-card-text>

    <v-data-table
      v-if="view === 'account'"
      density="comfortable"
      :headers="headers"
      hover
      item-value="id"
      :items="keys"
      :items-per-page="20"
      :loading="loading"
      no-data-text="该账号尚未创建 API 密钥"
    >
      <template #[`item.key_prefix`]="{ item }">
        <code>{{ item.key_prefix }}••••••••</code>
      </template>

      <template #[`item.status`]="{ item }">
        <v-chip
          :color="keyStatus(item).color"
          size="small"
          :text="keyStatus(item).text"
          variant="tonal"
        />
      </template>

      <template #[`item.expires_at`]="{ item }">
        <span :class="isExpired(item.expires_at) ? 'text-error' : 'text-medium-emphasis'">
          {{ item.expires_at ? formatTime(item.expires_at) : '永不过期' }}
        </span>
      </template>

      <template #[`item.quota`]="{ item }">
        <div v-if="item.total_token_limit" class="text-caption">
          <div>{{ formatNumber(item.used_tokens) }} / {{ formatNumber(item.total_token_limit) }}</div>

          <v-progress-linear
            class="mt-1"
            color="primary"
            height="4"
            :model-value="quotaPercent(item)"
            rounded
          />
        </div>

        <span v-else class="text-caption text-medium-emphasis">不单独限制</span>
      </template>

      <template #[`item.last_used_at`]="{ item }">
        <span class="text-caption text-medium-emphasis">
          {{ item.last_used_at ? formatTime(item.last_used_at) : '从未使用' }}
        </span>

        <div v-if="idleHint(item, new Date())" class="text-caption text-medium-emphasis">
          {{ idleHint(item, new Date()) }}
        </div>
      </template>

      <template #[`item.created_at`]="{ item }">
        <span class="text-caption text-medium-emphasis">
          {{ item.created_at ? formatTime(item.created_at) : '—' }}
        </span>
      </template>

      <template #[`item.actions`]="{ item }">
        <v-btn
          v-if="item.status === 1"
          color="warning"
          size="small"
          text="吊销"
          variant="text"
          @click="confirmRevoke(item)"
        />

        <span v-else class="text-caption text-medium-emphasis">不可恢复</span>
      </template>
    </v-data-table>
  </v-card>

  <v-dialog v-model="createDialog" max-width="520" persistent>
    <v-card title="新建 API 密钥">
      <v-card-text>
        <v-form ref="createFormRef" @submit.prevent="submitCreate">
          <v-text-field
            v-model="createForm.name"
            autofocus
            density="comfortable"
            hint="例如「办公室 Mac」或「Hermes 桌面端」"
            label="用途备注"
            maxlength="64"
            persistent-hint
            :rules="[required]"
            variant="outlined"
          />

          <v-select
            v-model="createForm.expiry_mode"
            class="mt-4"
            density="comfortable"
            item-title="label"
            item-value="value"
            :items="expiryOptions"
            label="有效期"
            variant="outlined"
          />

          <v-text-field
            v-if="createForm.expiry_mode === 'custom'"
            v-model="createForm.expires_at"
            class="mt-2"
            density="comfortable"
            label="指定到期时间"
            :min="minimumExpiry"
            :rules="[futureTime]"
            type="datetime-local"
            variant="outlined"
          />

          <v-text-field
            v-model.number="createForm.total_token_limit"
            class="mt-2"
            clearable
            density="comfortable"
            hint="该 Key 在所有模型上的累计 Token 上限；留空表示不单独限制"
            label="独立用量额度"
            min="1"
            persistent-hint
            suffix="Token"
            type="number"
            variant="outlined"
          />

          <v-alert
            class="mt-2"
            density="compact"
            text="密钥额度与账号额度同时生效：任一额度耗尽都会停止调用。临时设备建议设置有效期。"
            type="info"
            variant="tonal"
          />
        </v-form>
      </v-card-text>

      <v-card-actions>
        <v-spacer />
        <v-btn text="取消" @click="createDialog = false" />

        <v-btn
          color="primary"
          :loading="submitting"
          text="创建"
          variant="tonal"
          @click="submitCreate"
        />
      </v-card-actions>
    </v-card>
  </v-dialog>

  <v-dialog v-model="secretDialog" max-width="620" persistent>
    <v-card title="立即保存 API 密钥">
      <v-card-text>
        <v-alert
          class="mb-4"
          density="compact"
          text="关闭窗口后无法再次查看；服务端只保存哈希。"
          type="warning"
          variant="tonal"
        />

        <v-textarea
          auto-grow
          density="comfortable"
          :model-value="generatedKey"
          readonly
          rows="2"
          variant="outlined"
        />
      </v-card-text>

      <v-card-actions>
        <v-btn
          prepend-icon="mdi-content-copy"
          text="复制密钥"
          variant="text"
          @click="copyGeneratedKey"
        />

        <v-spacer />

        <v-btn
          color="primary"
          text="我已保存"
          variant="tonal"
          @click="closeSecret"
        />
      </v-card-actions>
    </v-card>
  </v-dialog>

  <v-dialog v-model="revokeDialog" max-width="460">
    <v-card title="吊销 API 密钥">
      <v-card-text>
        确认吊销「{{ currentKey?.name }}」？已接入的客户端会立即无法继续调用，且操作不可恢复。
      </v-card-text>

      <v-card-actions>
        <v-spacer />
        <v-btn text="取消" @click="revokeDialog = false" />

        <v-btn
          color="warning"
          :loading="submitting"
          text="确认吊销"
          variant="tonal"
          @click="submitRevoke"
        />
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<script lang="ts" setup>
  import type { ApiKeyRecord } from '@/types/api'
  import { computed, onMounted, reactive, ref } from 'vue'
  import * as apikeyApi from '@/api/apikey'
  import ApiKeyOverview from '@/components/apikeys/ApiKeyOverview.vue'
  import MemberPicker from '@/components/MemberPicker.vue'
  import { roleHasPermission } from '@/constants/roles'
  import { idleHint, KEY_STATE_META, keyState } from '@/pages/apikey-lifecycle'
  import { useAuthStore } from '@/stores/auth'
  import { useToastStore } from '@/stores/toast'

  const auth = useAuthStore()
  const toast = useToastStore()

  const headers = [
    { title: '用途', key: 'name' },
    { title: '密钥前缀', key: 'key_prefix', width: 190 },
    { title: '状态', key: 'status', width: 100 },
    { title: '到期时间', key: 'expires_at', width: 190 },
    { title: '独立额度', key: 'quota', width: 180, sortable: false },
    { title: '最后使用', key: 'last_used_at', width: 180 },
    { title: '创建时间', key: 'created_at', width: 180 },
    { title: '操作', key: 'actions', sortable: false, align: 'end' as const, width: 100 },
  ]

  const expiryOptions = [
    { label: '1 小时', value: 'hour' },
    { label: '1 天', value: 'day' },
    { label: '1 个月', value: 'month' },
    { label: '3 个月', value: 'quarter' },
    { label: '1 年', value: 'year' },
    { label: '指定日期时间', value: 'custom' },
    { label: '永不过期', value: 'never' },
  ]

  const keys = ref<ApiKeyRecord[]>([])
  const targetUid = ref<string | null>(null)
  const view = ref<'account' | 'overview'>('account')
  const loading = ref(false)
  const submitting = ref(false)
  const createDialog = ref(false)
  const secretDialog = ref(false)
  const revokeDialog = ref(false)
  const generatedKey = ref('')
  const currentKey = ref<ApiKeyRecord | null>(null)
  const createFormRef = ref<{ validate: () => Promise<{ valid: boolean }> } | null>(null)
  const createForm = reactive({
    name: '',
    expiry_mode: 'month',
    expires_at: '',
    total_token_limit: null as number | null,
  })

  const canSeeOverview = computed(() =>
    roleHasPermission(
      auth.user?.permission,
      'apikey:admin',
      auth.user?.dept_code,
      auth.user?.permissions,
    ),
  )

  const canManageOthers = computed(() =>
    roleHasPermission(
      auth.user?.permission,
      'user:view',
      auth.user?.dept_code,
      auth.user?.permissions,
    ),
  )

  const required = (value: string) => (value && value.trim() !== '') || '必填'
  const minimumExpiry = computed(() => toLocalDateTime(new Date(Date.now() + 60_000)))
  function futureTime (value: string) {
    return (
      value && new Date(value).getTime() > Date.now()
    ) || '到期时间必须晚于当前时间'
  }

  function formatNumber (value: number): string {
    return new Intl.NumberFormat('zh-CN').format(value)
  }

  function quotaPercent (item: ApiKeyRecord): number {
    return item.total_token_limit
      ? Math.min(100, item.used_tokens / item.total_token_limit * 100)
      : 0
  }

  function keyStatus (item: ApiKeyRecord) {
    const state = keyState(item)
    if (state === 'revoked') return { color: 'grey', text: '已吊销' }
    if (state === 'expired' || isExpired(item.expires_at)) return { color: 'error', text: '已过期' }
    if (state === 'idle') return { color: KEY_STATE_META.idle.color, text: KEY_STATE_META.idle.label }
    if (item.total_token_limit && item.used_tokens >= item.total_token_limit) {
      return { color: 'warning', text: '额度已尽' }
    }
    return { color: 'success', text: '有效' }
  }

  function toLocalDateTime (date: Date): string {
    const offset = date.getTimezoneOffset() * 60_000
    return new Date(date.getTime() - offset).toISOString().slice(0, 16)
  }

  function presetExpiry (): string | undefined {
    if (createForm.expiry_mode === 'never') return undefined
    if (createForm.expiry_mode === 'custom') return createForm.expires_at || undefined

    const expires = new Date()
    if (createForm.expiry_mode === 'hour') expires.setHours(expires.getHours() + 1)
    if (createForm.expiry_mode === 'day') expires.setDate(expires.getDate() + 1)
    if (createForm.expiry_mode === 'month') expires.setMonth(expires.getMonth() + 1)
    if (createForm.expiry_mode === 'quarter') expires.setMonth(expires.getMonth() + 3)
    if (createForm.expiry_mode === 'year') expires.setFullYear(expires.getFullYear() + 1)
    return toLocalDateTime(expires)
  }

  function selectedUidParam (): string | undefined {
    return targetUid.value && targetUid.value !== auth.user?.uid ? targetUid.value : undefined
  }

  function formatTime (value: string): string {
    return new Date(value).toLocaleString('zh-CN', { hour12: false })
  }

  function isExpired (value: string | null): boolean {
    return Boolean(value && new Date(value).getTime() < Date.now())
  }

  async function loadKeys () {
    loading.value = true
    try {
      const result = await apikeyApi.listApiKeys(selectedUidParam())
      keys.value = result.items
    } catch (error) {
      toast.error(error, '加载 API 密钥失败')
    } finally {
      loading.value = false
    }
  }

  function openCreate () {
    Object.assign(createForm, {
      name: '', expiry_mode: 'month', expires_at: '', total_token_limit: null,
    })
    createDialog.value = true
  }

  async function submitCreate () {
    const validation = await createFormRef.value?.validate()
    if (!validation?.valid) {
      return
    }

    submitting.value = true
    try {
      const result = await apikeyApi.createApiKey({
        name: createForm.name.trim(),
        expires_at: presetExpiry(),
        total_token_limit: createForm.total_token_limit ?? undefined,
        target_uid: selectedUidParam(),
      })
      generatedKey.value = result.api_key
      createDialog.value = false
      secretDialog.value = true
      await loadKeys()
    } catch (error) {
      toast.error(error, '创建 API 密钥失败')
    } finally {
      submitting.value = false
    }
  }

  async function copyGeneratedKey () {
    try {
      await navigator.clipboard.writeText(generatedKey.value)
      toast.success('密钥已复制')
    } catch (error) {
      toast.error(error, '复制失败，请手动选择密钥')
    }
  }

  function closeSecret () {
    generatedKey.value = ''
    secretDialog.value = false
  }

  function confirmRevoke (item: ApiKeyRecord) {
    currentKey.value = item
    revokeDialog.value = true
  }

  async function submitRevoke () {
    if (!currentKey.value) {
      return
    }

    submitting.value = true
    try {
      await apikeyApi.revokeApiKey(currentKey.value.id)
      revokeDialog.value = false
      currentKey.value = null
      toast.success('API 密钥已吊销')
      await loadKeys()
    } catch (error) {
      toast.error(error, '吊销失败')
    } finally {
      submitting.value = false
    }
  }

  onMounted(async () => {
    targetUid.value = auth.user?.uid ?? null
    await loadKeys()
  })
</script>
