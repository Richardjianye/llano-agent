<template>
  <v-dialog v-model="open" max-width="640">
    <v-card title="按成员查看可用模型">
      <v-card-text>
        <MemberPicker v-model="uid" label="选择成员" />

        <div v-if="report" class="text-body-2 text-medium-emphasis mb-2">
          {{ report.member.name }} · {{ roleLabel(report.member.role) }}
          · {{ report.member.dept_path || '未分配部门' }}
        </div>

        <v-list v-if="report && report.items.length > 0" density="compact">
          <v-list-item v-for="item in report.items" :key="item.model_id">
            <template #prepend>
              <v-icon
                :color="item.allowed ? 'success' : 'error'"
                :icon="item.allowed ? 'mdi-check-circle-outline' : 'mdi-close-circle-outline'"
              />
            </template>

            <v-list-item-title>
              {{ item.display_name }}
              <v-chip
                v-if="item.model_status !== 1"
                class="ml-1"
                color="grey"
                size="x-small"
                text="模型已停用"
              />
            </v-list-item-title>

            <v-list-item-subtitle class="text-wrap">
              <span v-if="item.deny_sources.length > 0" class="text-error">
                被拒绝：{{ item.deny_sources.map(describe).join('；') }}
              </span>

              <span v-if="item.allow_sources.length > 0" :class="item.deny_sources.length > 0 ? 'ml-2' : ''">
                授权来自：{{ item.allow_sources.map(describe).join('；') }}
              </span>
            </v-list-item-subtitle>
          </v-list-item>
        </v-list>

        <v-alert
          v-if="report && report.items.every(i => !i.allowed)"
          class="mt-2"
          density="compact"
          text="该成员当前没有任何可用模型"
          type="warning"
          variant="tonal"
        />

        <v-progress-linear v-if="loading" indeterminate />
      </v-card-text>

      <v-card-actions>
        <v-spacer />
        <v-btn text="关闭" @click="open = false" />
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<script setup lang="ts">
  /**
   * 按成员反查 —— 与网关真实下发同一套判定（含上级部门继承、显式拒绝）。
   * 停用的模型也列出来并标注，便于看清「授权了但模型关着」。
   */
  import type { AccessSource, MemberAccessReport } from '@/api/catalog'
  import { ref, watch } from 'vue'
  import { memberModelAccess } from '@/api/catalog'
  import MemberPicker from '@/components/MemberPicker.vue'
  import { roleLabel } from '@/constants/roles'
  import { sourceText } from '@/pages/whitelist-batch'
  import { useToastStore } from '@/stores/toast'
  import { SCOPE_LABELS } from '@/types/api'

  const open = defineModel<boolean>({ default: false })
  const toast = useToastStore()

  const uid = ref<string | null>(null)
  const report = ref<MemberAccessReport | null>(null)
  const loading = ref(false)

  function describe (source: AccessSource): string {
    return sourceText(source, SCOPE_LABELS, roleLabel)
  }

  watch(uid, async value => {
    report.value = null
    if (!value) {
      return
    }
    loading.value = true
    try {
      const result = await memberModelAccess(value)
      // 选择已经换人了就丢掉旧结果
      if (uid.value === value) {
        report.value = result
      }
    } catch (error) {
      toast.error(error, '查询失败')
    } finally {
      loading.value = false
    }
  })
</script>
