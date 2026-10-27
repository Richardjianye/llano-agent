<template>
  <v-autocomplete
    v-model="selection"
    v-model:search="search"
    :chips="multiple"
    :closable-chips="multiple"
    density="comfortable"
    hide-no-data
    :item-title="memberLabel"
    item-value="uid"
    :items="options"
    :label="label"
    :loading="loading"
    :multiple="multiple"
    no-filter
    :placeholder="placeholder"
    variant="outlined"
  >
    <template #item="{ props: itemProps, item }">
      <v-list-item v-bind="itemProps" :subtitle="item.dept_path || '未分配部门'">
        <template #append>
          <v-chip v-if="item.status !== 1" color="grey" size="x-small" text="已停用" />
          <span v-else class="text-caption text-medium-emphasis">{{ roleLabel(item.role) }}</span>
        </template>
      </v-list-item>
    </template>

    <template #append-item>
      <div v-if="total > results.length" class="px-4 py-2 text-caption text-medium-emphasis">
        共 {{ total }} 人匹配，仅显示前 {{ results.length }} 个，请输入更多关键字
      </div>
    </template>
  </v-autocomplete>
</template>

<script setup lang="ts">
  /**
   * 选人控件 —— 服务端全量搜索（/admin/member/search），支持单选/多选。
   *
   * v-model 是 uid（单选）或 uid 数组（多选）；选中成员的完整信息通过 `members`
   * 事件给到父组件，用来展示「已选 N 人：张三、李四」之类的摘要。
   */
  import type { MemberHit } from '@/api/members'
  import { computed, ref, watch } from 'vue'
  import { searchMembers } from '@/api/members'
  import { memberLabel, mergeMemberOptions, missingMemberUids } from '@/components/member-picker'
  import { roleLabel } from '@/constants/roles'

  const props = withDefaults(defineProps<{
    multiple?: boolean
    label?: string
    placeholder?: string
    /** 包含已停用账号（白名单配置不需要；排查时可能需要） */
    includeDisabled?: boolean
  }>(), {
    multiple: false,
    label: '成员',
    placeholder: '输入姓名或账号搜索',
    includeDisabled: false,
  })

  const selection = defineModel<string | string[] | null>({ default: null })
  const emit = defineEmits<{ members: [members: MemberHit[]] }>()

  const search = ref('')
  const loading = ref(false)
  const results = ref<MemberHit[]>([])
  const total = ref(0)
  const known = ref<MemberHit[]>([])

  const selectedUids = computed(() => {
    if (Array.isArray(selection.value)) {
      return selection.value
    }
    return selection.value ? [selection.value] : []
  })

  const selectedMembers = computed(() =>
    selectedUids.value
      .map(uid => known.value.find(m => m.uid === uid))
      .filter((m): m is MemberHit => m !== undefined),
  )

  const options = computed(() => mergeMemberOptions(selectedMembers.value, results.value))

  function remember (members: MemberHit[]) {
    known.value = mergeMemberOptions(known.value, members)
  }

  let timer: ReturnType<typeof setTimeout> | undefined
  let requestSeq = 0

  async function runSearch (keyword: string) {
    const seq = ++requestSeq
    loading.value = true
    try {
      const page = await searchMembers({
        keyword,
        limit: 50,
        status: props.includeDisabled ? 'all' : 'active',
      })
      // 只认最后一次请求的结果，避免慢请求覆盖快请求
      if (seq === requestSeq) {
        results.value = page.items
        total.value = page.total
        remember(page.items)
      }
    } catch {
      if (seq === requestSeq) {
        results.value = []
        total.value = 0
      }
    } finally {
      if (seq === requestSeq) {
        loading.value = false
      }
    }
  }

  watch(search, keyword => {
    clearTimeout(timer)
    timer = setTimeout(() => void runSearch(keyword ?? ''), 250)
  }, { immediate: true })

  // 外部传进来的已选 uid（比如编辑时回显）不在 known 里时，按 uid 回查一次名字
  watch(selectedUids, async uids => {
    const missing = missingMemberUids(uids, known.value)
    if (missing.length > 0) {
      try {
        const page = await searchMembers({ uids: missing, status: 'all', limit: 100 })
        remember(page.items)
      } catch {
        // 回查失败只影响 chip 上的名字，不影响选择本身
      }
    }
  }, { immediate: true })

  watch(selectedMembers, members => emit('members', members), { immediate: true })
</script>
