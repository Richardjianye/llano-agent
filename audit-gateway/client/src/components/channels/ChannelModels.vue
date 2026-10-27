<template>
  <div>
    <v-alert class="mb-4" text="登记模型不会自动授权。授权与额度继续在「模型与白名单」「限流与配额」中配置。" type="info" variant="tonal" />

    <v-alert
      v-if="error"
      class="mb-4"
      :text="error"
      type="error"
      variant="tonal"
    />

    <v-card
      v-if="discovered.length > 0"
      border
      class="mb-4"
      flat
      title="探测结果"
    >
      <v-card-text v-if="can('model:edit')"><v-btn :disabled="selectedDiscovery.length === 0 || saving" :text="`批量登记已选 ${selectedDiscovery.length} 项`" @click="openBatch" /></v-card-text>

      <v-data-table
        v-model="selectedDiscovery"
        first-page-label="首页"
        :headers="discoveryHeaders"
        item-selectable="can_register"
        item-value="id"
        :items="discoveryRows"
        :items-per-page="10"
        items-per-page-text="每页条数"
        last-page-label="末页"
        next-page-label="下一页"
        no-data-text="没有发现模型"
        page-text="{0}–{1}，共 {2} 项"
        prev-page-label="上一页"
        :show-select="can('model:edit')"
      >
        <template #[`item.id`]="{ item }"><div>{{ item.title }}</div><code class="text-caption">{{ item.id }}</code></template>
        <template #[`item.supported`]="{ item }">{{ item.supported ? `${item.protocol} · ${item.resolution}${image ? ` · 最多 ${item.max_reference_images} 张参考图` : ''}` : '暂不支持此生图模型' }}</template>

        <template #[`item.actions`]="{ item }">
          <span v-if="models.some(m => m.model_name === item.id)">已登记</span>

          <v-btn
            v-else-if="can('model:edit') && item.supported"
            size="small"
            text="登记模型"
            variant="text"
            @click="openCreate(item)"
          />
        </template>
      </v-data-table>
    </v-card>

    <v-data-table
      first-page-label="首页"
      :headers="headers"
      :items="models"
      :items-per-page="10"
      items-per-page-text="每页条数"
      last-page-label="末页"
      next-page-label="下一页"
      no-data-text="此渠道尚未登记模型"
      page-text="{0}–{1}，共 {2} 项"
      prev-page-label="上一页"
    >
      <template #[`item.display_name`]="{ item }"><div>{{ item.display_name }}</div><code class="text-caption">{{ item.alias || item.model_name }}</code></template>
      <template #[`item.billing_mode`]="{ item }">{{ item.billing_mode === 'per_call' ? '按次' : '按 token' }} · {{ priceLabel(item) }}</template>
      <template #[`item.status`]="{ item }">{{ item.status === 1 ? '已启用' : '已停用' }}</template>

      <template #[`item.actions`]="{ item }">
        <v-btn
          v-if="can('model:edit')"
          size="small"
          text="编辑"
          variant="text"
          @click="openEdit(item)"
        />

        <v-btn
          v-if="can('model:edit')"
          size="small"
          :text="item.status === 1 ? '停用并清授权' : '启用'"
          variant="text"
          @click="toggle(item)"
        />

        <v-btn size="small" text="授权" :to="{ path: '/models', query: { model_id: item.id, tab: 'whitelist' } }" variant="text" />
        <v-btn size="small" text="配额" to="/quotas" variant="text" />
      </template>
    </v-data-table>

    <v-dialog v-model="dialog" max-width="580" persistent>
      <v-card :title="editing ? '编辑模型' : batch.length > 0 ? '批量登记模型' : '登记模型'">
        <v-card-text>
          <v-alert
            v-if="formError"
            class="mb-3"
            :text="formError"
            type="error"
            variant="tonal"
          />

          <template v-if="batch.length > 0">
            <v-alert class="mb-3" text="逐项登记，成功项保留；失败项可单独修正后重试。计价应用于本次选择，登记后仍可逐项修改。" type="info" variant="tonal" />

            <div v-for="m in batch" :key="m.id" class="mb-4">
              <code>{{ m.id }}</code>
              <v-text-field v-model="m.title" label="中文展示名" maxlength="128" />
              <v-text-field v-model="m.alias" label="对外别名（可选，不含空格）" />
            </div>
          </template>

          <template v-else>
            <v-text-field label="真实模型 ID" :model-value="form.model_name" readonly />
            <v-text-field v-model="form.display_name" label="中文展示名" maxlength="128" />
            <v-text-field v-model="form.alias" hint="不能包含空格；留空使用真实模型名" label="对外别名" persistent-hint />
          </template>

          <v-select v-model="form.billing_mode" :items="[{ title: '按次', value: 'per_call' }, { title: '按 token', value: 'token' }]" label="计费方式（以供应商账单为准）" />

          <v-text-field
            v-if="form.billing_mode === 'per_call'"
            v-model="form.price_per_call"
            label="每次单价（元，留空表示未知）"
            min="0"
            type="number"
          />

          <template v-else>
            <v-text-field v-model="form.input_price_per_1m" label="输入单价（元/百万 token，留空表示未知）" min="0" type="number" />
            <v-text-field v-model="form.output_price_per_1m" label="输出单价（元/百万 token，留空表示未知）" min="0" type="number" />
          </template>
        </v-card-text>

        <v-card-actions><v-spacer /><v-btn :disabled="saving" text="取消" @click="dialog = false" /><v-btn color="primary" :loading="saving" text="保存" @click="save" /></v-card-actions>
      </v-card>
    </v-dialog>
  </div>
</template>
<script setup lang="ts">
  import type { DiscoveredModel } from '@/api/channels'
  import type { ModelAdminInfo } from '@/types/api'
  import { computed, reactive, ref, watch } from 'vue'
  import { createModel, disableModel, updateModel } from '@/api/catalog'
  import { usePermission } from '@/composables/usePermission'
  const props = defineProps<{ providerId: number, models: ModelAdminInfo[], discovered: DiscoveredModel[], image?: boolean }>()
  const emit = defineEmits<{ changed: [] }>()
  const { can } = usePermission()
  const headers = [{ title: '模型', key: 'display_name' }, { title: '计价', key: 'billing_mode' }, { title: '状态', key: 'status' }, { title: '操作', key: 'actions', sortable: false }]
  const discoveryHeaders = [{ title: '模型', key: 'id' }, { title: '能力', key: 'supported' }, { title: '登记', key: 'actions', sortable: false }]
  const selectedDiscovery = ref<string[]>([])
  const batch = ref<{ id: string, title: string, alias: string }[]>([])
  const discoveryRows = computed(() => props.discovered.map(m => ({ ...m, can_register: m.supported && !props.models.some(row => row.model_name === m.id) })))
  watch(() => props.discovered, () => {
    selectedDiscovery.value = []
  })
  const dialog = ref(false)
  const saving = ref(false)
  const error = ref('')
  const formError = ref('')
  const editing = ref<ModelAdminInfo | null>(null)
  const form = reactive({ model_name: '', display_name: '', alias: '', billing_mode: 'token', price_per_call: '' as string | number | null, input_price_per_1m: '' as string | number | null, output_price_per_1m: '' as string | number | null })
  function priceLabel (m: ModelAdminInfo) {
    if (m.billing_mode === 'per_call') return m.price_per_call == null ? '单价未配置' : `${m.price_per_call} ${m.currency}/次`
    return m.input_price_per_1m == null || m.output_price_per_1m == null ? '单价未配置' : `${m.input_price_per_1m}/${m.output_price_per_1m} ${m.currency}/百万 token`
  }
  function openCreate (m: DiscoveredModel) {
    batch.value = []
    editing.value = null
    formError.value = ''
    Object.assign(form, { model_name: m.id, display_name: m.title, alias: '', billing_mode: 'token', price_per_call: '', input_price_per_1m: '', output_price_per_1m: '' })
    dialog.value = true
  }
  function openEdit (m: ModelAdminInfo) {
    batch.value = []
    editing.value = m
    formError.value = ''
    Object.assign(form, m, { alias: m.alias || '' })
    dialog.value = true
  }
  const price = (v: string | number | null) => v === '' || v == null ? null : Number(v)
  async function save () {
    if (saving.value) return
    if (batch.value.length > 0) return saveBatch()
    formError.value = ''
    if (!form.display_name.trim()) {
      formError.value = '请填写展示名'
      return
    }
    if ([form.price_per_call, form.input_price_per_1m, form.output_price_per_1m].some(v => v != null && v !== '' && (!Number.isFinite(Number(v)) || Number(v) < 0))) {
      formError.value = '单价必须是非负数或留空'
      return
    }
    saving.value = true
    try {
      const body = { display_name: form.display_name.trim(), alias: form.alias.trim() || null, billing_mode: form.billing_mode, price_per_call: price(form.price_per_call), input_price_per_1m: price(form.input_price_per_1m), output_price_per_1m: price(form.output_price_per_1m) }
      await (editing.value ? updateModel(editing.value.id, body) : createModel({ ...body, alias: body.alias || undefined, price_per_call: body.price_per_call ?? undefined, input_price_per_1m: body.input_price_per_1m ?? undefined, output_price_per_1m: body.output_price_per_1m ?? undefined, provider_id: props.providerId, model_name: form.model_name, supports_stream: !props.image, supports_multimodal: !!props.image }))
      dialog.value = false
      emit('changed')
    } catch (error_) {
      formError.value = error_ instanceof Error ? error_.message : '保存失败'
    } finally {
      saving.value = false
    }
  }
  function openBatch () {
    const items = discoveryRows.value.filter(m => m.can_register && selectedDiscovery.value.includes(m.id))
    if (items.length === 0) return
    openCreate(items[0]!)
    batch.value = items.map(m => ({ id: m.id, title: m.title, alias: '' }))
  }
  async function saveBatch () {
    if (batch.value.some(m => !m.title.trim() || /\s/.test(m.alias))) {
      formError.value = '请填写每项展示名，别名不能含空格'
      return
    }
    if ([form.price_per_call, form.input_price_per_1m, form.output_price_per_1m].some(v => v != null && v !== '' && (!Number.isFinite(Number(v)) || Number(v) < 0))) {
      formError.value = '单价必须是非负数或留空'
      return
    }
    saving.value = true
    const failures: string[] = []
    const pending: typeof batch.value = []
    for (const m of batch.value) {
      try {
        await createModel({ provider_id: props.providerId, model_name: m.id, display_name: m.title.trim(), alias: m.alias.trim() || undefined, supports_stream: !props.image, supports_multimodal: !!props.image, billing_mode: form.billing_mode, price_per_call: price(form.price_per_call) ?? undefined, input_price_per_1m: price(form.input_price_per_1m) ?? undefined, output_price_per_1m: price(form.output_price_per_1m) ?? undefined })
      } catch (error_) {
        pending.push(m)
        failures.push(`${m.id}：${error_ instanceof Error ? error_.message : '登记失败'}`)
      }
    }
    const succeeded = batch.value.length - pending.length
    batch.value = pending
    formError.value = failures.length > 0 ? `成功 ${succeeded} 项，失败 ${pending.length} 项。仅保留失败项：\n${failures.join('\n')}` : ''
    selectedDiscovery.value = []
    saving.value = false
    if (pending.length === 0) dialog.value = false
    emit('changed')
  }
  async function toggle (m: ModelAdminInfo) {
    if (saving.value) return
    saving.value = true
    error.value = ''
    try {
      await (m.status === 1 ? disableModel(m.id) : updateModel(m.id, { status: 1 }))
      emit('changed')
    } catch (error_) {
      error.value = error_ instanceof Error ? error_.message : '修改失败'
    } finally {
      saving.value = false
    }
  }
</script>
