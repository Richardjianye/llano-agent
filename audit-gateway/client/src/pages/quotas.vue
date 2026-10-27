<template>
  <v-card border flat rounded="lg">
    <v-card-item>
      <v-card-title class="text-subtitle-1">限流与配额</v-card-title>

      <v-card-subtitle>
        按用户或部门限制单个模型的每日 token；额度耗尽后硬拒绝，由用户手动切换模型
      </v-card-subtitle>

      <template #append>
        <v-btn color="primary" prepend-icon="mdi-plus" variant="tonal" @click="openCreate">
          新建规则
        </v-btn>
      </template>
    </v-card-item>

    <v-divider />

    <v-alert
      class="m-4"
      density="compact"
      text="用户规则与部门规则同时生效，任意一条耗尽都会拒绝该模型；其他模型不受影响。"
      type="info"
      variant="tonal"
    />

    <v-data-table
      density="comfortable"
      :headers="headers"
      item-value="id"
      :items="rules"
      :items-per-page="20"
      :loading="loading"
      no-data-text="尚未配置每日配额"
    >
      <template #[`item.scope_name`]="{ item }">
        <div class="font-weight-medium">{{ item.scope_name }}</div>

        <div class="text-caption text-medium-emphasis">
          {{ item.scope === 'user' ? '用户' : '部门' }}
        </div>
      </template>

      <template #[`item.usage`]="{ item }">
        <div class="quota-usage">
          <div class="flex justify-between text-caption">
            <span>{{ formatTokens(item.used_tokens) }}</span>
            <span>{{ formatTokens(item.daily_token_limit) }}</span>
          </div>

          <v-progress-linear
            :color="item.exceeded ? 'error' : usageRatio(item) >= 0.8 ? 'warning' : 'primary'"
            height="7"
            :model-value="Math.min(100, usageRatio(item) * 100)"
            rounded
          />
        </div>
      </template>

      <template #[`item.status`]="{ item }">
        <v-chip
          :color="item.status !== 1 ? 'grey' : item.exceeded ? 'error' : 'success'"
          size="small"
          :text="item.status !== 1 ? '已停用' : item.exceeded ? '已耗尽' : '生效中'"
          variant="tonal"
        />
      </template>

      <template #[`item.actions`]="{ item }">
        <v-btn size="small" text="编辑" variant="text" @click="openEdit(item)" />

        <v-btn
          color="error"
          size="small"
          text="删除"
          variant="text"
          @click="confirmDelete(item)"
        />
      </template>
    </v-data-table>
  </v-card>

  <v-dialog v-model="editDialog" max-width="600" persistent>
    <v-card :title="editingId ? '编辑配额规则' : '新建配额规则'">
      <v-card-text>
        <v-form ref="formRef" @submit.prevent="submitSave">
          <v-select
            v-model="form.scope"
            density="comfortable"
            item-title="label"
            item-value="value"
            :items="scopeOptions"
            label="作用对象"
            variant="outlined"
            @update:model-value="form.scope_value = ''"
          />

          <MemberPicker
            v-if="form.scope === 'user'"
            v-model="form.scope_value"
            class="mt-2"
            label="用户"
            :rules="[required]"
          />

          <v-select
            v-else
            v-model="form.scope_value"
            class="mt-2"
            density="comfortable"
            item-title="label"
            item-value="value"
            :items="deptOptions"
            label="部门"
            :rules="[required]"
            variant="outlined"
          />

          <v-select
            v-model="form.model_id"
            class="mt-2"
            density="comfortable"
            item-title="label"
            item-value="value"
            :items="modelOptions"
            label="模型"
            :rules="[positive]"
            variant="outlined"
          />

          <v-text-field
            v-model.number="form.daily_token_limit"
            class="mt-2"
            density="comfortable"
            hint="当天已落账 token 达到该值后，后续请求返回 429"
            label="每日 token 上限"
            min="1"
            persistent-hint
            :rules="[positive]"
            type="number"
            variant="outlined"
          />

          <v-switch
            v-model="form.enabled"
            color="primary"
            hide-details
            label="启用规则"
          />

          <v-textarea
            v-model="form.remark"
            auto-grow
            density="comfortable"
            label="备注（可空）"
            maxlength="255"
            rows="2"
            variant="outlined"
          />
        </v-form>
      </v-card-text>

      <v-card-actions>
        <v-spacer />
        <v-btn text="取消" @click="editDialog = false" />

        <v-btn
          color="primary"
          :loading="submitting"
          text="保存"
          variant="tonal"
          @click="submitSave"
        />
      </v-card-actions>
    </v-card>
  </v-dialog>

  <v-dialog v-model="deleteDialog" max-width="460">
    <v-card title="删除配额规则">
      <v-card-text>
        确认删除「{{ current?.scope_name }} · {{ current?.model_name }}」的每日配额？
      </v-card-text>

      <v-card-actions>
        <v-spacer />
        <v-btn text="取消" @click="deleteDialog = false" />

        <v-btn
          color="error"
          :loading="submitting"
          text="确认删除"
          variant="tonal"
          @click="submitDelete"
        />
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<script lang="ts" setup>
  import type { Department, ModelAdminInfo, TokenQuotaRule } from '@/types/api'
  import { computed, onMounted, reactive, ref } from 'vue'
  import { listModels } from '@/api/catalog'
  import { listDepartments } from '@/api/department'
  import * as quotaApi from '@/api/quota'
  import MemberPicker from '@/components/MemberPicker.vue'
  import { useToastStore } from '@/stores/toast'

  const toast = useToastStore()
  const headers = [
    { title: '作用对象', key: 'scope_name', width: 180 },
    { title: '模型', key: 'model_name' },
    { title: '今日用量 / 上限', key: 'usage', width: 280, sortable: false },
    { title: '状态', key: 'status', width: 110 },
    { title: '备注', key: 'remark' },
    { title: '操作', key: 'actions', align: 'end' as const, sortable: false, width: 160 },
  ]

  const scopeOptions = [
    { label: '指定用户', value: 'user' },
    { label: '整个部门', value: 'dept' },
  ]
  const rules = ref<TokenQuotaRule[]>([])
  const departments = ref<Department[]>([])
  const models = ref<ModelAdminInfo[]>([])
  const loading = ref(false)
  const submitting = ref(false)
  const editDialog = ref(false)
  const deleteDialog = ref(false)
  const editingId = ref<number | null>(null)
  const current = ref<TokenQuotaRule | null>(null)
  const formRef = ref<{ validate: () => Promise<{ valid: boolean }> } | null>(null)
  const form = reactive({
    scope: 'user' as 'user' | 'dept',
    scope_value: '',
    model_id: 0,
    daily_token_limit: 100_000,
    enabled: true,
    remark: '',
  })

  const deptOptions = computed(() => departments.value.map(dept => ({
    value: String(dept.id), label: dept.name,
  })))
  const modelOptions = computed(() => models.value.map(model => ({
    value: model.id, label: model.display_name || model.model_name,
  })))

  const required = (value: string) => Boolean(value) || '必填'
  const positive = (value: number) => Number(value) > 0 || '必须大于 0'

  function formatTokens (value: number): string {
    return new Intl.NumberFormat('zh-CN').format(value)
  }

  function usageRatio (item: TokenQuotaRule): number {
    return item.daily_token_limit > 0 ? item.used_tokens / item.daily_token_limit : 0
  }

  async function load () {
    loading.value = true
    try {
      const result = await quotaApi.listQuotas()
      rules.value = result.items
    } catch (error) {
      toast.error(error, '加载配额失败')
    } finally {
      loading.value = false
    }
  }

  function resetForm () {
    Object.assign(form, {
      scope: 'user', scope_value: '', model_id: models.value[0]?.id ?? 0,
      daily_token_limit: 100_000, enabled: true, remark: '',
    })
  }

  function openCreate () {
    editingId.value = null
    resetForm()
    editDialog.value = true
  }

  function openEdit (item: TokenQuotaRule) {
    editingId.value = item.id
    Object.assign(form, {
      scope: item.scope, scope_value: item.scope_value, model_id: item.model_id,
      daily_token_limit: item.daily_token_limit, enabled: item.status === 1,
      remark: item.remark ?? '',
    })
    editDialog.value = true
  }

  async function submitSave () {
    const validation = await formRef.value?.validate()
    if (!validation?.valid) return
    submitting.value = true
    try {
      await quotaApi.saveQuota({
        id: editingId.value ?? undefined,
        scope: form.scope,
        scope_value: form.scope_value,
        model_id: form.model_id,
        daily_token_limit: form.daily_token_limit,
        status: form.enabled ? 1 : 0,
        remark: form.remark || undefined,
      })
      editDialog.value = false
      toast.success('配额规则已保存')
      await load()
    } catch (error) {
      toast.error(error, '保存配额失败')
    } finally {
      submitting.value = false
    }
  }

  function confirmDelete (item: TokenQuotaRule) {
    current.value = item
    deleteDialog.value = true
  }

  async function submitDelete () {
    if (!current.value) return
    submitting.value = true
    try {
      await quotaApi.deleteQuota(current.value.id)
      deleteDialog.value = false
      toast.success('配额规则已删除')
      await load()
    } catch (error) {
      toast.error(error, '删除失败')
    } finally {
      submitting.value = false
    }
  }

  onMounted(async () => {
    try {
      const [deptResult, modelResult] = await Promise.all([
        listDepartments(true), listModels(),
      ])
      departments.value = deptResult.items
      models.value = modelResult.items
    } catch (error) {
      toast.error(error, '加载配置选项失败')
    }
    await load()
  })
</script>

<style scoped>
  .quota-usage { min-width: 220px; }
</style>
