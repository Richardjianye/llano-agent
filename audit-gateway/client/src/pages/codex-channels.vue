<template>
  <div class="channel-page">
    <div class="flex flex-wrap items-center gap-3 mb-4">
      <div class="grow"><h1 class="text-h6">Codex 渠道</h1><div>管理账号授权、额度与模型；复用现有白名单和配额</div></div>
      <v-btn :disabled="loading" text="刷新状态" @click="load()" />
      <v-btn v-if="can('model:edit')" color="primary" text="新增账号授权" @click="openAuth(null)" />
    </div>

    <v-alert
      v-if="error"
      class="mb-4"
      :text="error"
      type="error"
      variant="tonal"
    />

    <v-progress-linear v-if="loading" indeterminate />

    <v-card border class="mb-4" flat>
      <v-card-text>
        <v-select
          v-model="selectedId"
          item-title="name"
          item-value="id"
          :items="channels"
          label="选择 Codex 账号渠道"
          no-data-text="尚未配置 Codex 渠道"
        />

        <template v-if="selected">
          <div class="flex flex-wrap gap-2 mb-4"><v-chip>{{ selected.status === 1 ? '已启用' : '已停用' }}</v-chip><v-chip :color="selected.credential_ok ? 'success' : 'warning'">{{ selected.credential_ok ? '凭据已配置' : '凭据不可用' }}</v-chip></div>
          <v-row><v-col cols="12" md="6">账号：{{ selected.email || '未提供' }} · {{ selected.plan_type || '未知套餐' }}</v-col><v-col cols="12" md="6">访问凭据有效期：{{ date(selected.access_expires_at) }}</v-col></v-row>
          <div class="my-3">本实例健康快照：{{ healthText(selected.health?.state) }} · {{ date(selected.health?.checked_at) }}</div>

          <div class="mt-4">
            <div class="flex flex-wrap items-center gap-2 mb-2">
              <span class="text-subtitle-2">账号剩余额度</span>
              <span class="text-caption text-medium-emphasis">{{ usageCheckedAt ? `查询时间：${usageCheckedAt}` : '尚未查询' }}</span>

              <v-btn
                :loading="usageLoading"
                size="small"
                text="刷新额度"
                variant="text"
                @click="getUsage()"
              />
            </div>

            <v-alert
              v-if="usageError"
              class="mb-3"
              :text="usageError"
              type="error"
              variant="tonal"
            />

            <div v-if="!usage" class="text-body-2 text-medium-emphasis">{{ usageLoading ? '正在查询额度…' : '额度未知，不表示无限额度。' }}</div>

            <template v-else>
              <div class="text-caption mb-3">{{ usage.allowed === false || usage.limit_reached === true ? '供应商报告额度受限' : '供应商未报告限额阻断' }}</div>

              <v-row><v-col v-for="window in windows" :key="window.title" cols="12" md="6">
                <div class="text-subtitle-2">{{ window.title }}</div>

                <template v-if="window.value?.used_percent != null">
                  <div class="text-body-2">剩余 {{ remaining(window.value.used_percent) }}% · 窗口 {{ duration(window.value.window_seconds) }} · 重置 {{ date(window.value.reset_at) }}</div>

                  <v-progress-linear
                    :aria-label="`${window.title}剩余额度`"
                    color="primary"
                    height="8"
                    :model-value="Number(remaining(window.value.used_percent))"
                    rounded
                  />
                </template>

                <div v-else class="text-body-2 text-medium-emphasis">无窗口数据</div>
              </v-col></v-row>

              <div v-if="usage.reset_credits" class="text-caption mt-3">重置券：{{ usage.reset_credits.available_count ?? '未知' }} · 当前可用 {{ usage.reset_credits.applicable_available_count ?? '未知' }}</div>
            </template>
          </div>

          <v-alert
            v-if="!selected.credential_ok"
            class="mb-3"
            text="凭据不可用，请重新授权恢复。状态不代表已通过真实推理测试。"
            type="warning"
            variant="tonal"
          />

          <div class="flex flex-wrap gap-2">
            <v-btn v-if="can('model:edit')" text="重新授权" @click="openAuth(selected)" />
            <v-btn v-if="can('model:edit')" text="编辑设置" @click="editing = selected; editorOpen = true" />
            <v-btn v-if="can('model:edit')" :disabled="loading" :text="selected.status === 1 ? '停用渠道' : '启用渠道'" @click="toggle()" />

            <v-btn
              v-if="can('model:edit')"
              :disabled="selected.status !== 1"
              :loading="probing"
              text="探测模型"
              @click="probe()"
            />

          </div>

          <div class="text-caption mt-3">{{ checkedAt ? `模型目录最近探测：${checkedAt}` : '模型目录尚未探测' }}。目录探测不等于真实聊天成功。</div>
        </template>

        <v-empty-state v-else text="先新增账号并由管理员手动完成浏览器授权；账号不会自动向员工开放。" title="尚未配置账号" />
      </v-card-text>
    </v-card>

    <v-card
      v-if="selectedId"
      border
      class="mb-4"
      flat
      title="账号模型"
    ><v-card-text><ChannelModels
      :key="selectedId"
      :discovered="discovered"
      :models="channelModels"
      :provider-id="selectedId"
      @changed="load()"
    /></v-card-text></v-card>

    <ProviderEditor
      codex
      :open="editorOpen"
      :provider="editing"
      @close="editorOpen = false"
      @saved="saved"
    />

    <v-dialog v-model="authOpen" max-width="640" persistent>
      <v-card :title="authTarget ? `重新授权：${authTarget.name}` : '新增 Codex 账号'">
        <v-card-text>
          <v-alert
            v-if="authError"
            class="mb-4"
            :text="authError"
            type="error"
            variant="tonal"
          />

          <v-alert
            v-if="authTarget"
            class="mb-4"
            text="新凭据会替换此渠道原凭据；已有模型、授权和配额保留。请确认要接入的账号。"
            type="warning"
            variant="tonal"
          />

          <v-text-field v-model="authForm.provider_code" :disabled="!!authTarget || authorizing" label="渠道编码" maxlength="64" />
          <v-text-field v-model="authForm.name" :disabled="authorizing" label="渠道中文名称" maxlength="64" />
          <v-tabs v-model="authMode"><v-tab value="oauth">浏览器授权</v-tab><v-tab value="import">高级：导入凭据</v-tab></v-tabs>

          <div v-if="authMode === 'oauth'" class="mt-4">
            <v-btn :disabled="authorizing" :loading="starting" :text="authSession ? '重新发起授权' : '发起授权'" @click="start()" />

            <template v-if="authSession">
              <v-btn
                class="ml-2"
                :href="authSession.auth_url"
                rel="noopener noreferrer"
                target="_blank"
                text="打开账号授权页面"
              />

              <v-alert class="my-4" text="请在浏览器手动登录并完成授权。之后可能跳到无法打开的 localhost 页面，请复制地址栏完整回调 URL 粘到下方；无需向网关提供账号密码。" type="info" variant="tonal" />
              <div class="text-caption mb-3">会话有效 30 分钟，过期可重新发起。{{ authStartedAt }}</div>

              <v-textarea
                v-model="authForm.code"
                autocomplete="off"
                :disabled="authorizing"
                label="完整回调 URL（包含 code 和 state）"
                rows="3"
              />

              <v-btn
                color="primary"
                :disabled="!authForm.code.trim()"
                :loading="authorizing"
                text="完成授权并保存"
                @click="authorize()"
              />
            </template>
          </div>

          <div v-else class="mt-4">
            <v-alert class="mb-3" text="仅供已有 refresh token 的管理员使用。凭据只进不出，提交后清空。" type="info" variant="tonal" />

            <v-text-field
              v-model="authForm.refresh_token"
              autocomplete="new-password"
              :disabled="authorizing"
              label="Refresh Token"
              type="password"
            />

            <v-btn
              color="primary"
              :disabled="!authForm.refresh_token.trim()"
              :loading="authorizing"
              text="验证并导入"
              @click="authorize()"
            />
          </div>
        </v-card-text>

        <v-card-actions><v-spacer /><v-btn :disabled="starting || authorizing" text="关闭" @click="authOpen = false" /></v-card-actions>
      </v-card>
    </v-dialog>
  </div>
</template>
<script setup lang="ts">
  import type { CodexChannel, CodexUsage, DiscoveredModel } from '@/api/channels'
  import type { ModelAdminInfo, ProviderInfo } from '@/types/api'
  import { computed, onMounted, reactive, ref, watch } from 'vue'
  import { useRoute } from 'vue-router'
  import { listModels, updateProvider } from '@/api/catalog'
  import { codexUsage, exchangeCodexLogin, importCodexToken, listCodexChannels, probeCodex, startCodexLogin } from '@/api/channels'
  import ChannelModels from '@/components/channels/ChannelModels.vue'
  import ProviderEditor from '@/components/channels/ProviderEditor.vue'
  import { usePermission } from '@/composables/usePermission'
  const route = useRoute()
  const { can } = usePermission()
  const channels = ref<CodexChannel[]>([])
  const models = ref<ModelAdminInfo[]>([])
  const selectedId = ref<number | null>(null)
  const selected = computed(() => channels.value.find(p => p.id === selectedId.value))
  const channelModels = computed(() => models.value.filter(m => m.provider_id === selectedId.value))
  const discovered = ref<DiscoveredModel[]>([])
  const loading = ref(false)
  const probing = ref(false)
  const checkedAt = ref('')
  const error = ref('')
  const usage = ref<CodexUsage | null>(null)
  const usageLoading = ref(false)
  const usageError = ref('')
  const usageCheckedAt = ref('')
  const windows = computed(() => [{ title: '主窗口', value: usage.value?.primary_window }, { title: '副窗口', value: usage.value?.secondary_window }])
  const editorOpen = ref(false)
  const editing = ref<ProviderInfo | null>(null)
  const authOpen = ref(false)
  const authTarget = ref<CodexChannel | null>(null)
  const authMode = ref('oauth')
  const authSession = ref<{ session_id: string, auth_url: string } | null>(null)
  const authStartedAt = ref('')
  const authError = ref('')
  const starting = ref(false)
  const authorizing = ref(false)
  const authForm = reactive({ provider_code: '', name: '', code: '', refresh_token: '' })
  let epoch = 0
  let loadEpoch = 0
  watch(selectedId, () => {
    epoch++
    discovered.value = []
    checkedAt.value = ''
    usage.value = null
    usageError.value = ''
    usageCheckedAt.value = ''
    probing.value = false
    usageLoading.value = false
    if (selectedId.value) void getUsage()
  }, { flush: 'sync' })
  watch(authOpen, open => {
    if (!open) {
      authForm.code = ''
      authForm.refresh_token = ''
      authSession.value = null
    }
  })
  watch(authMode, () => {
    authForm.code = ''
    authForm.refresh_token = ''
    authError.value = ''
  })
  function date (ts: number | null | undefined) {
    return ts ? new Date(ts * 1000).toLocaleString() : '未提供'
  }
  function duration (seconds: number | null | undefined) {
    return seconds ? `${(seconds / 3600).toFixed(1)} 小时` : '未提供'
  }
  function remaining (used: number) {
    return Math.max(0, Math.min(100, 100 - used)).toFixed(1)
  }
  function healthText (state?: string) {
    return ({ alive: '正常', dead: '凭据失效', limited: '额度受限', unknown: '未知' } as Record<string, string>)[state || ''] || '尚未检测'
  }
  async function load (id?: number) {
    const token = ++loadEpoch
    const previousId = selectedId.value
    loading.value = true
    error.value = ''
    try {
      const [p, m] = await Promise.all([listCodexChannels(), listModels()])
      if (token !== loadEpoch) return
      channels.value = p.items
      models.value = m.items
      if (id && channels.value.some(p => p.id === id)) selectedId.value = id
      else if (!channels.value.some(p => p.id === selectedId.value)) selectedId.value = channels.value[0]?.id ?? null
      if (selectedId.value && selectedId.value === previousId) void getUsage()
    } catch (error_) {
      if (token === loadEpoch) error.value = error_ instanceof Error ? error_.message : '加载失败'
    } finally {
      if (token === loadEpoch) loading.value = false
    }
  }
  async function saved (id: number) {
    editorOpen.value = false
    await load(id)
  }
  async function toggle () {
    if (!selected.value) return
    loading.value = true
    try {
      await updateProvider(selected.value.id, { status: selected.value.status === 1 ? 0 : 1 })
      await load()
    } catch (error_) {
      error.value = error_ instanceof Error ? error_.message : '修改失败'
    } finally {
      loading.value = false
    }
  }
  async function probe () {
    if (!selectedId.value || probing.value) return
    const id = selectedId.value
    const token = epoch
    probing.value = true
    error.value = ''
    discovered.value = []
    try {
      const p = await probeCodex(id)
      if (token === epoch) {
        discovered.value = p.models.map(id => ({ id, title: id, supported: true, protocol: 'Codex 聊天', resolution: '文本输出', max_reference_images: 0 }))
        checkedAt.value = new Date().toLocaleString()
      }
    } catch (error_) {
      if (token === epoch) error.value = error_ instanceof Error ? error_.message : '探测失败，请检查凭据或重新授权'
    } finally {
      if (token === epoch) probing.value = false
    }
  }
  async function getUsage () {
    if (!selectedId.value || usageLoading.value) return
    const id = selectedId.value
    const token = epoch
    usageLoading.value = true
    usageError.value = ''
    usage.value = null
    try {
      const response = await codexUsage(id)
      if (token === epoch) {
        usage.value = response.usage
        usageCheckedAt.value = new Date().toLocaleString()
      }
    } catch (error_) {
      if (token === epoch) usageError.value = error_ instanceof Error ? error_.message : '额度不可查询'
    } finally {
      if (token === epoch) usageLoading.value = false
    }
  }
  function openAuth (channel: CodexChannel | null) {
    authTarget.value = channel
    authError.value = ''
    authSession.value = null
    authStartedAt.value = ''
    authMode.value = 'oauth'
    Object.assign(authForm, { provider_code: channel?.code || '', name: channel?.name || '', code: '', refresh_token: '' })
    authOpen.value = true
  }
  async function start () {
    if (starting.value) return
    starting.value = true
    authError.value = ''
    authForm.code = ''
    authSession.value = null
    try {
      authSession.value = await startCodexLogin()
      authStartedAt.value = `发起于 ${new Date().toLocaleString()}`
    } catch (error_) {
      authError.value = error_ instanceof Error ? error_.message : '发起授权失败'
    } finally {
      starting.value = false
    }
  }
  async function authorize () {
    if (authorizing.value) return
    authError.value = ''
    if (!authForm.provider_code.trim() || !authForm.name.trim()) {
      authError.value = '请填写渠道编码和中文名称'
      return
    }
    authorizing.value = true
    try {
      const common = { provider_code: authForm.provider_code.trim(), name: authForm.name.trim() }
      const response = authMode.value === 'oauth' ? await exchangeCodexLogin({ ...common, session_id: authSession.value?.session_id || '', code: authForm.code.trim() }) : await importCodexToken({ ...common, refresh_token: authForm.refresh_token.trim() })
      authOpen.value = false
      discovered.value = []
      checkedAt.value = ''
      usage.value = null
      epoch++
      await load(response.provider_id)
    } catch (error_) {
      authError.value = error_ instanceof Error ? error_.message : '授权失败；会话过期请重新发起'
    } finally {
      authorizing.value = false
      authForm.code = ''
      authForm.refresh_token = ''
    }
  }
  watch(() => route.query.provider_id, () => {
    if (route.query.tab === 'codex') void load(Number(route.query.provider_id) || undefined)
  })
  watch(() => route.query.tab, value => {
    if (value === 'codex' && selectedId.value) void getUsage()
  })
  onMounted(() => load(Number(route.query.provider_id) || undefined))
</script>
<style scoped>
.channel-page { min-width: 0; }
</style>
