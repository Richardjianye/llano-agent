<template>
  <v-card border flat rounded="lg">
    <v-card-item>
      <v-card-title class="text-subtitle-1">用户与角色</v-card-title>

      <v-card-subtitle>
        你的角色是「{{ roleLabel(auth.user?.permission) }}」，只能管理级别低于自己的账号（开发者之间可互相管理，总裁账号不可被管理）
      </v-card-subtitle>

      <template #append>
        <v-btn
          v-perm="'user:edit'"
          class="mr-2"
          prepend-icon="mdi-sync"
          text="同步钉钉通讯录"
          variant="tonal"
          @click="directoryDialog?.start()"
        />

        <v-btn
          v-perm="'user:edit'"
          color="primary"
          prepend-icon="mdi-account-plus-outline"
          variant="tonal"
          @click="openCreate"
        >
          新建账号
        </v-btn>
      </template>
    </v-card-item>

    <v-divider />

    <v-card-text>
      <v-row dense>
        <v-col cols="12" md="4" sm="6">
          <v-text-field
            v-model="filters.keyword"
            clearable
            density="compact"
            hide-details
            label="搜索用户名 / 姓名"
            prepend-inner-icon="mdi-magnify"
            variant="outlined"
            @keyup.enter="applyFilters"
          />
        </v-col>

        <v-col cols="12" md="3" sm="6">
          <v-autocomplete
            v-model="filters.dept_id"
            clearable
            density="compact"
            hide-details
            item-title="name"
            item-value="id"
            :items="departments"
            label="部门"
            no-data-text="没有匹配项"
            variant="outlined"
            @update:model-value="applyFilters"
          />
        </v-col>

        <v-col cols="12" md="3" sm="6">
          <v-autocomplete
            v-model="filters.role"
            clearable
            density="compact"
            hide-details
            item-title="label"
            item-value="value"
            :items="roleOptions"
            label="角色"
            no-data-text="没有匹配项"
            variant="outlined"
            @update:model-value="applyFilters"
          />
        </v-col>

        <v-col cols="12" md="2" sm="6">
          <v-btn block height="40" variant="tonal" @click="applyFilters">查询</v-btn>
        </v-col>
      </v-row>
    </v-card-text>

    <v-data-table-server
      density="comfortable"
      :headers="headers"
      hover
      item-value="uid"
      :items="accounts"
      :items-length="totalAccounts"
      :items-per-page="pageSize"
      :loading="loading"
      no-data-text="没有符合条件的账号"
      :page="page"
      :sort-by="sortBy"
      @update:options="onTableOptions"
    >
      <template #[`item.realname`]="{ item }">
        <div class="font-weight-medium">{{ item.realname || item.username }} {{ item.username != (item.realname || item.username) ? ` · `+ item.username : '' }}</div>
      </template>

      <template #[`item.permission`]="{ item }">
        <v-chip
          :color="roleColor(item.permission)"
          label
          size="small"
          :text="roleLabel(item.permission)"
        />
      </template>

      <template #[`item.dept_id`]="{ item }">
        {{ deptName(item.dept_id) }}
      </template>

      <template #[`item.status`]="{ item }">
        <v-chip
          :color="item.status === 1 ? 'success' : 'grey'"
          size="small"
          :text="item.status === 1 ? '正常' : '已禁用'"
          variant="tonal"
        />
      </template>

      <template #[`item.last_login_at`]="{ item }">
        <span class="text-caption text-medium-emphasis">
          {{ formatTime(item.last_login_at) }}
        </span>
      </template>

      <template #[`item.last_request_at`]="{ item }">
        <span class="text-caption text-medium-emphasis">
          {{ formatRequestTime(item.last_request_at) }}
        </span>
      </template>

      <template #[`item.actions`]="{ item }">
        <!-- manageable 由后端按角色级别算好；这里只是不显示，后端仍会拦 -->
        <template v-if="item.manageable">
          <v-btn size="small" text="调整" variant="text" @click="openAssign(item)" />

          <v-btn
            size="small"
            text="重置密码"
            variant="text"
            @click="confirmReset(item)"
          />

          <v-btn
            v-if="item.status === 1"
            color="warning"
            size="small"
            text="禁用"
            variant="text"
            @click="confirmStatus(item, 0)"
          />

          <v-btn
            v-else
            color="success"
            size="small"
            text="启用"
            variant="text"
            @click="confirmStatus(item, 1)"
          />
        </template>

        <span v-else class="text-caption text-medium-emphasis">
          {{ item.uid === auth.user?.uid ? '本人' : '级别不低于你' }}
        </span>
      </template>
    </v-data-table-server>
  </v-card>

  <DirectorySyncDialog ref="directoryDialog" :departments="departments" @changed="load" />

  <!-- 新建账号 -->
  <v-dialog v-model="createDialog" max-width="560" persistent>
    <v-card title="新建账号">
      <v-card-text>
        <v-form ref="createFormRef" @submit.prevent="submitCreate">
          <v-text-field
            v-model="createForm.username"
            density="comfortable"
            hint="登录用户名，用中文花名，如「admin」"
            label="用户名"
            persistent-hint
            :rules="[required, noSpace]"
            variant="outlined"
          />

          <v-text-field
            v-model="createForm.realname"
            class="mt-4"
            density="comfortable"
            label="真实姓名（可空）"
            variant="outlined"
          />

          <v-autocomplete
            v-model="createForm.permission"
            class="mt-2"
            density="comfortable"
            item-title="label"
            item-value="value"
            :items="assignableOptions"
            label="角色"
            no-data-text="没有匹配项"
            :rules="[required]"
            variant="outlined"
          />

          <v-autocomplete
            v-model="createForm.dept_id"
            class="mt-2"
            clearable
            density="comfortable"
            item-title="name"
            item-value="id"
            :items="departments"
            label="部门（可空）"
            no-data-text="没有匹配项"
            variant="outlined"
          />

          <v-alert
            class="mt-2"
            density="compact"
            text="初始密码留空则由系统随机生成，创建后只显示一次"
            type="info"
            variant="tonal"
          />
        </v-form>
      </v-card-text>

      <v-card-actions>
        <v-spacer />

        <v-btn text="取消" @click="createDialog = false" />

        <v-btn
          color="primary"
          :loading="submitting"
          text="创建"
          variant="tonal"
          @click="submitCreate"
        />
      </v-card-actions>
    </v-card>
  </v-dialog>

  <!-- 调整部门 / 角色 -->
  <v-dialog v-model="assignDialog" max-width="520" persistent>
    <v-card :title="`调整 ${current?.realname || current?.username || ''}`">
      <v-card-text>
        <v-autocomplete
          v-model="assignForm.permission"
          density="comfortable"
          item-title="label"
          item-value="value"
          :items="assignableOptions"
          label="角色"
          no-data-text="没有匹配项"
          variant="outlined"
        />

        <v-autocomplete
          v-model="assignForm.dept_id"
          class="mt-2"
          clearable
          density="comfortable"
          item-title="name"
          item-value="id"
          :items="departments"
          label="部门"
          no-data-text="没有匹配项"
          variant="outlined"
        />

        <v-alert
          class="mt-2"
          density="compact"
          text="角色或部门发生变化时，对方现有后台登录会话将立即失效，需重新登录"
          type="info"
          variant="tonal"
        />
      </v-card-text>

      <v-card-actions>
        <v-spacer />
        <v-btn text="取消" @click="assignDialog = false" />

        <v-btn
          color="primary"
          :loading="submitting"
          text="保存"
          variant="tonal"
          @click="submitAssign"
        />
      </v-card-actions>
    </v-card>
  </v-dialog>

  <!-- 二次确认 -->
  <v-dialog v-model="confirmDialog" max-width="460">
    <v-card :title="confirmTitle">
      <v-card-text>{{ confirmText }}</v-card-text>

      <v-card-actions>
        <v-spacer />
        <v-btn text="取消" @click="confirmDialog = false" />

        <v-btn
          :color="confirmColor"
          :loading="submitting"
          text="确认"
          variant="tonal"
          @click="runConfirm"
        />
      </v-card-actions>
    </v-card>
  </v-dialog>

  <!-- 明文密码只显示这一次 -->
  <v-dialog v-model="passwordDialog" max-width="460" persistent>
    <v-card title="初始密码">
      <v-card-text>
        <p class="mb-3 text-body-2">
          请当面交给本人，关闭后无法再次查看（库里只存哈希）。
        </p>

        <v-text-field
          density="comfortable"
          :model-value="generatedPassword"
          readonly
          variant="outlined"
        />
      </v-card-text>

      <v-card-actions>
        <v-spacer />

        <v-btn
          color="primary"
          text="我已记录"
          variant="tonal"
          @click="passwordDialog = false"
        />
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<script lang="ts" setup>
  import type { Account, Department } from '@/types/api'
  import { computed, onMounted, reactive, ref } from 'vue'
  import * as accountApi from '@/api/account'
  import { listDepartments } from '@/api/department'
  import DirectorySyncDialog from '@/components/DirectorySyncDialog.vue'
  import { ASSIGNABLE_ROLES, assignableRoles, roleColor, roleLabel } from '@/constants/roles'
  import { useAuthStore } from '@/stores/auth'
  import { useToastStore } from '@/stores/toast'

  const directoryDialog = ref<InstanceType<typeof DirectorySyncDialog> | null>(null)
  const auth = useAuthStore()
  const toast = useToastStore()

  const headers = [
    { title: '姓名 / 用户名', key: 'realname' },
    { title: '角色', key: 'permission', width: 130 },
    { title: '部门', key: 'dept_id', width: 160 },
    { title: '状态', key: 'status', width: 100 },
    { title: '最后登录', key: 'last_login_at', width: 160 },
    { title: '最近请求', key: 'last_request_at', width: 160 },
    { title: '操作', key: 'actions', sortable: false, align: 'end' as const, width: 280 },
  ]

  const accounts = ref<Account[]>([])
  const totalAccounts = ref(0)
  const page = ref(1)
  const pageSize = ref(20)
  const sortBy = ref<Array<{ key: string, order: 'asc' | 'desc' }>>([])
  const departments = ref<Department[]>([])
  const loading = ref(false)
  const submitting = ref(false)
  let loadSequence = 0

  const filters = reactive({
    keyword: '',
    dept_id: null as number | null,
    role: null as string | null,
  })

  const createDialog = ref(false)
  const assignDialog = ref(false)
  const confirmDialog = ref(false)
  const passwordDialog = ref(false)

  const current = ref<Account | null>(null)
  const generatedPassword = ref('')
  const confirmTitle = ref('')
  const confirmText = ref('')
  const confirmColor = ref('primary')
  let confirmAction: (() => Promise<void>) | null = null

  const createFormRef = ref<{ validate: () => Promise<{ valid: boolean }> } | null>(null)
  const createForm = reactive({
    username: '',
    realname: '',
    permission: '',
    dept_id: null as number | null,
  })
  const assignForm = reactive({
    permission: '' as string | undefined,
    dept_id: null as number | null,
  })

  const required = (v: string) => (v && v.trim() !== '') || '必填'
  const noSpace = (v: string) => !/\s/.test(v ?? '') || '不能包含空格'

  const roleOptions = computed(() =>
    ASSIGNABLE_ROLES.map(value => ({ value, label: roleLabel(value) })),
  )

  /** 能分配的角色由自己的级别决定，总裁才能造开发者 */
  const assignableOptions = computed(() =>
    assignableRoles(auth.user?.permission).map(value => ({
      value,
      label: roleLabel(value),
    })),
  )

  function deptName (id: number | null): string {
    if (id === null) {
      return '未分配'
    }
    return departments.value.find(d => d.id === id)?.name ?? `#${id}`
  }

  function formatTime (value: string | null): string {
    if (!value) {
      return '从未登录'
    }
    return new Date(value).toLocaleString('zh-CN', { hour12: false })
  }

  function formatRequestTime (value: string | null): string {
    return value ? formatTime(value) : '尚未使用'
  }

  function applyFilters () {
    page.value = 1
    void load()
  }

  function onTableOptions (options: {
    page: number
    itemsPerPage: number
    sortBy: Array<{ key: string, order?: boolean | 'asc' | 'desc' }>
  }) {
    page.value = options.page
    pageSize.value = options.itemsPerPage
    sortBy.value = options.sortBy.map(item => ({
      key: item.key,
      order: item.order === 'desc' || item.order === false ? 'desc' : 'asc',
    }))
    void load()
  }

  async function load () {
    const sequence = ++loadSequence
    loading.value = true
    try {
      const result = await accountApi.listAccounts({
        keyword: filters.keyword || undefined,
        dept_id: filters.dept_id ?? undefined,
        role: filters.role ?? undefined,
        limit: pageSize.value,
        offset: (page.value - 1) * pageSize.value,
        sort_by: sortBy.value[0]?.key,
        sort_order: sortBy.value[0]?.order,
      })
      if (sequence === loadSequence) {
        accounts.value = result.items
        totalAccounts.value = result.total
      }
    } catch (error) {
      if (sequence === loadSequence) {
        toast.error(error, '加载账号失败')
      }
    } finally {
      if (sequence === loadSequence) {
        loading.value = false
      }
    }
  }

  async function loadDepartments () {
    try {
      const result = await listDepartments(true)
      departments.value = result.items
    } catch (error) {
      toast.error(error, '加载部门失败')
    }
  }

  function openCreate () {
    Object.assign(createForm, {
      username: '',
      realname: '',
      permission: assignableOptions.value.at(-1)?.value ?? '',
      dept_id: null,
    })
    createDialog.value = true
  }

  async function submitCreate () {
    const result = await createFormRef.value?.validate()
    if (!result?.valid) {
      return
    }

    submitting.value = true
    try {
      const created = await accountApi.createAccount({
        username: createForm.username.trim(),
        realname: createForm.realname || undefined,
        permission: createForm.permission,
        dept_id: createForm.dept_id,
      })
      createDialog.value = false
      toast.success('账号已创建')

      if (created.initial_password) {
        generatedPassword.value = created.initial_password
        passwordDialog.value = true
      }
      await load()
    } catch (error) {
      toast.error(error, '创建失败')
    } finally {
      submitting.value = false
    }
  }

  function openAssign (account: Account) {
    current.value = account
    assignForm.permission = account.permission
    assignForm.dept_id = account.dept_id
    assignDialog.value = true
  }

  async function submitAssign () {
    if (!current.value) {
      return
    }

    const authorizationChanged = current.value.permission !== assignForm.permission
      || current.value.dept_id !== assignForm.dept_id
    submitting.value = true
    try {
      await accountApi.assignAccount({
        uid: current.value.uid,
        permission: assignForm.permission,
        dept_id: assignForm.dept_id,
      })
      assignDialog.value = false
      toast.success(authorizationChanged
        ? '已调整，对方现有登录会话已失效，需重新登录'
        : '已保存，角色与部门未发生变化')
      await load()
    } catch (error) {
      toast.error(error, '调整失败')
    } finally {
      submitting.value = false
    }
  }

  function confirmStatus (account: Account, status: number) {
    current.value = account
    const name = account.realname || account.username
    confirmTitle.value = status === 1 ? '启用账号' : '禁用账号'
    confirmText.value
      = status === 1
        ? `确认启用「${name}」？账号将可以重新登录，但此前已吊销的登录会话和 API 密钥不会恢复；本人需重新登录，已有客户端需重新签发密钥。`
        : `确认禁用「${name}」（${roleLabel(account.permission)}）？该账号将立即无法登录，全部登录会话和 API 密钥都会被永久吊销；以后重新启用也不会自动恢复。`
    confirmColor.value = status === 1 ? 'success' : 'warning'
    confirmAction = async () => {
      await accountApi.setAccountStatus(account.uid, status)
      toast.success(status === 1
        ? '账号已启用；请让本人重新登录，已有客户端需重新签发密钥'
        : '账号已禁用，登录会话和 API 密钥已吊销')
      await load()
    }
    confirmDialog.value = true
  }

  function confirmReset (account: Account) {
    current.value = account
    confirmTitle.value = '重置密码'
    confirmText.value = `确认为「${account.realname || account.username}」重置密码？新密码随机生成，只显示一次。`
    confirmColor.value = 'primary'
    confirmAction = async () => {
      const result = await accountApi.resetAccountPassword(account.uid)
      if (result.initial_password) {
        generatedPassword.value = result.initial_password
        passwordDialog.value = true
      }
      toast.success('密码已重置')
    }
    confirmDialog.value = true
  }

  async function runConfirm () {
    if (!confirmAction) {
      return
    }

    submitting.value = true
    try {
      await confirmAction()
      confirmDialog.value = false
    } catch (error) {
      toast.error(error, '操作失败')
    } finally {
      submitting.value = false
    }
  }

  onMounted(async () => {
    await loadDepartments()
    await load()
  })
</script>
