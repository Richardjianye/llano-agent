<template>
  <div class="mcp-layout">
    <v-card border class="h-full min-h-1/2 sticky top-4" flat rounded="lg">
      <v-card-item>
        <v-card-title class="text-subtitle-1">MCP 工具集</v-card-title>
        <v-card-subtitle>按平台动态加载配置与权限</v-card-subtitle>

        <template #append>
          <v-btn
            v-perm="'mcp:edit'"
            icon="mdi-plus"
            size="small"
            variant="tonal"
            @click="openCreate"
          />
        </template>
      </v-card-item>

      <v-divider />

      <v-list density="comfortable" nav>
        <v-list-item
          v-for="item in instances"
          :key="item.id"
          :active="selected?.id === item.id"
          :subtitle="item.code"
          :title="item.name"
          @click="selectInstance(item)"
        >
          <template #prepend><v-icon icon="mdi-database-outline" /></template>
          <template #append><v-icon :color="item.status === 1 ? 'success' : 'grey'" icon="mdi-circle" size="9" /></template>
        </v-list-item>
      </v-list>

      <v-empty-state v-if="instances.length === 0 && !loading" icon="mdi-power-plug-off-outline" text="先添加一个 MCP 平台实例" title="尚未配置" />
    </v-card>

    <v-card
      v-if="selected"
      border
      class="h-full min-w-0 min-h-1/2"
      flat
      rounded="lg"
    >
      <v-card-item>
        <v-card-title>{{ selected.name }}</v-card-title>
        <v-card-subtitle>{{ selected.adapter_name }} · {{ selected.code }}</v-card-subtitle>

        <template #append>
          <v-chip :color="selected.status === 1 ? 'success' : 'grey'" size="small" variant="tonal">{{ selected.status === 1 ? '已启用' : '已停用' }}</v-chip>
        </template>
      </v-card-item>

      <v-tabs v-model="tab" color="primary">
        <v-tab value="config">配置</v-tab>
        <v-tab value="grants">授权</v-tab>
        <v-tab value="catalog">接口目录</v-tab>
        <v-tab value="logs">调用日志</v-tab>
      </v-tabs>

      <v-divider />

      <v-window v-model="tab">
        <v-window-item value="config">
          <v-card-text class="p-6">
            <div class="text-body-2 text-medium-emphasis mb-5">密钥只会提交到网关并加密保存，页面不会读取明文。</div>

            <v-form @submit.prevent="saveConfig">
              <v-row>
                <v-col cols="12" md="6">
                  <v-text-field
                    v-model="instanceName"
                    label="显示名称"
                    maxlength="128"
                    variant="outlined"
                  />
                </v-col>

                <v-col cols="12" md="6">
                  <v-text-field
                    hint="创建后不可修改，用于稳定识别和调用路由"
                    label="实例编码"
                    :model-value="selected.code"
                    persistent-hint
                    readonly
                    variant="outlined"
                  />
                </v-col>
              </v-row>

              <v-row>
                <v-col v-for="field in activeAdapter?.config_schema ?? []" :key="field.key" cols="12" md="6">
                  <v-text-field
                    v-model="configForm[field.key]"
                    :hint="field.secret && selected.configured_secret_fields.includes(field.key) ? '已配置；留空表示不修改' : undefined"
                    :label="field.label"
                    persistent-hint
                    :type="field.type === 'password' ? 'password' : field.type === 'number' ? 'number' : 'text'"
                    variant="outlined"
                  />
                </v-col>
              </v-row>

              <div class="mt-4">
                <v-btn v-perm="'mcp:edit'" color="primary" :loading="saving" type="submit">保存配置</v-btn>
              </div>
            </v-form>
          </v-card-text>
        </v-window-item>

        <v-window-item value="grants">
          <v-card-text>
            <div class="flex items-center justify-end mb-4">
              <v-btn
                v-perm="'mcp:edit'"
                color="primary"
                prepend-icon="mdi-shield-plus-outline"
                variant="tonal"
                @click="openGrant()"
              >新增授权</v-btn>
            </div>

            <v-data-table :headers="grantHeaders" :items="grantUsers" :items-per-page="20" :loading="loadingGrants">
              <template #[`item.count`]="{ item }">
                <v-chip color="primary" size="small" variant="tonal">{{ item.count }} 项</v-chip>
              </template>

              <template #[`item.domains`]="{ item }">
                <span class="text-body-2">{{ item.domains.join('、') }}</span>
              </template>

              <template #[`item.actions`]="{ item }">
                <div class="flex items-center justify-end gap-1">
                  <v-btn size="small" variant="text" @click="openGrant(item.user_uid)">调整</v-btn>

                  <v-btn
                    v-perm="'mcp:edit'"
                    color="warning"
                    size="small"
                    variant="text"
                    @click="confirmRevokeGrant(item)"
                  >吊销</v-btn>
                </div>
              </template>
            </v-data-table>
          </v-card-text>
        </v-window-item>

        <v-window-item value="catalog">
          <v-card-text>
            <v-row class="mb-2" dense>
              <v-col cols="8">
                <v-text-field
                  v-model="query"
                  clearable
                  hide-details
                  label="搜索短码或接口名"
                  prepend-inner-icon="mdi-magnify"
                  variant="outlined"
                  @keyup.enter="loadCapabilities"
                />
              </v-col>

              <v-col cols="4">
                <v-select
                  v-model="domain"
                  clearable
                  hide-details
                  :items="domains"
                  label="分类"
                  variant="outlined"
                  @update:model-value="loadCapabilities"
                />
              </v-col>
            </v-row>

            <div class="flex gap-3 mb-4 mt-3">
              <v-btn variant="tonal" @click="loadCapabilities">查询</v-btn>
              <v-btn v-perm="'mcp:edit'" prepend-icon="mdi-sync" variant="text" @click="syncCatalog">同步目录</v-btn>
            </div>

            <v-data-table :headers="capHeaders" :items="capabilities" :items-per-page="10" :loading="loadingCaps">
              <template #[`item.code`]="{ item }"><code>{{ item.code }}</code></template>
              <template #[`item.method`]="{ item }"><v-chip color="primary" size="x-small" variant="tonal">{{ item.method }}</v-chip></template>

              <template #[`item.status`]="{ item }">
                <v-switch
                  v-perm="'mcp:edit'"
                  color="success"
                  density="compact"
                  hide-details
                  :model-value="item.status === 1"
                  @update:model-value="toggleCapability(item, $event)"
                />
              </template>
            </v-data-table>
          </v-card-text>
        </v-window-item>

        <v-window-item value="logs">
          <v-card-text>
            <v-data-table :headers="logHeaders" :items="logs" :items-per-page="20" :loading="loadingLogs">
              <template #[`item.status`]="{ item }"><v-chip :color="statusColor(item.status)" size="small" variant="tonal">{{ item.status }}</v-chip></template>
            </v-data-table>
          </v-card-text>
        </v-window-item>
      </v-window>
    </v-card>

    <v-empty-state
      v-else
      class="mcp-main"
      icon="mdi-cursor-default-click-outline"
      text="从左侧选择一个 MCP 实例"
      title="选择工具集"
    />
  </div>

  <v-dialog v-model="createDialog" max-width="620" persistent>
    <v-card title="添加 MCP 实例">
      <v-card-text>
        <v-select
          v-model="createForm.adapter_key"
          item-title="name"
          item-value="key"
          :items="adapters"
          label="适配器"
          variant="outlined"
        />

        <v-row dense><v-col cols="6"><v-text-field v-model="createForm.name" label="显示名称" variant="outlined" /></v-col><v-col cols="6"><v-text-field v-model="createForm.code" label="实例编码" variant="outlined" /></v-col></v-row>

        <v-text-field
          v-for="field in createAdapter?.config_schema ?? []"
          :key="field.key"
          v-model="createForm.fields[field.key]"
          :label="field.label"
          :type="field.type === 'password' ? 'password' : 'text'"
          variant="outlined"
        />
      </v-card-text>

      <v-card-actions><v-spacer /><v-btn @click="createDialog=false">取消</v-btn><v-btn color="primary" :loading="saving" @click="submitCreate">创建并同步目录</v-btn></v-card-actions>
    </v-card>
  </v-dialog>

  <v-dialog v-model="grantDialog" max-width="900" persistent>
    <v-card title="为用户配置 MCP 权限">
      <v-card-text>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-3 items-center">
          <MemberPicker
            v-model="grantForm.user_uid"
            hide-details
            label="用户"
            :loading="loadingGrantUser"
            @update:model-value="value => void onGrantUserChange(typeof value === 'string' ? value : null)"
          />

          <v-text-field
            v-model="grantSearch"
            clearable
            hide-details
            label="筛选接口"
            prepend-inner-icon="mdi-magnify"
            variant="outlined"
          />
        </div>

        <div class="flex items-center justify-between mt-3">
          <span class="text-caption text-medium-emphasis">已选择 {{ grantForm.codes.length }} 项权限</span>

          <v-switch
            v-model="onlySelectedGrants"
            color="primary"
            density="compact"
            hide-details
            label="仅展示已勾选"
          />
        </div>

        <div class="grant-list mt-3">
          <v-progress-linear v-if="loadingGrantUser" color="primary" indeterminate />

          <v-expansion-panels v-else multiple variant="accordion">
            <v-expansion-panel v-for="group in filteredGroups" :key="group.domain">
              <v-expansion-panel-title>
                <div class="flex items-center gap-2 min-w-0">
                  <v-checkbox-btn
                    class="shrink-0"
                    :indeterminate="domainSelectionCount(group.codes) > 0 && domainSelectionCount(group.codes) < group.codes.length"
                    :model-value="group.codes.every(c => grantForm.codes.includes(c))"
                    @click.stop
                    @update:model-value="toggleDomain(group.codes, $event)"
                  />

                  <strong class="truncate">{{ group.domain }}</strong>

                  <span class="text-caption text-medium-emphasis whitespace-nowrap">
                    {{ domainSelectionCount(group.codes) }} / {{ group.codes.length }} 个接口
                  </span>
                </div>
              </v-expansion-panel-title>

              <v-expansion-panel-text>
                <div class="grid grid-cols-1 md:grid-cols-2 gap-x-4 gap-y-1">
                  <v-checkbox
                    v-for="cap in group.items"
                    :key="cap.code"
                    v-model="grantForm.codes"
                    class="min-w-0"
                    density="compact"
                    hide-details
                    :value="cap.code"
                  >
                    <template #label>
                      <span class="min-w-0 break-words">{{ cap.name }} <code>{{ cap.code }}</code></span>
                    </template>
                  </v-checkbox>
                </div>
              </v-expansion-panel-text>
            </v-expansion-panel>
          </v-expansion-panels>

          <v-empty-state
            v-if="!loadingGrantUser && filteredGroups.length === 0"
            headline="没有匹配的权限"
            icon="mdi-shield-search-outline"
            size="small"
          />
        </div>

        <v-textarea
          v-model="grantForm.constraints"
          class="mt-3"
          hint="例如 {&quot;allowed_sids&quot;:[113],&quot;max_page_size&quot;:100}"
          label="范围约束 JSON（可空）"
          persistent-hint
          rows="2"
          variant="outlined"
        />

        <v-row class="mt-2" dense>
          <v-col cols="12" md="6">
            <v-text-field v-model="grantForm.expires_at" label="到期时间（可空）" type="datetime-local" variant="outlined" />
          </v-col>

          <v-col cols="12" md="6">
            <v-text-field v-model="grantForm.remark" label="授权备注（可空）" variant="outlined" />
          </v-col>
        </v-row>
      </v-card-text>

      <v-card-actions><v-spacer /><v-btn @click="grantDialog=false">取消</v-btn><v-btn color="primary" :loading="saving" @click="submitGrant">保存授权</v-btn></v-card-actions>
    </v-card>
  </v-dialog>

  <v-dialog v-model="revokeGrantDialog" max-width="460">
    <v-card title="吊销 MCP 权限">
      <v-card-text>
        确认吊销「{{ revokeTarget?.username }}」在当前 MCP 下的全部 {{ revokeTarget?.count }} 项权限？
      </v-card-text>

      <v-card-actions>
        <v-spacer />
        <v-btn @click="revokeGrantDialog = false">取消</v-btn>
        <v-btn color="warning" :loading="saving" @click="revokeGrant">确认吊销</v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>

  <v-snackbar v-model="notice.show" :color="notice.color">{{ notice.text }}</v-snackbar>
</template>

<script setup lang="ts">
  import { computed, onMounted, reactive, ref, watch } from 'vue'
  import { createMcpInstance, listMcpAdapters, listMcpCapabilities, listMcpInstances, listMcpLogs, listMcpPermissions, type McpAdapter, type McpCallLog, type McpCapability, type McpGrant, type McpInstance, replaceMcpPermissions, revokeMcpPermissions, setMcpCapabilityStatus, syncMcpInstance, updateMcpInstance } from '@/api/mcp'
  import MemberPicker from '@/components/MemberPicker.vue'

  interface GrantUserSummary {
    user_uid: string
    username: string
    count: number
    domains: string[]
  }

  const adapters = ref<McpAdapter[]>([])
  const instances = ref<McpInstance[]>([])
  const capabilities = ref<McpCapability[]>([])
  const grantCapabilities = ref<McpCapability[]>([])
  const grants = ref<McpGrant[]>([])
  const logs = ref<McpCallLog[]>([])
  const selected = ref<McpInstance | null>(null)
  const tab = ref('config')
  const query = ref('')
  const domain = ref<string | null>(null)
  const grantSearch = ref('')
  const onlySelectedGrants = ref(false)
  const loading = ref(false)
  const loadingCaps = ref(false)
  const loadingGrants = ref(false)
  const loadingGrantUser = ref(false)
  const loadingLogs = ref(false)
  const saving = ref(false)
  const createDialog = ref(false)
  const grantDialog = ref(false)
  const revokeGrantDialog = ref(false)
  const revokeTarget = ref<GrantUserSummary | null>(null)
  const instanceName = ref('')
  const configForm = reactive<Record<string, unknown>>({})
  const createForm = reactive({ adapter_key: 'lingxing', name: '领星 ERP', code: 'lingxing-main', fields: {} as Record<string, unknown> })
  const grantForm = reactive({ user_uid: '', codes: [] as string[], constraints: '', expires_at: '', remark: '' })
  const notice = reactive({ show: false, text: '', color: 'success' })
  const activeAdapter = computed(() => adapters.value.find(a => a.key === selected.value?.adapter_key))
  const createAdapter = computed(() => adapters.value.find(a => a.key === createForm.adapter_key))
  const domains = computed(() => [...new Set(capabilities.value.map(c => c.domain))])
  const grantUsers = computed<GrantUserSummary[]>(() => {
    const grouped = new Map<string, GrantUserSummary>()
    for (const grant of grants.value) {
      if (grant.status !== 1 || grant.effect !== 'allow') continue
      const current = grouped.get(grant.user_uid) ?? {
        user_uid: grant.user_uid,
        username: grant.username,
        count: 0,
        domains: [],
      }
      current.count += 1
      if (!current.domains.includes(grant.domain)) current.domains.push(grant.domain)
      grouped.set(grant.user_uid, current)
    }
    return [...grouped.values()].map(item => ({
      ...item,
      domains: item.domains,
    }))
  })
  const filteredGroups = computed(() => {
    const needle = grantSearch.value.toLowerCase()
    const items = grantCapabilities.value.filter(c =>
      (!onlySelectedGrants.value || grantForm.codes.includes(c.code))
      && (!needle || `${c.name} ${c.code}`.toLowerCase().includes(needle)),
    )
    return [...new Set(items.map(c => c.domain))].map(d => ({ domain: d, items: items.filter(c => c.domain === d), codes: items.filter(c => c.domain === d).map(c => c.code) }))
  })
  const capHeaders = [{ title: '短码', key: 'code' }, { title: '分类', key: 'domain' }, { title: '接口名', key: 'name' }, { title: '方法', key: 'method' }, { title: '固定路径', key: 'path' }, { title: '启用', key: 'status', sortable: false }]
  const grantHeaders = [{ title: '用户', key: 'username' }, { title: '已授权', key: 'count' }, { title: '覆盖分类', key: 'domains' }, { title: '操作', key: 'actions', sortable: false, align: 'end' as const }]
  const logHeaders = [{ title: '时间', key: 'created_at' }, { title: '用户名', key: 'username' }, { title: '能力短码', key: 'capability_code' }, { title: '状态', key: 'status' }, { title: '耗时(ms)', key: 'latency_ms' }, { title: '请求 ID', key: 'request_id' }]

  function toast (text: string, color = 'success') {
    Object.assign(notice, { show: true, text, color })
  }
  function statusColor (status: string) {
    return status === 'success' ? 'success' : (status === 'denied' ? 'warning' : 'error')
  }
  async function loadBase () {
    loading.value = true
    try {
      const [adapterResult, instanceResult] = await Promise.all([
        listMcpAdapters(), listMcpInstances(),
      ])
      adapters.value = adapterResult.items
      instances.value = instanceResult.items
      if (selected.value) {
        const refreshed = instances.value.find(item => item.id === selected.value?.id)
        if (refreshed) {
          selected.value = refreshed
          resetConfigForm(refreshed)
        }
      } else if (instances.value[0]) {
        await selectInstance(instances.value[0])
      }
    } catch (error) {
      toast(String(error), 'error')
    } finally {
      loading.value = false
    }
  }
  function resetConfigForm (item: McpInstance) {
    instanceName.value = item.name
    for (const key of Object.keys(configForm)) {
      delete configForm[key]
    }
    Object.assign(configForm, item.config)
  }
  async function selectInstance (item: McpInstance) {
    selected.value = item
    resetConfigForm(item)
    await Promise.all([loadCapabilities(), loadGrants(), loadLogs()])
  }
  async function loadCapabilities () {
    if (!selected.value) return
    loadingCaps.value = true
    try {
      capabilities.value = (await listMcpCapabilities(selected.value.id, query.value || undefined, domain.value || undefined)).items
    } finally {
      loadingCaps.value = false
    }
  }
  async function loadGrants () {
    if (!selected.value) return
    loadingGrants.value = true
    try {
      grants.value = (await listMcpPermissions(selected.value.id)).items
    } finally {
      loadingGrants.value = false
    }
  }
  async function loadLogs () {
    if (!selected.value) return
    loadingLogs.value = true
    try {
      logs.value = (await listMcpLogs(selected.value.id)).items
    } finally {
      loadingLogs.value = false
    }
  }
  function openCreate () {
    createForm.fields = {}
    createDialog.value = true
  }
  async function submitCreate () {
    const adapter = createAdapter.value
    if (!adapter) return
    const auth_config: Record<string, unknown> = {}
    const config: Record<string, unknown> = {}
    for (const field of adapter.config_schema) {
      const target = field.secret ? auth_config : config
      target[field.key] = createForm.fields[field.key] ?? field.default
    }
    saving.value = true
    try {
      await createMcpInstance({
        code: createForm.code,
        name: createForm.name,
        adapter_key: createForm.adapter_key,
        auth_config,
        config,
      })
      createDialog.value = false
      await loadBase()
      toast('实例已创建，目录已同步')
    } catch (error) {
      toast(String(error), 'error')
    } finally {
      saving.value = false
    }
  }
  async function saveConfig () {
    if (!selected.value || !activeAdapter.value) return
    const name = instanceName.value.trim()
    if (!name) {
      toast('显示名称不能为空', 'warning')
      return
    }
    const auth_config: Record<string, unknown> = {}
    const config: Record<string, unknown> = {}
    for (const field of activeAdapter.value.config_schema) {
      const value = configForm[field.key]
      if (field.secret && value) {
        auth_config[field.key] = value
      } else if (!field.secret) {
        config[field.key] = value ?? field.default
      }
    }
    saving.value = true
    try {
      await updateMcpInstance(selected.value.id, { name, auth_config, config })
      toast('配置已保存')
      await loadBase()
    } catch (error) {
      toast(String(error), 'error')
    } finally {
      saving.value = false
    }
  }
  async function syncCatalog () {
    if (!selected.value) return
    try {
      const result = await syncMcpInstance(selected.value.id)
      await loadCapabilities()
      toast(`目录共 ${result.total} 项；新增 ${result.added}、短码修复 ${result.renamed}、变化 ${result.changed}、下线 ${result.unavailable}`)
    } catch (error) {
      toast(String(error), 'error')
    }
  }
  async function toggleCapability (capability: McpCapability, enabled: boolean | null) {
    if (!selected.value) return
    try {
      await setMcpCapabilityStatus(selected.value.id, capability.id, enabled ? 1 : 0)
      capability.status = enabled ? 1 : 0
    } catch (error) {
      toast(String(error), 'error')
      await loadCapabilities()
    }
  }
  async function openGrant (userUid = '') {
    const request = ++grantUserRequest
    grantForm.user_uid = userUid
    grantForm.codes = []
    grantForm.constraints = ''
    grantForm.expires_at = ''
    grantForm.remark = ''
    grantSearch.value = ''
    onlySelectedGrants.value = false
    grantCapabilities.value = []
    grantDialog.value = true
    loadingGrantUser.value = true
    if (selected.value) {
      try {
        const rows = (await listMcpCapabilities(selected.value.id)).items
        if (request !== grantUserRequest) return
        grantCapabilities.value = rows
      } catch (error) {
        toast(String(error), 'error')
      }
    }
    if (userUid && request === grantUserRequest) {
      await loadGrantUser(userUid)
    } else if (request === grantUserRequest) {
      loadingGrantUser.value = false
    }
  }
  async function onGrantUserChange (userUid: string | null) {
    grantForm.codes = []
    grantForm.constraints = ''
    grantForm.expires_at = ''
    grantForm.remark = ''
    if (userUid) await loadGrantUser(userUid)
  }
  let grantUserRequest = 0
  async function loadGrantUser (userUid: string) {
    if (!selected.value) return
    const request = ++grantUserRequest
    loadingGrantUser.value = true
    try {
      const rows = (await listMcpPermissions(selected.value.id, userUid)).items
      if (request !== grantUserRequest || grantForm.user_uid !== userUid) return
      const allowed = rows.filter(grant => grant.status === 1 && grant.effect === 'allow')
      grantForm.codes = allowed.map(grant => grant.capability_code)
      const first = allowed[0]
      grantForm.constraints = first && Object.keys(first.constraints).length > 0
        ? JSON.stringify(first.constraints)
        : ''
      grantForm.expires_at = first?.expires_at?.slice(0, 16) ?? ''
      grantForm.remark = first?.remark ?? ''
    } catch (error) {
      if (request === grantUserRequest) toast(String(error), 'error')
    } finally {
      if (request === grantUserRequest) loadingGrantUser.value = false
    }
  }
  function domainSelectionCount (codes: string[]) {
    return codes.filter(code => grantForm.codes.includes(code)).length
  }
  function toggleDomain (codes: string[], checked: boolean | null) {
    grantForm.codes = checked ? [...new Set([...grantForm.codes, ...codes])] : grantForm.codes.filter(c => !codes.includes(c))
  }
  async function submitGrant () {
    if (!selected.value || !grantForm.user_uid) {
      toast('请选择用户', 'warning')
      return
    }
    let constraints = {}
    try {
      constraints = grantForm.constraints.trim() ? JSON.parse(grantForm.constraints) : {}
    } catch {
      toast('范围约束不是合法 JSON', 'warning')
      return
    }
    saving.value = true
    try {
      await replaceMcpPermissions(selected.value.id, {
        user_uid: grantForm.user_uid,
        capability_codes: grantForm.codes,
        effect: 'allow',
        constraints,
        expires_at: grantForm.expires_at || null,
        remark: grantForm.remark || null,
        replace: true,
      })
      grantDialog.value = false
      await loadGrants()
      toast(`已保存 ${grantForm.codes.length} 项权限`)
    } catch (error) {
      toast(String(error), 'error')
    } finally {
      saving.value = false
    }
  }
  function confirmRevokeGrant (item: GrantUserSummary) {
    revokeTarget.value = item
    revokeGrantDialog.value = true
  }
  async function revokeGrant () {
    if (!selected.value || !revokeTarget.value) return
    saving.value = true
    try {
      await revokeMcpPermissions(selected.value.id, revokeTarget.value.user_uid)
      revokeGrantDialog.value = false
      toast(`已吊销 ${revokeTarget.value.username} 的全部 MCP 权限`)
      revokeTarget.value = null
      await loadGrants()
    } catch (error) {
      toast(String(error), 'error')
    } finally {
      saving.value = false
    }
  }
  watch(tab, value => {
    if (value === 'logs') void loadLogs()
  })
  watch(grantDialog, open => {
    if (!open) {
      grantUserRequest += 1
      loadingGrantUser.value = false
    }
  })
  onMounted(loadBase)
</script>

<style scoped>
.mcp-layout { display: grid; grid-template-columns: 280px minmax(0, 1fr); gap: 16px; align-items: start; }
.mcp-main { min-height: 620px; min-width: 0; }
.grant-list { max-height: 50vh; overflow-y: auto; border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: 8px; padding: 16px; }
code { font-size: 12px; }
@media (max-width: 900px) { .mcp-layout { grid-template-columns: 1fr; } .mcp-sidebar { min-height: auto; position: static; } }
</style>
