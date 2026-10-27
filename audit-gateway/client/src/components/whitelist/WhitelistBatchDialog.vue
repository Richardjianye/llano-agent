<template>
  <v-dialog v-model="open" max-width="680" persistent>
    <v-card title="新增授权">
      <v-card-subtitle class="text-wrap pb-2">
        可同时选多个模型和多个部门、角色、成员，按「模型 × 对象」逐条生成。
        部门授权对其全部下级部门生效；已存在的条目会跳过，不改动原来的允许/拒绝。
      </v-card-subtitle>

      <v-card-text>
        <v-autocomplete
          v-model="modelIds"
          chips
          closable-chips
          density="comfortable"
          item-title="display_name"
          item-value="id"
          :items="models"
          label="模型（可多选）"
          multiple
          variant="outlined"
        />

        <v-switch
          v-model="selection.everyone"
          class="mt-1"
          color="primary"
          hide-details
          label="全员（勾选后忽略下面的部门、角色、成员）"
        />

        <template v-if="!selection.everyone">
          <v-autocomplete
            v-model="selection.deptIds"
            chips
            class="mt-3"
            closable-chips
            density="comfortable"
            :item-title="(d: Department) => departmentPath(d.id, departments)"
            item-value="id"
            :items="departments"
            label="部门（可多选，含下级部门）"
            multiple
            no-data-text="没有匹配项"
            variant="outlined"
          />

          <v-autocomplete
            v-model="selection.roles"
            chips
            class="mt-2"
            closable-chips
            density="comfortable"
            item-title="label"
            item-value="value"
            :items="roleOptions"
            label="角色（可多选）"
            multiple
            variant="outlined"
          />

          <MemberPicker
            v-model="selection.uids"
            class="mt-2"
            label="成员（可多选，搜索全量成员）"
            multiple
          />
        </template>

        <v-switch
          v-model="allowed"
          color="primary"
          hide-details
          :label="allowed ? '允许使用' : '显式拒绝（优先级高于所有允许规则，并对下级部门生效）'"
        />

        <v-text-field
          v-model="remark"
          class="mt-2"
          density="comfortable"
          label="备注"
          variant="outlined"
        />

        <v-alert
          v-if="rowCount > 0"
          density="compact"
          :text="`将为 ${modelIds.length} 个模型 × ${targets.length} 个对象生成 ${rowCount} 条授权`"
          type="info"
          variant="tonal"
        />
      </v-card-text>

      <v-card-actions>
        <v-spacer />
        <v-btn text="取消" @click="open = false" />

        <v-btn
          color="primary"
          :disabled="rowCount === 0"
          :loading="submitting"
          text="保存"
          variant="tonal"
          @click="submit"
        />
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<script setup lang="ts">
  import type { Department, ModelAdminInfo } from '@/types/api'
  import { computed, reactive, ref, watch } from 'vue'
  import { batchCreateWhitelist } from '@/api/catalog'
  import MemberPicker from '@/components/MemberPicker.vue'
  import { ASSIGNABLE_ROLES, roleLabel } from '@/constants/roles'
  import {
    batchSize,
    batchSummary,
    buildBatchTargets,
    departmentPath,
    emptySelection,
  } from '@/pages/whitelist-batch'
  import { useToastStore } from '@/stores/toast'

  const props = defineProps<{
    models: ModelAdminInfo[]
    departments: Department[]
    /** 打开时预选的模型（从模型列表「配置授权」跳过来） */
    presetModelId?: number | null
  }>()

  const emit = defineEmits<{ saved: [] }>()
  const open = defineModel<boolean>({ default: false })
  const toast = useToastStore()

  const modelIds = ref<number[]>([])
  const selection = reactive(emptySelection())
  const allowed = ref(true)
  const remark = ref('')
  const submitting = ref(false)

  const roleOptions = ASSIGNABLE_ROLES.map(value => ({ value, label: roleLabel(value) }))
  const targets = computed(() => buildBatchTargets(selection))
  const rowCount = computed(() => batchSize(modelIds.value, targets.value))

  watch(open, value => {
    if (!value) {
      return
    }
    modelIds.value = props.presetModelId ? [props.presetModelId] : []
    Object.assign(selection, emptySelection())
    allowed.value = true
    remark.value = ''
  })

  async function submit () {
    submitting.value = true
    try {
      const result = await batchCreateWhitelist({
        model_ids: modelIds.value,
        targets: targets.value,
        allowed: allowed.value,
        remark: remark.value || undefined,
      })
      toast.success(batchSummary(result))
      open.value = false
      emit('saved')
    } catch (error) {
      toast.error(error, '保存失败')
    } finally {
      submitting.value = false
    }
  }
</script>
