<template>
  <!-- ============ 筛选条 ============ -->
  <v-card border class="mb-4" flat rounded="lg">
    <v-card-text class="pb-2">
      <div class="usage-filters flex flex-wrap items-center gap-3">
        <v-btn-toggle
          v-model="period"
          color="primary"
          density="comfortable"
          mandatory
          variant="outlined"
          @update:model-value="onPeriodChange"
        >
          <v-btn v-for="(label, value) in PERIOD_LABELS" :key="value" :value="value">
            {{ label }}
          </v-btn>
        </v-btn-toggle>

        <v-date-input
          v-model="dateRange"
          class="date-range-input"
          clearable
          density="compact"
          :display-format="formatDateDisplay"
          hide-details
          label="日期范围"
          multiple="range"
          prepend-icon=""
          prepend-inner-icon="mdi-calendar-range"
          variant="outlined"
          @update:model-value="onDateRangeChange"
        />

        <v-autocomplete
          v-if="scope === 'all'"
          v-model="deptId"
          class="filter-select"
          clearable
          density="compact"
          hide-details
          item-title="name"
          item-value="id"
          :items="filters.departments"
          label="部门"
          no-data-text="没有匹配项"
          variant="outlined"
        />

        <v-select
          v-model="modelId"
          class="filter-select"
          clearable
          density="compact"
          hide-details
          item-title="name"
          item-value="id"
          :items="filters.models"
          label="模型"
          variant="outlined"
        />

        <v-btn
          color="primary"
          :loading="loading"
          prepend-icon="mdi-magnify"
          text="查询"
          variant="tonal"
          @click="loadAll"
        />

        <v-chip
          :color="scope === 'all' ? 'primary' : 'warning'"
          size="small"
          variant="tonal"
        >
          数据范围：{{ USAGE_SCOPE_LABELS[scope] ?? scope }}
        </v-chip>
      </div>
    </v-card-text>
  </v-card>

  <!-- ============ 指标卡 ============ -->
  <v-row class="mb-5" dense>
    <v-col
      v-for="card in cards"
      :key="card.title"
      cols="12"
      md="3"
      sm="6"
    >
      <v-card border flat rounded="lg">
        <v-card-text>
          <div class="text-base font-bold">{{ card.title }}</div>

          <div class="text-xl mt-1 font-bold">
            <CompactNumber v-if="card.compact && typeof card.value === 'number'" :value="card.value" />
            <template v-else>{{ card.value }}</template>
          </div>

          <div class="text-caption text-medium-emphasis mt-1">{{ card.hint }}</div>
        </v-card-text>
      </v-card>
    </v-col>
  </v-row>

  <!-- 估算占比提醒：只在真的有估算数据时才出现，避免变成人人无视的常驻横幅 -->
  <v-alert
    v-if="summary && summary.total.estimated_requests > 0"
    class="mb-4"
    density="compact"
    icon="mdi-alert-outline"
    type="warning"
    variant="tonal"
  >
    有 {{ summary.total.estimated_requests }} 次请求（占 {{ estimatedShare }}）的 token
    是估算值 —— 上游未返回 usage，网关按字符数推算。这部分不宜用于成本分摊。
  </v-alert>

  <!-- ============ 趋势 ============ -->
  <v-card border class="mb-4" flat rounded="lg">
    <v-card-item>
      <v-card-title class="text-subtitle-1">用量趋势</v-card-title>

      <v-card-subtitle>{{ rangeText }}</v-card-subtitle>
    </v-card-item>

    <v-divider />

    <v-card-text>
      <div v-if="trendMax === 0" class="text-medium-emphasis text-center py-8">
        该区间内没有调用记录
      </div>

      <!-- 纯 CSS 柱状图：为一张趋势图引入图表库不值当，
           而且这样打包体积和依赖都少一份 -->
      <div
        v-else
        class="trend flex items-end gap-1"
        :class="{ 'trend--intraday': summary?.trend_interval === '5m' }"
      >
        <div
          v-for="point in summary?.trend ?? []"
          :key="point.bucket"
          class="trend-col grow min-w-0"
        >
          <v-tooltip location="top">
            <template #activator="{ props }">
              <div class="trend-bar-wrap flex items-end" v-bind="props">
                <div
                  class="trend-bar w-full"
                  :class="{ 'trend-bar--empty': point.metrics.total_tokens === 0 }"
                  :style="{ height: barHeight(point.metrics.total_tokens) }"
                />
              </div>
            </template>

            <div class="text-caption">
              <div>{{ point.label }}</div>
              <div>请求 {{ point.metrics.requests }} 次</div>
              <div>token {{ exactNumber(point.metrics.total_tokens) }}</div>

              <div v-if="point.metrics.failed_count > 0">
                失败 {{ point.metrics.failed_count }} 次
              </div>
            </div>
          </v-tooltip>
        </div>
      </div>

      <div v-if="trendMax > 0" class="flex justify-between mt-2">
        <span class="text-caption text-medium-emphasis">
          {{ summary?.trend[0]?.label }}
        </span>

        <span class="text-caption text-medium-emphasis">
          {{ summary?.trend.at(-1)?.label }}
        </span>
      </div>
    </v-card-text>
  </v-card>

  <!-- ============ 下钻 ============ -->
  <v-card border class="mb-4" flat rounded="lg">
    <v-card-item>
      <v-card-title class="text-subtitle-1">用量下钻</v-card-title>

      <template #append>
        <div class="flex gap-2 items-center">
          <v-btn-toggle
            v-model="dimension"
            color="primary"
            density="compact"
            mandatory
            variant="outlined"
            @update:model-value="loadBreakdown"
          >
            <v-btn v-for="d in USAGE_DIMENSIONS" :key="d.value" :value="d.value">
              {{ d.title }}
            </v-btn>
          </v-btn-toggle>

          <v-btn
            prepend-icon="mdi-download-outline"
            size="small"
            text="导出 CSV"
            variant="text"
            @click="exportCsv"
          />
        </div>
      </template>
    </v-card-item>

    <v-divider />

    <v-data-table
      density="comfortable"
      :headers="breakdownHeaders"
      hover
      item-value="key"
      :items="breakdownRows"
      :items-per-page="20"
      :loading="loading"
      no-data-text="该区间内没有调用记录"
    >
      <template #[`item.label`]="{ item }">
        <span class="font-weight-medium">{{ displayLabel(item) }}</span>
      </template>

      <template #[`item.requests`]="{ item }">
        {{ item.metrics.requests }}
        <span v-if="item.metrics.failed_count > 0" class="text-error text-caption">
          （失败 {{ item.metrics.failed_count }}）
        </span>
      </template>

      <template #[`item.prompt_tokens`]="{ item }">
        <CompactNumber :value="item.metrics.prompt_tokens" />
      </template>

      <template #[`item.completion_tokens`]="{ item }">
        <CompactNumber :value="item.metrics.completion_tokens" />
      </template>

      <template #[`item.total_tokens`]="{ item }">
        <CompactNumber class="font-weight-medium" :value="item.metrics.total_tokens" />

        <!-- 估算的部分单独挂个 chip，别让它混进「精确数字」的观感里 -->
        <v-chip
          v-if="item.metrics.estimated_tokens > 0"
          class="ms-1"
          color="warning"
          size="x-small"
          variant="tonal"
        >
          含估算 <CompactNumber :value="item.metrics.estimated_tokens" />
        </v-chip>
      </template>

      <template #[`item.avg_latency_ms`]="{ item }">
        {{ item.metrics.avg_latency_ms }} ms
      </template>

      <template #[`item.cost`]="{ item }">
        <span v-if="item.metrics.cost === null" class="text-medium-emphasis">
          —
          <v-tooltip activator="parent" location="top">
            该分组内存在未配置单价的模型，费用无法估算
          </v-tooltip>
        </span>

        <span v-else>
          {{ item.metrics.cost.toFixed(4) }}
          <!-- 只在这一行确实混了「按当前单价回算」时才提示：
               每行都挂一句的话，真正需要留意的那几行反而看不出来 -->
          <span v-if="item.metrics.cost_basis !== 'snapshot'" class="text-medium-emphasis">
            *
            <v-tooltip activator="parent" location="top">
              {{ item.metrics.cost_basis_label }}，金额会随日后调价变动
            </v-tooltip>
          </span>
        </span>
      </template>
    </v-data-table>

    <v-divider />

    <v-card-text class="text-caption text-medium-emphasis">
      {{ summary?.note }}
    </v-card-text>
  </v-card>

  <!-- ============ 明细 ============ -->
  <v-card border flat rounded="lg">
    <v-card-item>
      <v-card-title class="text-subtitle-1">调用明细</v-card-title>

      <v-card-subtitle>
        共 {{ recordTotal }} 条 · 只含指标，聊天内容请走会话审计
      </v-card-subtitle>

      <template #append>
        <v-btn
          :append-icon="showRecords ? 'mdi-chevron-up' : 'mdi-chevron-down'"
          size="small"
          :text="showRecords ? '收起' : '展开'"
          variant="text"
          @click="toggleRecords"
        />
      </template>
    </v-card-item>

    <template v-if="showRecords">
      <v-divider />

      <v-data-table-server
        density="comfortable"
        :headers="recordHeaders"
        hover
        item-value="id"
        :items="records"
        :items-length="recordTotal"
        :items-per-page="recordPageSize"
        :loading="recordLoading"
        no-data-text="该区间内没有调用记录"
        :page="recordPage"
        @update:options="onRecordOptions"
      >
        <template #[`item.created_at`]="{ item }">
          <span class="text-caption">{{ formatTime(item.created_at) }}</span>
        </template>

        <template #[`item.username`]="{ item }">
          {{ item.username }}
          <div class="text-caption text-medium-emphasis">
            {{ item.dept_name ?? '未分配部门' }} · {{ roleLabel(item.role) }}
          </div>
        </template>

        <template #[`item.total_tokens`]="{ item }">
          <CompactNumber :value="item.total_tokens" />

          <v-chip
            v-if="item.tokens_estimated"
            class="ms-1"
            color="warning"
            size="x-small"
            text="估算"
            variant="tonal"
          />

          <div v-if="item.cached_prompt_tokens > 0" class="text-caption text-medium-emphasis">
            缓存命中 <CompactNumber :value="item.cached_prompt_tokens" />
          </div>
        </template>

        <!-- 费用为 null 表示这笔当时没配单价（或早于单价快照上线）：
             显示「—」而不是 0.0000，0 会被读成「这次没花钱」 -->
        <template #[`item.cost`]="{ item }">
          <span v-if="item.cost === null" class="text-medium-emphasis">
            —
            <v-tooltip activator="parent" location="top">
              这次调用发生时该模型未配置单价，费用无法估算
            </v-tooltip>
          </span>

          <span v-else>
            {{ item.cost.toFixed(4) }}
            <v-tooltip activator="parent" location="top">
              {{ priceHint(item) }}
            </v-tooltip>
          </span>
        </template>

        <template #[`item.status`]="{ item }">
          <v-chip
            :color="item.status === 'success' ? 'success' : 'error'"
            size="small"
            :text="item.status === 'success' ? '成功' : (item.error_code ?? '失败')"
            variant="tonal"
          />

          <!-- 中断与成功/失败是正交的：这次调用成功了、token 也花了，
               只是用户点了「停止生成」。所以另起一个 chip，不混进状态里 -->
          <v-chip
            v-if="item.aborted"
            class="ms-1"
            color="warning"
            size="x-small"
            text="中断"
            variant="tonal"
          >
            <v-tooltip activator="parent" location="top">
              用户中途停止了生成。调用本身是成功的，已产生的 token 照常计费。
            </v-tooltip>
          </v-chip>
        </template>

        <!-- 流式与非流式的耗时、token 特征差别很大，排查时得先分清是哪种 -->
        <template #[`item.stream`]="{ item }">
          <v-chip
            :color="item.stream ? 'primary' : 'default'"
            size="x-small"
            :text="item.stream ? '流式' : '直接'"
            variant="tonal"
          />
        </template>

        <template #[`item.request_id`]="{ item }">
          <span
            class="request-id text-caption cursor-pointer"
            @click="copyRequestId(item.request_id)"
          >
            {{ item.request_id }}
            <v-tooltip activator="parent" location="top">
              点击复制。拿它到「会话审计」页可直接定位这次调用的聊天原文
            </v-tooltip>
          </span>
        </template>

        <template #[`item.latency_ms`]="{ item }">
          {{ item.latency_ms }} ms
        </template>
      </v-data-table-server>
    </template>
  </v-card>
</template>

<script lang="ts" setup>
  import type {
    UsageBreakdownResult,
    UsageFilterOptions,
    UsageGroupRow,
    UsageRecordRow,
    UsageSummary,
  } from '@/types/api'
  import { computed, onMounted, ref } from 'vue'
  import * as usageApi from '@/api/usage'
  import CompactNumber from '@/components/CompactNumber.vue'
  import { roleLabel } from '@/constants/roles'
  import { compactNumber, exactNumber } from '@/lib/numbers'
  import { useToastStore } from '@/stores/toast'
  import {
    USAGE_DIMENSIONS,
    USAGE_SCOPE_LABELS,
  } from '@/types/api'

  const toast = useToastStore()
  const PERIOD_LABELS = { day: '按日', week: '按周', month: '按月' }

  // ---------------- 查询条件 ----------------
  const period = ref<'day' | 'week' | 'month' | 'custom'>('day')
  const dateRange = ref<Date[]>([])
  const deptId = ref<number | null>(null)
  const modelId = ref<number | null>(null)
  const dimension = ref('user')

  // ---------------- 数据 ----------------
  const summary = ref<UsageSummary | null>(null)
  const breakdown = ref<UsageBreakdownResult | null>(null)
  const filters = ref<UsageFilterOptions>({ departments: [], models: [], scope: 'self' })
  const loading = ref(false)

  const records = ref<UsageRecordRow[]>([])
  const recordTotal = ref(0)
  const recordPage = ref(1)
  const recordPageSize = ref(20)
  const recordLoading = ref(false)
  const showRecords = ref(false)

  const scope = computed(() => summary.value?.scope ?? filters.value.scope)
  const breakdownRows = computed(() => breakdown.value?.items ?? [])

  const start = computed(() => formatLocalDate(dateRange.value.reduce<Date | undefined>(
    (earliest, value) => !earliest || value < earliest ? value : earliest,
    undefined,
  )))
  const end = computed(() => formatLocalDate(dateRange.value.reduce<Date | undefined>(
    (latest, value) => !latest || value > latest ? value : latest,
    undefined,
  )))

  function query () {
    const singleDay = Boolean(start.value && start.value === end.value)
    return {
      start: start.value,
      end: end.value,
      granularity: 'day',
      trend_interval: singleDay ? '5m' as const : 'auto' as const,
      dept_id: deptId.value ?? undefined,
      model_id: modelId.value ?? undefined,
    }
  }

  // ---------------- 指标卡 ----------------
  const cards = computed(() => {
    const t = summary.value?.total
    if (!t) {
      return [
        { title: '请求数', value: '—', hint: '' },
        { title: '总 token', value: '—', hint: '' },
        { title: '平均耗时', value: '—', hint: '' },
        { title: '估算费用', value: '—', hint: '' },
      ]
    }

    const successRate = t.requests > 0
      ? `${((t.success_count / t.requests) * 100).toFixed(1)}% 成功`
      : '暂无调用'

    return [
      { title: '请求数', value: exactNumber(t.requests), hint: successRate },
      {
        title: '总 token',
        value: t.total_tokens,
        compact: true,
        hint: `输入 ${compactNumber(t.prompt_tokens)} / 输出 ${compactNumber(t.completion_tokens)}`,
      },
      { title: '平均耗时', value: `${t.avg_latency_ms} ms`, hint: '端到端，含网关开销' },
      {
        title: '估算费用',
        // null 表示有模型没配单价，显示「—」而不是 0：0 会让人以为真的没花钱
        value: t.cost === null ? '—' : `${t.cost.toFixed(2)} ${t.currency}`,
        // 口径直接写在卡片上：按调用时单价算的金额是稳定的，
        // 按当前单价回算的会随日后调价变动，两者不能长一个样
        hint: t.cost === null
          ? '部分模型未配置单价'
          : (summary.value?.cost_note || `${t.cost_basis_label}，仅供参考`),
      },
    ]
  })

  const estimatedShare = computed(() => {
    const t = summary.value?.total
    if (!t || t.requests === 0) {
      return '0%'
    }
    return `${((t.estimated_requests / t.requests) * 100).toFixed(1)}%`
  })

  const rangeText = computed(() => {
    const r = summary.value?.range
    return r ? `${r.start} ~ ${r.end}（服务器本地时区）` : ''
  })

  // ---------------- 趋势条 ----------------
  const trendMax = computed(() =>
    Math.max(0, ...(summary.value?.trend ?? []).map(p => p.metrics.total_tokens)),
  )

  function barHeight (value: number): string {
    if (trendMax.value <= 0) {
      return '0%'
    }
    // 有数据但极小的桶也要看得见，给个 2% 下限
    return `${Math.max(2, (value / trendMax.value) * 100)}%`
  }

  // ---------------- 表头 ----------------
  const breakdownHeaders = [
    { title: '分组', key: 'label' },
    { title: '请求数', key: 'requests', sortable: false },
    { title: '输入 token', key: 'prompt_tokens', sortable: false },
    { title: '输出 token', key: 'completion_tokens', sortable: false },
    { title: '合计 token', key: 'total_tokens', sortable: false },
    { title: '平均耗时', key: 'avg_latency_ms', sortable: false },
    { title: '估算费用', key: 'cost', sortable: false, align: 'end' as const },
  ]

  const recordHeaders = [
    { title: '时间', key: 'created_at', sortable: false, width: 160 },
    { title: '用户', key: 'username', sortable: false },
    { title: '模型', key: 'model_name', sortable: false },
    { title: '方式', key: 'stream', sortable: false, width: 80 },
    { title: '合计 token', key: 'total_tokens', sortable: false },
    // 明细页的用处之一就是回答「这笔怎么这么贵」，
    // 所以费用要能逐笔看到，且鼠标悬上去能看到当时按什么价算的
    { title: '费用', key: 'cost', sortable: false, align: 'end' as const, width: 110 },
    { title: '耗时', key: 'latency_ms', sortable: false },
    { title: '状态', key: 'status', sortable: false, width: 150 },
    // 放最后：它长且不常读，但要追到聊天原文时非它不可
    { title: '请求ID', key: 'request_id', sortable: false, width: 180 },
  ]

  /** 复制请求ID —— 到「会话审计」页粘进去就能定位这次调用的原文 */
  async function copyRequestId (requestId: string) {
    try {
      await navigator.clipboard.writeText(requestId)
      toast.success('已复制请求ID，可在「会话审计」页检索')
    } catch {
      toast.error(null, '复制失败，请手动选中')
    }
  }

  // ---------------- 格式化 ----------------
  function formatLocalDate (value?: Date): string | undefined {
    if (!value || Number.isNaN(value.getTime())) return undefined
    const year = value.getFullYear()
    const month = String(value.getMonth() + 1).padStart(2, '0')
    const day = String(value.getDate()).padStart(2, '0')
    return `${year}-${month}-${day}`
  }

  function formatDateDisplay (value: unknown): string {
    const parsed = value instanceof Date ? value : new Date(String(value))
    return formatLocalDate(parsed) ?? ''
  }

  function datesBetween (from: Date, to: Date): Date[] {
    const dates: Date[] = []
    const cursor = new Date(from.getFullYear(), from.getMonth(), from.getDate())
    const last = new Date(to.getFullYear(), to.getMonth(), to.getDate())
    while (cursor <= last) {
      dates.push(new Date(cursor))
      cursor.setDate(cursor.getDate() + 1)
    }
    return dates
  }

  function setPeriodRange (value: 'day' | 'week' | 'month') {
    const today = new Date()
    let from = new Date(today.getFullYear(), today.getMonth(), today.getDate())
    let to = new Date(from)
    if (value === 'week') {
      const mondayOffset = (today.getDay() + 6) % 7
      from.setDate(from.getDate() - mondayOffset)
      to = new Date(from)
      to.setDate(to.getDate() + 6)
    } else if (value === 'month') {
      from = new Date(today.getFullYear(), today.getMonth(), 1)
      to = new Date(today.getFullYear(), today.getMonth() + 1, 0)
    }
    dateRange.value = datesBetween(from, to)
  }

  /**
   * 这笔费用当时按什么价算的
   *
   * 单价快照上线前的记录没有这些字段，此时如实说「按当前单价回算」——
   * 那种金额会随日后调价变动，不能和快照价长一个样。
   */
  function priceHint (row: UsageRecordRow): string {
    const unit = row.cost_currency ?? 'CNY'
    if (row.billing_mode === 'per_call') {
      return `按次计价：${row.price_per_call ?? '—'} ${unit}/次（调用时单价）`
    }
    if (row.input_price_per_1m === null) {
      return '该记录早于单价快照上线，费用按当前单价回算，会随调价变动'
    }
    const cached = row.cached_input_price_per_1m === null
      ? '缓存未单独配价，按输入单价计'
      : `缓存 ${row.cached_input_price_per_1m}`
    return `调用时单价：输入 ${row.input_price_per_1m} / 输出 ${row.output_price_per_1m} ${unit}/百万 token（${cached}）`
  }

  function formatTime (iso: string | null): string {
    if (!iso) {
      return '—'
    }
    return iso.replace('T', ' ').slice(0, 19)
  }

  /** 角色维度的键是角色编码，翻成中文由前端这份镜像表负责 */
  function displayLabel (row: UsageGroupRow): string {
    if (dimension.value === 'role' && row.key) {
      return roleLabel(row.key)
    }
    return row.label
  }

  // ---------------- 加载 ----------------
  function onPeriodChange (value: 'day' | 'week' | 'month' | 'custom') {
    if (!value || value === 'custom') return
    setPeriodRange(value)
    void loadAll()
  }

  function onDateRangeChange () {
    period.value = 'custom'
  }

  async function loadSummary () {
    summary.value = await usageApi.fetchUsageSummary(query())
  }

  async function loadBreakdown () {
    breakdown.value = await usageApi.fetchUsageBreakdown(dimension.value, query())
  }

  async function loadAll () {
    loading.value = true
    try {
      await Promise.all([loadSummary(), loadBreakdown()])
      if (showRecords.value) {
        await loadRecords()
      }
    } catch (error) {
      toast.error(error, '加载用量数据失败')
    } finally {
      loading.value = false
    }
  }

  async function loadRecords () {
    recordLoading.value = true
    try {
      const result = await usageApi.fetchUsageRecords({
        ...query(),
        granularity: undefined,
        page: recordPage.value,
        page_size: recordPageSize.value,
      })
      records.value = result.items
      recordTotal.value = result.total
    } catch (error) {
      toast.error(error, '加载明细失败')
    } finally {
      recordLoading.value = false
    }
  }

  function toggleRecords () {
    showRecords.value = !showRecords.value
    // 明细表是唯一会扫 usage_records 的查询，不展开就不查
    if (showRecords.value && records.value.length === 0) {
      loadRecords()
    }
  }

  function onRecordOptions (options: { page: number, itemsPerPage: number }) {
    recordPage.value = options.page
    recordPageSize.value = options.itemsPerPage
    if (showRecords.value) {
      loadRecords()
    }
  }

  function exportCsv () {
    // 导出接口返回文件流，直接开新窗口让浏览器接管下载
    window.open(usageApi.buildExportUrl(dimension.value, query()), '_blank')
  }

  onMounted(async () => {
    setPeriodRange('day')
    try {
      filters.value = await usageApi.fetchUsageFilters()
    } catch {
      // 下拉框拿不到不影响主流程，静默降级成空列表
    }
    await loadAll()
  })
</script>

<style scoped>
/* 柱高是百分比，百分比要能解析就必须有**确定的**父高度，
   所以 .trend 和 .trend-bar-wrap 的高度必须一致且写死，用变量绑在一起免得改漏。

   ⚠️ 这里只写「Tailwind 没有的东西」（主题色、过渡、定高）。
   flex 布局一律走模板里的 Tailwind 类 —— Vuetify 的 d-flex/ga-1 在本项目
   是空类名（settings.scss 里 $utilities: false），曾经就是它们让这张图整个不显示。 */
.trend,
.trend-bar-wrap {
  height: var(--trend-height, 160px);
}

.trend-bar {
  background: rgb(var(--v-theme-primary));
  border-radius: 2px 2px 0 0;
  min-height: 2px;
  transition: height 0.2s ease;
}

/* 请求ID 是等宽才好逐字核对，鼠标悬停给个可点的暗示 */
.request-id {
  font-family: var(--font-mono);
  opacity: 0.7;
}

.request-id:hover {
  opacity: 1;
  text-decoration: underline;
}

.cursor-pointer {
  cursor: pointer;
}

/* 没有数据的桶画成一条浅灰底线：既能看出缺口，又不会被误读成有量 */
.trend-bar--empty {
  background: rgba(var(--v-theme-on-surface), 0.12);
  height: 2px !important;
}

.trend--intraday {
  gap: 1px;
  overflow-x: auto;
}

.trend--intraday .trend-col {
  flex: 1 0 2px;
}

.date-range-input {
  flex: 1 1 280px;
  min-width: min(100%, 260px);
  max-width: 360px;
}

.filter-select {
  flex: 1 1 180px;
  min-width: min(100%, 180px);
  max-width: 220px;
}

@media (max-width: 600px) {
  .date-range-input,
  .filter-select {
    flex-basis: 100%;
    max-width: none;
  }
}
</style>
