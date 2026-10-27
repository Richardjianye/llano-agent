<template>
  <v-card border flat rounded="lg">
    <v-tabs v-model="tab" color="primary" @update:model-value="selectTab">
      <v-tab text="白名单" value="whitelist" />
      <v-tab text="模型" value="models" />
      <v-tab text="上游渠道" value="providers" />
      <v-tab text="生图渠道" value="images" />
      <v-tab text="Codex 渠道" value="codex" />
    </v-tabs>

    <v-divider />

    <v-tabs-window v-model="tab">
      <!-- ==================== 白名单 ==================== -->
      <v-tabs-window-item value="whitelist">
        <v-card-item>
          <v-card-title class="text-subtitle-1">模型白名单</v-card-title>

          <v-card-subtitle>
            默认拒绝：没有授权条目的人一个模型都用不了；显式拒绝优先级最高
          </v-card-subtitle>

          <template #append>
            <div class="flex gap-2">
              <v-btn
                prepend-icon="mdi-account-search-outline"
                variant="text"
                @click="previewDialog = true"
              >
                按成员查看
              </v-btn>

              <v-btn
                v-perm="'model:edit'"
                color="primary"
                prepend-icon="mdi-plus"
                variant="tonal"
                @click="openWhitelistCreate"
              >
                新增授权
              </v-btn>
            </div>
          </template>
        </v-card-item>

        <v-divider />

        <div class="flex items-center gap-3 px-4 pt-3">
          <v-btn-toggle
            v-model="whitelistView"
            color="primary"
            density="compact"
            mandatory
            variant="outlined"
          >
            <v-btn text="按模型查看" value="overview" />
            <v-btn text="全部条目" value="entries" />
          </v-btn-toggle>

          <v-spacer />

          <v-btn
            v-if="whitelistView === 'entries' && selectedEntries.length > 0"
            v-perm="'model:edit'"
            color="error"
            prepend-icon="mdi-delete-outline"
            :text="`删除选中的 ${selectedEntries.length} 条`"
            variant="tonal"
            @click="removeSelectedWhitelist"
          />
        </div>

        <WhitelistOverview
          v-if="whitelistView === 'overview'"
          ref="overviewRef"
          @grant="openWhitelistCreate"
        />

        <v-data-table
          v-else
          v-model="selectedEntries"
          density="comfortable"
          :headers="whitelistHeaders"
          hover
          item-value="id"
          :items="whitelist"
          :items-per-page="20"
          :loading="loading"
          no-data-text="还没有任何授权条目 —— 当前所有人都没有可用模型"
          show-select
        >
          <template #[`item.scope`]="{ item }">
            <v-chip
              :color="item.scope === 'global' ? 'primary' : 'default'"
              label
              size="small"
              :text="scopeText(item)"
            />
          </template>

          <template #[`item.model_id`]="{ item }">
            {{ modelName(item.model_id) }}
          </template>

          <template #[`item.allowed`]="{ item }">
            <v-chip
              :color="item.allowed ? 'success' : 'error'"
              size="small"
              :text="item.allowed ? '允许' : '显式拒绝'"
              variant="tonal"
            />
          </template>

          <template #[`item.actions`]="{ item }">
            <v-btn
              v-perm="'model:edit'"
              size="small"
              :text="item.allowed ? '改为拒绝' : '改为允许'"
              variant="text"
              @click="toggleWhitelist(item)"
            />

            <v-btn
              v-perm="'model:edit'"
              color="error"
              size="small"
              text="删除"
              variant="text"
              @click="removeWhitelist(item)"
            />
          </template>
        </v-data-table>
      </v-tabs-window-item>

      <!-- ==================== 模型 ==================== -->
      <v-tabs-window-item value="models">
        <v-card-item>
          <v-card-title class="text-subtitle-1">模型清单</v-card-title>

          <v-select
            v-model="capability"
            class="mt-3"
            density="compact"
            hide-details
            :items="[{ title: '全部能力', value: 'all' }, { title: '聊天模型', value: 'chat' }, { title: '生图模型', value: 'image' }]"
            label="模型能力"
          />

          <v-card-subtitle>登记后才能在白名单里授权</v-card-subtitle>

          <template #append>
            <div class="flex gap-2">
              <v-switch
                v-model="hideDisabledModels"
                color="primary"
                density="compact"
                hide-details
                label="隐藏已停用"
              />

              <v-btn
                v-perm="'model:edit'"
                color="primary"
                prepend-icon="mdi-plus"
                variant="tonal"
                @click="openModelCreate"
              >
                登记模型
              </v-btn>
            </div>
          </template>
        </v-card-item>

        <v-divider />

        <v-data-table
          density="comfortable"
          :headers="modelHeaders"
          hover
          item-value="id"
          :items="visibleModels"
          :items-per-page="20"
          :loading="loading"
          :no-data-text="hideDisabledModels
            ? '没有启用的模型；关闭“隐藏已停用”可查看历史模型'
            : '暂无模型，可用 scripts/bootstrap.py 从上游自动同步'"
        >
          <template #[`item.display_name`]="{ item }">
            <div class="text-base">{{ item.display_name }} <v-chip size="x-small">{{ item.capability === 'image' ? '生图' : '聊天' }}</v-chip></div>

            <div class="text-caption text-medium-emphasis text-gray-400 text-sm">
              {{ item.model_name }}{{ item.alias&&(item.alias != item.model_name) ? ` · 别名 ${item.alias}` : '' }}
            </div>
          </template>

          <template #[`item.price`]="{ item }">
            <span
              class="text-caption"
              :class="{ 'text-medium-emphasis': priceText(item) === '未配置' }"
            >
              {{ priceText(item) }}
            </span>
          </template>

          <template #[`item.status`]="{ item }">
            <v-chip
              :color="item.status === 1 ? 'success' : 'grey'"
              size="small"
              :text="item.status === 1 ? '启用' : '停用'"
              variant="tonal"
            />
          </template>

          <template #[`item.actions`]="{ item }">
            <v-btn
              v-perm="'model:edit'"
              size="small"
              text="编辑"
              variant="text"
              @click="openModelEdit(item)"
            />

            <v-btn
              v-if="item.status === 1"
              v-perm="'model:edit'"
              color="warning"
              size="small"
              text="停用"
              variant="text"
              @click="confirmDisableModel(item)"
            />

            <v-btn
              v-else
              v-perm="'model:edit'"
              color="success"
              size="small"
              text="启用"
              variant="text"
              @click="enableModel(item)"
            />
          </template>
        </v-data-table>
      </v-tabs-window-item>

      <!-- ==================== 上游渠道 ==================== -->
      <v-tabs-window-item value="providers">
        <v-card-item>
          <v-card-title class="text-subtitle-1">上游渠道</v-card-title>

          <v-card-subtitle>API Key 加密存储，只进不出</v-card-subtitle>

          <template #append>
            <v-btn
              v-perm="'model:edit'"
              color="primary"
              prepend-icon="mdi-plus"
              variant="tonal"
              @click="openProviderCreate"
            >
              新增渠道
            </v-btn>
          </template>
        </v-card-item>

        <v-divider />

        <v-data-table
          density="comfortable"
          :headers="providerHeaders"
          hover
          item-value="id"
          :items="providers"
          :loading="loading"
          no-data-text="暂无上游渠道"
        >
          <template #[`item.name`]="{ item }">
            <div class="font-weight-medium">{{ item.name }} <v-btn v-if="item.kind === 'codex'" size="small" text="账号管理" :to="{ path: '/models', query: { tab: 'codex', provider_id: item.id } }" /><v-btn v-else size="small" text="生图管理" :to="{ path: '/models', query: { tab: 'images', provider_id: item.id } }" /></div>
            <div class="text-caption text-medium-emphasis">{{ item.base_url }}</div>
          </template>

          <template #[`item.api_key_masked`]="{ item }">
            <v-chip
              :color="item.api_key_masked === '已配置' ? 'success' : 'warning'"
              size="small"
              :text="item.api_key_masked"
              variant="tonal"
            />
          </template>

          <template #[`item.status`]="{ item }">
            <v-chip
              :color="item.status === 1 ? 'success' : 'grey'"
              size="small"
              :text="item.status === 1 ? '启用' : '停用'"
              variant="tonal"
            />
          </template>

          <template #[`item.actions`]="{ item }">
            <v-btn
              v-perm="'model:edit'"
              size="small"
              text="编辑"
              variant="text"
              @click="openProviderEdit(item)"
            />
          </template>
        </v-data-table>
      </v-tabs-window-item>

      <v-tabs-window-item value="images"><v-card-text><ImageChannels /></v-card-text></v-tabs-window-item>
      <v-tabs-window-item value="codex"><v-card-text><CodexChannels /></v-card-text></v-tabs-window-item>
    </v-tabs-window>
  </v-card>

  <!-- 新增授权（可多选模型 × 部门/角色/成员） -->
  <WhitelistBatchDialog
    v-model="whitelistDialog"
    :departments="departments"
    :models="enabledModels"
    :preset-model-id="presetModelId"
    @saved="load"
  />

  <!-- 模型表单 -->
  <v-dialog v-model="modelDialog" max-width="560" persistent>
    <v-card :title="editingModel ? '编辑模型' : '登记模型'">
      <v-card-text>
        <v-select
          v-if="!editingModel"
          v-model="modelForm.provider_id"
          density="comfortable"
          item-title="name"
          item-value="id"
          :items="providers"
          label="上游渠道"
          variant="outlined"
        />

        <v-text-field
          v-if="!editingModel"
          v-model="modelForm.model_name"
          class="mt-2"
          density="comfortable"
          hint="上游真实模型名，如 deepseek-chat"
          label="模型名"
          persistent-hint
          variant="outlined"
        />

        <v-text-field
          v-model="modelForm.display_name"
          class="mt-4"
          density="comfortable"
          label="展示名"
          variant="outlined"
        />

        <v-text-field
          v-model="modelForm.alias"
          class="mt-2"
          density="comfortable"
          hint="对外别名，留空则用模型名；换上游时可保持客户端不变。不能带空格 —— 客户端按空格分词，会切不了模型；中文名请填「展示名」"
          label="别名（可空）"
          persistent-hint
          variant="outlined"
        />

        <v-text-field
          v-model.number="modelForm.max_context_tokens"
          class="mt-4"
          density="comfortable"
          label="上下文长度（可空）"
          type="number"
          variant="outlined"
        />

        <!-- ---------- 计价 ---------- -->
        <v-btn-toggle
          v-model="modelForm.billing_mode"
          class="mt-4 mb-3"
          color="primary"
          density="comfortable"
          mandatory
          variant="outlined"
        >
          <v-btn value="token">按 Token 计价</v-btn>

          <v-btn value="per_call">按次计价</v-btn>
        </v-btn-toggle>

        <template v-if="modelForm.billing_mode === 'token'">
          <div class="flex gap-3">
            <v-text-field
              v-model.number="modelForm.input_price_per_1m"
              density="comfortable"
              hint="留空表示未配置"
              label="输入单价（元 / M Token）"
              persistent-hint
              step="0.0001"
              type="number"
              variant="outlined"
            />

            <v-text-field
              v-model.number="modelForm.output_price_per_1m"
              density="comfortable"
              hint="两项都填才会算费用"
              label="输出单价（元 / M Token）"
              persistent-hint
              step="0.0001"
              type="number"
              variant="outlined"
            />
          </div>

          <v-text-field
            v-model.number="modelForm.cached_input_price_per_1m"
            class="mt-3"
            density="comfortable"
            hint="留空 = 按输入单价计，不是免费"
            label="缓存命中输入单价（元 / M Token）"
            persistent-hint
            step="0.0001"
            type="number"
            variant="outlined"
          />

          <!-- 这条警告必须在，否则会出现「配了缓存单价却一直按全价算」而无人察觉 -->
          <v-alert
            class="mt-3"
            density="compact"
            icon="mdi-database-search-outline"
            type="warning"
            variant="tonal"
          >
            缓存单价只在<b>上游确实返回缓存命中 token 数</b>时才生效。填之前请先跑
            <code>python scripts/probe_upstream_usage.py</code> 探明 ——
            上游不返回的话，配了也没有 token 数可乘，报表会一直按全价算。
          </v-alert>
        </template>

        <template v-else>
          <v-text-field
            v-model.number="modelForm.price_per_call"
            density="comfortable"
            hint="每次调用固定金额，与 token 数无关"
            label="按次单价（元 / 次）"
            persistent-hint
            step="0.0001"
            type="number"
            variant="outlined"
          />
        </template>

        <v-alert class="mt-3" density="compact" type="info" variant="tonal">
          单价只用于报表估算分摊，不是账单。留空的模型在报表里费用显示「—」，
          不会被当成免费计入总额。分段/阶梯计价暂不支持，
          但每次调用的原始 usage 已全量入库，日后要精算随时能重算。
        </v-alert>

        <div class="flex gap-6 mt-2">
          <v-switch
            v-model="modelForm.supports_stream"
            color="primary"
            hide-details
            label="支持流式"
          />

          <v-switch
            v-model="modelForm.supports_multimodal"
            color="primary"
            hide-details
            label="支持多模态"
          >
            <template #details>
              <span class="text-caption text-medium-emphasis">
                图片 / 音频 / 视频 / 文档，均以 base64 内联传递
              </span>
            </template>
          </v-switch>
        </div>
      </v-card-text>

      <v-card-actions>
        <v-spacer />
        <v-btn text="取消" @click="modelDialog = false" />

        <v-btn
          color="primary"
          :loading="submitting"
          text="保存"
          variant="tonal"
          @click="submitModel"
        />
      </v-card-actions>
    </v-card>
  </v-dialog>

  <!-- 渠道表单 -->
  <v-dialog v-model="providerDialog" max-width="560" persistent>
    <v-card :title="editingProvider ? '编辑渠道' : '新增渠道'">
      <v-card-text>
        <v-text-field
          v-if="!editingProvider"
          v-model="providerForm.code"
          density="comfortable"
          hint="稳定标识，如 newapi"
          label="渠道编码"
          persistent-hint
          variant="outlined"
        />

        <v-text-field
          v-model="providerForm.name"
          class="mt-4"
          density="comfortable"
          label="渠道名称"
          variant="outlined"
        />

        <v-text-field
          v-model="providerForm.base_url"
          class="mt-2"
          density="comfortable"
          hint="OpenAI 兼容的 base_url"
          label="上游地址"
          persistent-hint
          :readonly="editingProvider?.kind === 'codex'"
          variant="outlined"
        />

        <v-text-field
          v-if="editingProvider?.kind !== 'codex'"
          v-model="providerForm.api_key"
          class="mt-4"
          density="comfortable"
          :hint="editingProvider ? '留空表示不修改现有 Key' : '加密后入库，之后不可读出'"
          label="API Key"
          persistent-hint
          type="password"
          variant="outlined"
        />
      </v-card-text>

      <v-card-actions>
        <v-spacer />
        <v-btn text="取消" @click="providerDialog = false" />

        <v-btn
          color="primary"
          :loading="submitting"
          text="保存"
          variant="tonal"
          @click="submitProvider"
        />
      </v-card-actions>
    </v-card>
  </v-dialog>

  <!-- 按成员反查可用模型及来源 -->
  <MemberAccessDialog v-model="previewDialog" />
</template>

<script lang="ts" setup>
  import type {
    Department,
    ModelAdminInfo,
    ProviderInfo,
    WhitelistEntry,
  } from '@/types/api'
  import { computed, defineAsyncComponent, onMounted, reactive, ref, watch } from 'vue'
  import { useRoute, useRouter } from 'vue-router'
  import * as catalogApi from '@/api/catalog'
  import { listDepartments } from '@/api/department'
  import { searchMembers } from '@/api/members'
  import MemberAccessDialog from '@/components/whitelist/MemberAccessDialog.vue'
  import WhitelistBatchDialog from '@/components/whitelist/WhitelistBatchDialog.vue'
  import WhitelistOverview from '@/components/whitelist/WhitelistOverview.vue'
  import { roleLabel } from '@/constants/roles'
  import {
    DEFAULT_HIDE_DISABLED_MODELS,
    filterModelsByStatus,
  } from '@/pages/model-filter'
  import { useToastStore } from '@/stores/toast'
  import { SCOPE, SCOPE_LABELS } from '@/types/api'

  const toast = useToastStore()

  const route = useRoute()
  const router = useRouter()
  const ImageChannels = defineAsyncComponent(() => import('@/pages/image-channels.vue'))
  const CodexChannels = defineAsyncComponent(() => import('@/pages/codex-channels.vue'))
  const validTabs = new Set(['whitelist', 'models', 'providers', 'images', 'codex'])
  const tab = ref(validTabs.has(String(route.query.tab)) ? String(route.query.tab) : 'whitelist')
  const capability = ref('all')
  const loading = ref(false)
  const submitting = ref(false)
  const hideDisabledModels = ref(DEFAULT_HIDE_DISABLED_MODELS)

  const providers = ref<ProviderInfo[]>([])
  const models = ref<ModelAdminInfo[]>([])
  const whitelist = ref<WhitelistEntry[]>([])
  const departments = ref<Department[]>([])
  // 白名单里「指定用户」条目的显示名，按 uid 回查（不再预拉前 200 个账号）
  const memberNames = ref<Record<string, string>>({})
  const whitelistView = ref<'overview' | 'entries'>('overview')
  const selectedEntries = ref<number[]>([])
  const presetModelId = ref<number | null>(null)
  const overviewRef = ref<InstanceType<typeof WhitelistOverview> | null>(null)

  const whitelistHeaders = [
    { title: '作用域', key: 'scope', width: 220 },
    { title: '模型', key: 'model_id' },
    { title: '效果', key: 'allowed', width: 120 },
    { title: '备注', key: 'remark' },
    { title: '操作', key: 'actions', sortable: false, align: 'end' as const, width: 200 },
  ]

  const modelHeaders = [
    { title: '模型/别名', key: 'display_name' },
    { title: '渠道', key: 'provider_code', width: 140 },
    { title: '上下文', key: 'max_context_tokens', width: 120 },
    { title: '计价', key: 'price', sortable: false, width: 230 },
    { title: '状态', key: 'status', width: 100 },
    { title: '操作', key: 'actions', sortable: false, align: 'end' as const, width: 180 },
  ]

  /** 单价展示：未配置要明确说出来，不能显示成 0（0 会被读成免费） */
  function priceText (model: ModelAdminInfo): string {
    if (model.billing_mode === 'per_call') {
      return model.price_per_call === null
        ? '未配置'
        : `按次 ${model.price_per_call} / 次`
    }
    if (model.input_price_per_1m === null || model.output_price_per_1m === null) {
      return '未配置'
    }
    const base = `入 ${model.input_price_per_1m} / 出 ${model.output_price_per_1m}`
    // 缓存单价没单独配时不显示 —— 显示「缓存 —」会让人以为缓存不计费
    return model.cached_input_price_per_1m === null
      ? base
      : `${base} / 缓存 ${model.cached_input_price_per_1m}`
  }

  const providerHeaders = [
    { title: '渠道', key: 'name' },
    { title: '编码', key: 'code', width: 140 },
    { title: 'API Key', key: 'api_key_masked', width: 120 },
    { title: '状态', key: 'status', width: 100 },
    { title: '操作', key: 'actions', sortable: false, align: 'end' as const, width: 120 },
  ]

  const enabledModels = computed(() => models.value.filter(m => m.status === 1))
  const visibleModels = computed(() =>
    filterModelsByStatus(models.value, hideDisabledModels.value).filter(m => capability.value === 'all' || (m.capability ?? 'chat') === capability.value),
  )

  // ---------------- 对话框状态 ----------------

  const whitelistDialog = ref(false)
  const modelDialog = ref(false)
  const providerDialog = ref(false)
  const previewDialog = ref(false)

  const editingModel = ref<ModelAdminInfo | null>(null)
  const editingProvider = ref<ProviderInfo | null>(null)

  const modelForm = reactive({
    provider_id: null as number | null,
    model_name: '',
    display_name: '',
    alias: '',
    max_context_tokens: null as number | null,
    billing_mode: 'token',
    // null = 未配置。后端把「显式传 null」和「不传」区别对待，所以要能表达清空
    input_price_per_1m: null as number | null,
    output_price_per_1m: null as number | null,
    cached_input_price_per_1m: null as number | null,
    price_per_call: null as number | null,
    supports_stream: true,
    supports_multimodal: false,
  })

  /** 空字符串、NaN 一律归成 null（清空单价），有值才转数字 */
  function priceOrNull (value: number | string | null): number | null {
    if (value === null || value === '' || Number.isNaN(Number(value))) {
      return null
    }
    return Number(value)
  }

  const providerForm = reactive({
    code: '',
    name: '',
    base_url: '',
    api_key: '',
  })

  // ---------------- 展示辅助 ----------------

  function modelName (id: number): string {
    const model = models.value.find(m => m.id === id)
    return model ? model.display_name : `#${id}`
  }

  function scopeText (entry: WhitelistEntry): string {
    const label = SCOPE_LABELS[entry.scope] ?? entry.scope
    if (entry.scope === SCOPE.GLOBAL) {
      return label
    }
    if (entry.scope === SCOPE.DEPT) {
      const dept = departments.value.find(d => String(d.id) === entry.scope_value)
      return `${label}：${dept?.name ?? entry.scope_value}`
    }
    if (entry.scope === SCOPE.ROLE) {
      return `${label}：${roleLabel(entry.scope_value)}`
    }
    return `${label}：${memberNames.value[entry.scope_value] ?? entry.scope_value}`
  }

  // ---------------- 加载 ----------------

  async function load () {
    loading.value = true
    try {
      const [providerResult, modelResult, whitelistResult] = await Promise.all([
        catalogApi.listProviders(),
        catalogApi.listModels(),
        catalogApi.listWhitelist(),
      ])
      providers.value = providerResult.items
      models.value = modelResult.items
      whitelist.value = whitelistResult.items
      selectedEntries.value = selectedEntries.value.filter(id => whitelist.value.some(e => e.id === id))
      void loadMemberNames()
      void overviewRef.value?.load()
    } catch (error) {
      toast.error(error, '加载失败')
    } finally {
      loading.value = false
    }
  }

  async function loadRefs () {
    try {
      departments.value = (await listDepartments(true)).items
    } catch (error) {
      toast.error(error, '加载部门失败')
    }
  }

  /** 白名单里出现的成员按 uid 批量回查姓名 */
  async function loadMemberNames () {
    const uids = [...new Set(whitelist.value.filter(e => e.scope === SCOPE.USER).map(e => e.scope_value))]
      .filter(uid => !(uid in memberNames.value))
    // 接口单次最多 100 条一页，分批取
    for (let i = 0; i < uids.length; i += 100) {
      const batch = uids.slice(i, i + 100)
      try {
        const page = await searchMembers({ uids: batch, status: 'all', limit: 100 })
        const names = { ...memberNames.value }
        for (const m of page.items) {
          names[m.uid] = m.status === 1 ? m.display_name : `${m.display_name}（已停用）`
        }
        memberNames.value = names
      } catch {
        // 取不到名字时退回显示 uid，不影响条目本身
      }
    }
  }

  // ---------------- 白名单 ----------------

  function openWhitelistCreate (modelId?: number) {
    presetModelId.value = typeof modelId === 'number' ? modelId : null
    whitelistDialog.value = true
  }

  async function removeSelectedWhitelist () {
    try {
      const { deleted } = await catalogApi.batchDeleteWhitelist(selectedEntries.value)
      toast.success(`已删除 ${deleted} 条`)
      selectedEntries.value = []
      await load()
    } catch (error) {
      toast.error(error, '删除失败')
    }
  }

  async function toggleWhitelist (entry: WhitelistEntry) {
    try {
      await catalogApi.updateWhitelistEntry(entry.id, { allowed: !entry.allowed })
      toast.success('已更新')
      await load()
    } catch (error) {
      toast.error(error, '更新失败')
    }
  }

  async function removeWhitelist (entry: WhitelistEntry) {
    try {
      await catalogApi.deleteWhitelistEntry(entry.id)
      toast.success('条目已删除')
      await load()
    } catch (error) {
      toast.error(error, '删除失败')
    }
  }

  // ---------------- 模型 ----------------

  function openModelCreate () {
    editingModel.value = null
    Object.assign(modelForm, {
      provider_id: providers.value[0]?.id ?? null,
      model_name: '',
      display_name: '',
      alias: '',
      max_context_tokens: null,
      billing_mode: 'token',
      input_price_per_1m: null,
      output_price_per_1m: null,
      cached_input_price_per_1m: null,
      price_per_call: null,
      supports_stream: true,
      supports_multimodal: false,
    })
    modelDialog.value = true
  }

  function openModelEdit (model: ModelAdminInfo) {
    editingModel.value = model
    Object.assign(modelForm, {
      provider_id: model.provider_id,
      model_name: model.model_name,
      display_name: model.display_name,
      alias: model.alias ?? '',
      max_context_tokens: model.max_context_tokens,
      billing_mode: model.billing_mode ?? 'token',
      input_price_per_1m: model.input_price_per_1m,
      output_price_per_1m: model.output_price_per_1m,
      cached_input_price_per_1m: model.cached_input_price_per_1m,
      price_per_call: model.price_per_call,
      supports_stream: model.supports_stream,
      supports_multimodal: model.supports_multimodal,
    })
    modelDialog.value = true
  }

  async function submitModel () {
    submitting.value = true
    try {
      if (editingModel.value) {
        await catalogApi.updateModel(editingModel.value.id, {
          display_name: modelForm.display_name,
          // 清空别名必须发 null：undefined 会被 JSON.stringify 丢掉，后端只当「没传」，
          // 于是弹「模型已更新」而别名还在（2026-09-11 线上原样复现过）
          alias: modelForm.alias.trim() || null,
          max_context_tokens: modelForm.max_context_tokens ?? undefined,
          billing_mode: modelForm.billing_mode,
          // 这里必须显式带上 null：后端靠「字段在不在请求体里」区分清空与不改
          input_price_per_1m: priceOrNull(modelForm.input_price_per_1m),
          output_price_per_1m: priceOrNull(modelForm.output_price_per_1m),
          cached_input_price_per_1m: priceOrNull(modelForm.cached_input_price_per_1m),
          price_per_call: priceOrNull(modelForm.price_per_call),
          supports_stream: modelForm.supports_stream,
          supports_multimodal: modelForm.supports_multimodal,
        })
        toast.success('模型已更新')
      } else {
        if (modelForm.provider_id === null) {
          toast.warn('请先选择上游渠道')
          return
        }
        await catalogApi.createModel({
          provider_id: modelForm.provider_id,
          model_name: modelForm.model_name.trim(),
          display_name: modelForm.display_name.trim() || modelForm.model_name.trim(),
          alias: modelForm.alias.trim() || undefined,
          max_context_tokens: modelForm.max_context_tokens ?? undefined,
          billing_mode: modelForm.billing_mode,
          input_price_per_1m: priceOrNull(modelForm.input_price_per_1m) ?? undefined,
          output_price_per_1m: priceOrNull(modelForm.output_price_per_1m) ?? undefined,
          cached_input_price_per_1m:
            priceOrNull(modelForm.cached_input_price_per_1m) ?? undefined,
          price_per_call: priceOrNull(modelForm.price_per_call) ?? undefined,
          supports_stream: modelForm.supports_stream,
          supports_multimodal: modelForm.supports_multimodal,
        })
        toast.success('模型已登记')
      }
      modelDialog.value = false
      await load()
    } catch (error) {
      toast.error(error, '保存失败')
    } finally {
      submitting.value = false
    }
  }

  async function confirmDisableModel (model: ModelAdminInfo) {
    // 停用会连带清掉该模型的全部授权，这一点必须让操作者知道

    const ok = globalThis.confirm(
      `停用「${model.display_name}」会同时删除它的全部白名单授权，确认继续？`,
    )
    if (!ok) {
      return
    }

    try {
      await catalogApi.disableModel(model.id)
      toast.success('模型已停用，相关授权已清理')
      await load()
    } catch (error) {
      toast.error(error, '操作失败')
    }
  }

  async function enableModel (model: ModelAdminInfo) {
    try {
      await catalogApi.updateModel(model.id, { status: 1 })
      toast.success('模型已启用，需要重新配置授权')
      await load()
    } catch (error) {
      toast.error(error, '操作失败')
    }
  }

  // ---------------- 渠道 ----------------

  function openProviderCreate () {
    editingProvider.value = null
    Object.assign(providerForm, { code: '', name: '', base_url: '', api_key: '' })
    providerDialog.value = true
  }

  function openProviderEdit (provider: ProviderInfo) {
    editingProvider.value = provider
    Object.assign(providerForm, {
      code: provider.code,
      name: provider.name,
      base_url: provider.base_url,
      api_key: '',
    })
    providerDialog.value = true
  }

  async function submitProvider () {
    submitting.value = true
    try {
      if (editingProvider.value) {
        await catalogApi.updateProvider(editingProvider.value.id, {
          name: providerForm.name,
          base_url: providerForm.base_url,
          api_key: providerForm.api_key || undefined,
        })
        toast.success('渠道已更新')
      } else {
        await catalogApi.createProvider({
          code: providerForm.code.trim(),
          name: providerForm.name.trim(),
          base_url: providerForm.base_url.trim(),
          api_key: providerForm.api_key || undefined,
        })
        toast.success('渠道已创建')
      }
      providerDialog.value = false
      await load()
    } catch (error) {
      toast.error(error, '保存失败')
    } finally {
      submitting.value = false
    }
  }

  function applyRoute () {
    tab.value = validTabs.has(String(route.query.tab)) ? String(route.query.tab) : 'whitelist'
    const id = Number(route.query.model_id)
    if (id && enabledModels.value.some(m => m.id === id)) {
      openWhitelistCreate(id)
    }
  }

  function selectTab (value: unknown) {
    if (validTabs.has(String(value))) void router.replace({ path: '/models', query: { tab: String(value) } })
  }

  watch(() => route.query, async () => {
    await load()
    applyRoute()
  })

  onMounted(async () => {
    await loadRefs()
    await load()
    applyRoute()
  })
</script>
