<template>
  <div>
    <div class="flex items-center gap-3 px-4 py-3">
      <v-text-field
        v-model="keyword"
        clearable
        density="compact"
        hide-details
        label="按模型或授权对象过滤"
        prepend-inner-icon="mdi-magnify"
        style="max-width: 320px"
        variant="outlined"
      />

      <span class="text-caption text-medium-emphasis">
        「实际可用」已算上部门继承与显式拒绝，在职共 {{ activeUserCount }} 人
      </span>

      <v-spacer />
      <v-btn icon="mdi-refresh" :loading="loading" variant="text" @click="load" />
    </div>

    <v-data-table
      density="comfortable"
      :headers="headers"
      item-value="model_id"
      :items="visible"
      :items-per-page="25"
      :loading="loading"
      no-data-text="没有模型"
    >
      <template #[`item.display_name`]="{ item }">
        <div class="font-weight-medium">{{ item.display_name }}</div>

        <div class="text-caption text-medium-emphasis">
          {{ item.public_name }}
          <v-chip
            v-if="item.status !== 1"
            class="ml-1"
            color="grey"
            size="x-small"
            text="已停用"
          />
        </div>
      </template>

      <template #[`item.grants`]="{ item }">
        <div class="flex flex-wrap gap-1 py-2">
          <v-chip
            v-for="g in item.grants.global"
            :key="`g-${g.scope_value}`"
            :color="g.allowed ? 'primary' : 'error'"
            label
            size="small"
            :text="g.allowed ? '全员' : '全员（拒绝）'"
          />

          <v-chip
            v-for="g in item.grants.dept"
            :key="`d-${g.scope_value}`"
            :color="g.allowed ? 'default' : 'error'"
            label
            prepend-icon="mdi-sitemap-outline"
            size="small"
            :text="deptChipText(g)"
          />

          <v-chip
            v-for="g in item.grants.role"
            :key="`r-${g.scope_value}`"
            :color="g.allowed ? 'default' : 'error'"
            label
            prepend-icon="mdi-shield-account-outline"
            size="small"
            :text="`${roleLabel(g.scope_value)}${g.allowed ? '' : '（拒绝）'}`"
          />

          <v-chip
            v-for="g in item.grants.user"
            :key="`u-${g.scope_value}`"
            :color="g.allowed ? 'default' : 'error'"
            label
            prepend-icon="mdi-account-outline"
            size="small"
            :text="`${g.label}${g.allowed ? '' : '（拒绝）'}`"
          />

          <span v-if="item.rule_count === 0" class="text-caption text-medium-emphasis">未授权</span>
        </div>
      </template>

      <template #[`item.effective_user_count`]="{ item }">
        <span :class="item.effective_user_count === 0 ? 'text-medium-emphasis' : ''">
          {{ item.effective_user_count }} 人
        </span>
      </template>

      <template #[`item.actions`]="{ item }">
        <v-btn
          v-perm="'model:edit'"
          size="small"
          text="追加授权"
          variant="text"
          @click="emit('grant', item.model_id)"
        />
      </template>
    </v-data-table>
  </div>
</template>

<script setup lang="ts">
  /** 按模型看授权：开放给了哪些部门/角色/成员，实际多少人能用 */
  import type { GrantItem, ModelGrantOverview } from '@/api/catalog'
  import { computed, onMounted, ref } from 'vue'
  import { whitelistOverview } from '@/api/catalog'
  import { roleLabel } from '@/constants/roles'
  import { filterOverview } from '@/pages/whitelist-batch'
  import { useToastStore } from '@/stores/toast'

  const emit = defineEmits<{ grant: [modelId: number] }>()
  const toast = useToastStore()

  const items = ref<ModelGrantOverview[]>([])
  const activeUserCount = ref(0)
  const loading = ref(false)
  const keyword = ref('')
  const visible = computed(() => filterOverview(items.value, keyword.value ?? ''))

  const headers = [
    { title: '模型', key: 'display_name', width: 220 },
    { title: '开放给', key: 'grants', sortable: false },
    { title: '实际可用', key: 'effective_user_count', width: 110 },
    { title: '操作', key: 'actions', sortable: false, align: 'end' as const, width: 120 },
  ]

  function deptChipText (grant: GrantItem): string {
    const sub = grant.descendant_dept_count ? `（含 ${grant.descendant_dept_count} 个下级）` : ''
    return `${grant.label}${sub}${grant.allowed ? '' : '（拒绝）'}`
  }

  async function load () {
    loading.value = true
    try {
      const result = await whitelistOverview()
      items.value = result.items
      activeUserCount.value = result.active_user_count
    } catch (error) {
      toast.error(error, '加载授权概览失败')
    } finally {
      loading.value = false
    }
  }

  defineExpose({ load })
  onMounted(load)
</script>
