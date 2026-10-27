<template>
  <v-dialog v-model="open" max-width="1280" persistent scrollable>
    <v-card title="钉钉通讯录 · 选择本次处理事项">
      <v-card-text>
        <v-alert
          v-if="loading"
          aria-live="polite"
          class="mb-4"
          type="info"
          variant="tonal"
        >
          <div class="font-medium">正在读取钉钉通讯录 · 已等待 {{ elapsed }} 秒</div>
          <p class="mt-2">本次请求包含部门遍历和成员分页，返回后会显示差分。</p>
          <p v-if="elapsed >= 60" class="mt-2">仍在等待钉钉返回，部门较多时同步会更久。可先关闭窗口，稍后重新打开查看。</p>
          <v-progress-linear class="mt-3" indeterminate />
        </v-alert>

        <template v-if="preview">
          <v-alert class="mb-3" density="compact" :type="preview.complete ? 'success' : 'warning'" variant="tonal">
            {{ preview.complete ? '完整企业快照' : '部分快照，不能据此判断离职' }} · {{ preview.departments.length }} 个部门 · {{ memberCount }} 位成员 · 耗时 {{ elapsed }} 秒
            <div>同步时间：{{ new Date(preview.created_at).toLocaleString('zh-CN') }} · 权限范围：{{ preview.scope.departments?.includes('1') ? '全企业' : '局部授权' }}</div>
            <div v-if="!preview.complete && preview.last_success_at">上次完整同步：{{ new Date(preview.last_success_at).toLocaleString('zh-CN') }}</div>
            <div v-for="error in preview.errors" :key="error">{{ error }}</div>
          </v-alert>

          <p class="text-body-2 mb-3">只选择本次需要处理的账号，其余保留为“暂不处理”。姓名仅供核对；选好有效事项即可提交，不必处理全部差分。</p>

          <div class="flex flex-wrap items-center gap-3 mb-4">
            <v-btn
              color="primary"
              :disabled="busy || uniqueCandidates.length === 0 || credentials.length > 0"
              text="选入全部唯一候选"
              variant="tonal"
              @click="selectUniqueCandidates"
            />

            <v-btn :disabled="busy || plan.selected === 0" text="清除未提交选择" variant="text" @click="clearPending" />
            <span class="text-caption">可选 {{ uniqueCandidates.length }} 项；超过 100 项会自动分批，最后由你确认。</span>
          </div>

          <div class="flex flex-wrap gap-3 mb-3">
            <v-select
              v-model="category"
              class="min-w-52 flex-1"
              density="compact"
              hide-details
              item-title="label"
              item-value="value"
              :items="categories"
              label="差分类型"
              variant="outlined"
            />

            <v-select
              v-model="view"
              class="min-w-52 flex-1"
              density="compact"
              hide-details
              item-title="label"
              item-value="value"
              :items="views"
              label="处理状态"
              variant="outlined"
            />
          </div>

          <v-alert
            v-if="busy"
            aria-live="polite"
            class="mb-3"
            type="info"
            variant="tonal"
          >
            正在提交第 {{ batchIndex }} / {{ batchTotal }} 批 · 已收到 {{ processed }} / {{ submittedCount }} 项结果
            <v-progress-linear class="mt-2" :model-value="submittedCount ? processed / submittedCount * 100 : 0" />
          </v-alert>

          <v-data-table
            :headers="headers"
            item-value="key"
            :items="visibleRows"
            :items-per-page="10"
            no-data-text="没有此类记录"
          >
            <template #[`item.category`]="{ item }">{{ labels[item.category] || item.category }}</template>

            <template #[`item.person`]="{ item }">
              <div class="max-w-64 break-words">{{ item.member?.name || item.account?.name }}</div>
              <div class="text-caption text-medium-emphasis">{{ item.member?.user_id || item.account?.username }}</div>
              <div v-if="item.member" class="text-caption text-medium-emphasis max-w-64 truncate" :title="departmentNames(item.member.dept_ids)">{{ departmentSummary(item.member.dept_ids) }}</div>
            </template>

            <template #[`item.local`]="{ item }">
              <template v-if="item.account">{{ item.account.username }} · {{ item.account.status === 0 ? '已禁用' : '正常' }}</template>

              <template v-else-if="item.candidates.length > 0">
                <div v-for="candidate in item.candidates" :key="candidate.uid" class="text-caption">候选：{{ accountTitle(candidate) }}<span v-if="candidate.manageable === false" class="text-medium-emphasis"> · 需本人绑定</span></div>
              </template>

              <span v-else class="text-medium-emphasis">暂无候选，可暂不处理</span>
            </template>

            <template #[`item.action`]="{ item }">
              <span v-if="applied.has(item.key)" class="text-success">已完成</span>
              <span v-else-if="selfBindOnly(item)" class="text-medium-emphasis">需本人绑定</span>

              <v-select
                v-else-if="hasActions(item)"
                v-model="choices[item.key]!.action"
                density="compact"
                :disabled="busy"
                hide-details
                item-title="label"
                item-value="value"
                :items="actionsFor(item)"
                label="本次操作"
                placeholder="暂不处理"
                variant="outlined"
                @update:model-value="prefillCandidate(item)"
              />

              <span v-else class="text-medium-emphasis">无需操作</span>
            </template>

            <template #[`item.target`]="{ item }">
              <div v-if="selfBindOnly(item)" class="text-caption max-w-64">该账号需本人用原密码登录，在右上角账号菜单选择“绑定钉钉账号”。</div>

              <v-autocomplete
                v-else-if="choices[item.key]?.action === 'bind'"
                v-model="choices[item.key]!.uid"
                clearable
                density="compact"
                :disabled="busy || applied.has(item.key)"
                hide-details
                :item-title="accountTitle"
                item-value="uid"
                :items="preview.accounts"
                label="搜索姓名 / 账号"
                no-data-text="没有可绑定账号；高权限账号需本人绑定"
                variant="outlined"
              />

              <template v-else-if="choices[item.key]?.action === 'create'">
                <v-text-field
                  v-model="choices[item.key]!.username"
                  density="compact"
                  :disabled="busy || applied.has(item.key)"
                  hide-details
                  label="新用户名（花名）"
                  variant="outlined"
                />

                <v-select
                  v-model="choices[item.key]!.dept_id"
                  clearable
                  density="compact"
                  :disabled="busy || applied.has(item.key)"
                  hide-details
                  item-title="name"
                  item-value="id"
                  :items="mappedDepartments(item)"
                  label="映射部门（可留空）"
                  variant="outlined"
                />
              </template>

              <span v-else-if="choices[item.key]?.action === 'disable'" class="text-warning text-caption">确认离职后禁用，撤销全部会话和设备 Key。</span>
              <span v-else-if="!applied.has(item.key)" class="text-caption text-medium-emphasis">{{ noActionReason(item) }}</span>
            </template>

            <template #[`item.result`]="{ item }">
              <span v-if="plan.incomplete[item.key]" class="text-warning text-caption" role="status">待补全：{{ plan.incomplete[item.key] }}</span>
              <span v-else :class="outcomes[item.key]?.status === 'failed' ? 'text-error' : 'text-medium-emphasis'">{{ resultLabel(item.key) }}</span>
            </template>
          </v-data-table>
        </template>

        <v-alert v-if="message" class="mt-3" type="error" variant="tonal">{{ message }}</v-alert>

        <v-alert v-if="credentials.length > 0" class="mt-3" type="info" variant="tonal">
          已创建 {{ credentials.length }} 个账号。请下载一次性账号密码表；关闭后只能重置密码。首次密码登录需改密。
          <v-btn class="mt-2" :disabled="busy" text="下载本次已创建账号密码表" @click="downloadCredentials" />
        </v-alert>
      </v-card-text>

      <v-card-actions class="flex-wrap gap-2">
        <span class="text-caption">可提交 {{ plan.ready.length }} 项 · 待补全 {{ incompleteCount }} 项 · 暂不处理 {{ plan.skipped }} 项</span>
        <v-spacer />
        <v-btn :disabled="busy" text="关闭窗口" @click="close" />

        <v-btn
          color="primary"
          :disabled="plan.ready.length === 0 || loading || credentials.length > 0"
          :loading="busy"
          :text="`提交可执行的 ${plan.ready.length} 项`"
          @click="confirm = true"
        />
      </v-card-actions>
    </v-card>
  </v-dialog>

  <v-dialog v-model="confirm" max-width="520" persistent>
    <v-card title="核对本次提交">
      <v-card-text>
        将提交 {{ plan.ready.length }} 项：绑定 {{ plan.ready.filter(a => a.action === 'bind').length }}、创建 {{ plan.ready.filter(a => a.action === 'create').length }}、禁用 {{ plan.ready.filter(a => a.action === 'disable').length }}。
        <p class="mt-2">共 {{ Math.ceil(plan.ready.length / 100) }} 批，每批最多 100 项。{{ incompleteCount }} 项待补全和 {{ plan.skipped }} 项暂不处理记录不在本次提交中。</p>
        <p class="mt-2">请确认人员身份已核对。新账号默认普通员工；禁用会撤销全部会话和设备 Key。</p>
      </v-card-text>

      <v-card-actions><v-spacer /><v-btn :disabled="busy" text="返回核对" @click="confirm = false" /><v-btn color="primary" :loading="busy" text="确认提交" @click="submit" /></v-card-actions>
    </v-card>
  </v-dialog>
</template>

<script lang="ts" setup>
  import type { DirectoryAccount, DirectoryPreview, DirectoryRow, InitialCredential } from '@/api/directory'
  import type { Department } from '@/types/api'
  import { computed, onBeforeUnmount, reactive, ref } from 'vue'
  import { applyDirectory, auditCredentialsExport, credentialsCsv, previewDirectory } from '@/api/directory'
  import { directoryBatches, type DirectoryChoice, planDirectoryActions, uniqueCandidateBindings } from './directory-matching'

  const props = defineProps<{ departments: Department[] }>()
  const emit = defineEmits<{ changed: [] }>()
  const open = ref(false), loading = ref(false), busy = ref(false), confirm = ref(false)
  const message = ref(''), category = ref('all'), view = ref('review')
  const preview = ref<DirectoryPreview | null>(null), credentials = ref<InitialCredential[]>([])
  const choices = reactive<Record<string, DirectoryChoice>>({})
  const outcomes = reactive<Record<string, { status: string, message?: string }>>({})
  const applied = reactive(new Set<string>())
  const elapsed = ref(0), batchIndex = ref(0), batchTotal = ref(0), processed = ref(0), submittedCount = ref(0)
  let timer: ReturnType<typeof setInterval> | undefined
  let generation = 0
  const labels: Record<string, string> = { bound: '已绑定', candidate: '疑似存量账号', ambiguous: '重名 · 待核对', new: '钉钉有、系统无', missing: '绑定账号未见', conflict: '标识冲突', external: '企业外账号', system_only: '系统账号未绑定' }
  const categories = [{ value: 'all', label: '全部差分' }, ...Object.entries(labels).map(([value, label]) => ({ value, label }))]
  const views = [{ value: 'review', label: '待核对（含需本人绑定）' }, { value: 'all', label: '全部记录' }, { value: 'ready', label: '可提交' }, { value: 'incomplete', label: '待补全' }, { value: 'skipped', label: '暂不处理 / 无需操作' }, { value: 'done', label: '已完成' }]
  const headers = [{ title: '差分', key: 'category', width: 110 }, { title: '成员 / 部门', key: 'person', width: 230 }, { title: '本地账号', key: 'local', width: 170 }, { title: '本次操作', key: 'action', width: 180 }, { title: '核对目标', key: 'target', width: 260 }, { title: '处理状态', key: 'result', width: 180 }]
  const plan = computed(() => planDirectoryActions(preview.value?.rows ?? [], preview.value?.accounts ?? [], choices, applied))
  const incompleteCount = computed(() => Object.keys(plan.value.incomplete).length)
  const memberCount = computed(() => preview.value?.rows.filter(row => row.member).length ?? 0)
  const uniqueCandidates = computed(() => uniqueCandidateBindings(preview.value?.rows ?? [], preview.value?.accounts ?? [], choices, applied))
  const visibleRows = computed(() => preview.value?.rows.filter(row => {
    if (category.value !== 'all' && row.category !== category.value) return false
    if (view.value === 'review') return !applied.has(row.key) && (hasActions(row) || selfBindOnly(row))
    if (view.value === 'done') return applied.has(row.key)
    if (view.value === 'ready') return plan.value.ready.some(action => action.key === row.key)
    if (view.value === 'incomplete') return Boolean(plan.value.incomplete[row.key])
    if (view.value === 'skipped') return !applied.has(row.key) && !choices[row.key]?.action
    return true
  }) ?? [])
  function selfBindOnly (row: DirectoryRow) {
    return row.candidates.length > 0 && row.candidates.every(account => account.manageable === false && account.status !== 0 && !account.bound && account.account_kind !== 'external')
  }
  function hasActions (row: DirectoryRow) {
    return row.can_bind || row.can_create || row.can_disable
  }
  function selectUniqueCandidates () {
    for (const action of uniqueCandidates.value) choices[action.key] = { ...choices[action.key], action: 'bind', uid: action.uid }
  }
  function clearPending () {
    for (const key of Object.keys(choices)) if (!applied.has(key)) choices[key]!.action = null
  }
  function prefillCandidate (row: DirectoryRow) {
    const choice = choices[row.key]
    if (!choice || choice.action !== 'bind' || choice.uid || !preview.value) return
    const suggestion = uniqueCandidateBindings(preview.value.rows, preview.value.accounts, { ...choices, [row.key]: { ...choice, action: null } }, applied).find(action => action.key === row.key)
    if (suggestion) choice.uid = suggestion.uid
  }
  function actionsFor (row: DirectoryRow) {
    return [{ value: null, label: '暂不处理' }, ...[row.can_bind && { value: 'bind', label: '绑定已有账号' }, row.can_create && { value: 'create', label: '创建员工账号' }, row.can_disable && { value: 'disable', label: '禁用离职账号' }].filter(Boolean)]
  }
  const accountTitle = (account: DirectoryAccount) => account.name === account.username ? account.username : `${account.name} / ${account.username}`
  function departmentNames (ids: string[]) {
    return ids.map(id => preview.value?.departments.find(d => d.id === id)?.name || id).join('、')
  }
  function departmentSummary (ids: string[]) {
    return departmentNames(ids.slice(0, 2)) + (ids.length > 2 ? ` 等 ${ids.length} 个部门` : '')
  }
  function mappedDepartments (row: DirectoryRow) {
    return props.departments.filter(d => row.mapped_dept_ids.includes(d.id))
  }
  function noActionReason (row: DirectoryRow) {
    if (row.category === 'bound') return '身份已绑定'
    if (row.category === 'conflict') return '请先核对平台身份标识'
    if (row.category === 'missing' && !row.can_disable) return '等待连续两次完整名册核对'
    if (row.category === 'system_only') return '本次无需操作'
    return '暂不处理'
  }
  function resultLabel (key: string) {
    const result = outcomes[key]
    return result?.message || ({ success: '已完成', already_applied: '已执行', failed: '失败，可核对后重试' }[result?.status || ''] ?? (choices[key]?.action ? '已选入，待提交' : '暂不处理'))
  }
  async function start () {
    open.value = true
    if (loading.value || busy.value) return
    const run = ++generation
    loading.value = true
    message.value = ''
    preview.value = null
    elapsed.value = 0
    applied.clear()
    for (const key of Object.keys(choices)) delete choices[key]
    for (const key of Object.keys(outcomes)) delete outcomes[key]
    const began = Date.now()
    timer = setInterval(() => {
      elapsed.value = Math.floor((Date.now() - began) / 1000)
    }, 1000)
    try {
      const data = await previewDirectory()
      if (run !== generation) return
      preview.value = data
      for (const row of data.rows) choices[row.key] = { action: null, dept_id: null, username: '' }
    } catch (error) {
      if (run === generation) message.value = error instanceof Error ? error.message : '同步失败，请重试'
    } finally {
      if (run === generation) {
        elapsed.value = Math.floor((Date.now() - began) / 1000)
        loading.value = false
        clearInterval(timer)
      }
    }
  }
  async function submit () {
    if (!preview.value || busy.value) return
    const actions = [...plan.value.ready]
    if (actions.length === 0) {
      confirm.value = false
      return
    }
    const batches = directoryBatches(actions)
    busy.value = true
    confirm.value = false
    message.value = ''
    batchTotal.value = batches.length
    processed.value = 0
    submittedCount.value = actions.length
    try {
      for (const [index, batch] of batches.entries()) {
        batchIndex.value = index + 1
        const result = await applyDirectory(preview.value.snapshot_id, batch)
        credentials.value.push(...result.credentials)
        for (const row of result.results) {
          outcomes[row.key] = row
          if (row.status !== 'failed') applied.add(row.key)
        }
        processed.value += result.results.length
      }
    } catch (error) {
      message.value = (error instanceof Error ? error.message : '提交失败') + '；已返回的成功结果及新账号密码已保留，未完成项可核对后重试。'
    } finally {
      busy.value = false
      if (applied.size > 0) emit('changed')
    }
  }
  async function downloadCredentials () {
    if (!preview.value || busy.value) return
    busy.value = true
    try {
      for (const batch of directoryBatches(credentials.value.map(row => row.uid))) await auditCredentialsExport(preview.value.snapshot_id, batch)
      const url = URL.createObjectURL(new Blob([credentialsCsv(credentials.value)], { type: 'text/csv;charset=utf-8' }))
      const link = document.createElement('a')
      link.href = url
      link.download = `账号初始密码-${preview.value.snapshot_id}.csv`
      link.click()
      credentials.value = []
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (error) {
      message.value = error instanceof Error ? error.message : '导出失败，请重试'
    } finally {
      busy.value = false
    }
  }
  function close () {
    credentials.value = []
    confirm.value = false
    open.value = false
  }
  onBeforeUnmount(() => {
    generation++
    clearInterval(timer)
  })
  defineExpose({ start })
</script>
