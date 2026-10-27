<template>
  <v-card class="h-100" rounded="0">
    <v-toolbar density="comfortable" title="角色权限示例">
      <v-spacer />
      <v-btn aria-label="关闭示例" icon="mdi-close" variant="text" @click="emit('close')" />
    </v-toolbar>

    <v-card-text class="demo-body pa-6">
      <v-alert v-if="error" type="error" variant="tonal">{{ error }}</v-alert>
      <v-progress-linear v-if="loading" indeterminate />

      <template v-if="capabilities">
        <div class="flex items-center flex-wrap gap-2 mb-5">
          <h2 class="text-h5">{{ capabilities.title }}</h2>
          <v-chip size="small" variant="tonal">当前角色：{{ roleName(capabilities.role) }}</v-chip>
          <v-chip size="small" variant="tonal">网关内建示例</v-chip>
        </div>

        <v-alert class="mb-5" type="info" variant="tonal">
          此页使用演示数据。组件按服务端返回的角色能力显示；数据接口每次请求都会重新校验网站访问范围与角色权限。
        </v-alert>

        <v-row>
          <v-col v-if="visible('welcome')" cols="12">
            <v-card border flat title="欢迎组件">
              <v-card-text>所有被授权访问该网站的用户都可以看到这段示例内容。</v-card-text>
            </v-card>
          </v-col>

          <v-col v-if="visible('team')" cols="12" md="6">
            <v-card border flat title="团队进度组件">
              <v-card-text v-if="team">
                <div>{{ team.title }}：{{ team.completed }} / {{ team.total }}</div>
                <v-progress-linear class="mt-3" color="primary" :model-value="team.completed / team.total * 100" rounded />
              </v-card-text>

              <v-card-text v-else>当前角色没有此数据的读取权限。</v-card-text>
            </v-card>
          </v-col>

          <v-col v-if="visible('finance')" cols="12" md="6">
            <v-card border flat title="预算组件">
              <v-card-text v-if="finance">
                <div>{{ finance.title }}：{{ finance.used }} / {{ finance.total }} {{ finance.unit }}</div>
                <v-progress-linear class="mt-3" color="secondary" :model-value="finance.used / finance.total * 100" rounded />
              </v-card-text>

              <v-card-text v-else>当前角色没有此数据的读取权限。</v-card-text>
            </v-card>
          </v-col>
        </v-row>

        <v-card
          v-if="capabilities.can_manage && policy"
          border
          class="mt-6"
          flat
          title="配置角色权限"
        >
          <v-card-subtitle>仅网站所有者可以修改。前端组件与后端数据分别授权。</v-card-subtitle>

          <v-card-text class="flex flex-col gap-3">
            <v-select
              v-for="item in policyFields"
              :key="`${item.section}-${item.key}`"
              chips
              closable-chips
              hide-details
              :items="roleOptions"
              :label="item.label"
              :model-value="selectedRoles(item)"
              multiple
              variant="outlined"
              @update:model-value="setRoles(item, $event)"
            />

            <div>
              <v-btn color="primary" :loading="saving" @click="void save()">保存示例权限</v-btn>
              <span v-if="saved" class="ml-3 text-success">已保存，权限立即生效</span>
            </div>
          </v-card-text>
        </v-card>
      </template>
    </v-card-text>
  </v-card>
</template>

<script setup lang="ts">
  import { onMounted, ref } from 'vue'
  import { type DemoCapabilities, type DemoComponent, type DemoDataset, type DemoPolicy, type FinanceData, getDemoCapabilities, getDemoData, getDemoPolicy, putDemoPolicy, type TeamData } from '@/api/site-demo'

  const props = defineProps<{ siteId: string }>()
  const emit = defineEmits<{ close: [] }>()
  const capabilities = ref<DemoCapabilities | null>(null)
  const policy = ref<DemoPolicy | null>(null)
  const team = ref<TeamData | null>(null)
  const finance = ref<FinanceData | null>(null)
  const loading = ref(false)
  const saving = ref(false)
  const saved = ref(false)
  const error = ref('')
  type PolicyField = { section: 'components', key: DemoComponent, label: string } | { section: 'data', key: DemoDataset, label: string }
  const policyFields: PolicyField[] = [
    { section: 'components', key: 'welcome', label: '欢迎组件可见角色' },
    { section: 'components', key: 'team', label: '团队组件可见角色' },
    { section: 'data', key: 'team', label: '团队数据可读取角色' },
    { section: 'components', key: 'finance', label: '预算组件可见角色' },
    { section: 'data', key: 'finance', label: '预算数据可读取角色' },
  ]
  const roleLabels: Record<string, string> = {
    president: '总裁', developer: '开发者', admin: '管理员', auditor: '审计员',
    manager: '部门负责人', employee: '员工', operator: '操作员',
  }
  const roleOptions = Object.entries(roleLabels).map(([value, title]) => ({ value, title }))

  function roleName (role: string) {
    return roleLabels[role] ?? role
  }
  function visible (component: DemoComponent) {
    return capabilities.value?.components.includes(component) ?? false
  }
  function selectedRoles (field: PolicyField): string[] {
    if (!policy.value) return []
    return field.section === 'components' ? policy.value.components[field.key] : policy.value.data[field.key]
  }
  function setRoles (field: PolicyField, roles: string[]) {
    if (!policy.value) return
    if (field.section === 'components') policy.value.components[field.key] = roles
    else policy.value.data[field.key] = roles
  }
  async function load () {
    loading.value = true
    error.value = ''
    capabilities.value = null
    team.value = null
    finance.value = null
    try {
      const result = await getDemoCapabilities(props.siteId)
      capabilities.value = result
      const jobs: Promise<unknown>[] = []
      if (result.components.includes('team') && result.data.includes('team')) {
        jobs.push(getDemoData<TeamData>(props.siteId, 'team').then(value => {
          team.value = value
        }))
      }
      if (result.components.includes('finance') && result.data.includes('finance')) {
        jobs.push(getDemoData<FinanceData>(props.siteId, 'finance').then(value => {
          finance.value = value
        }))
      }
      if (result.can_manage) {
        jobs.push(getDemoPolicy(props.siteId).then(value => {
          policy.value = value.policy
        }))
      }
      await Promise.all(jobs)
    } catch (error_) {
      error.value = error_ instanceof Error ? error_.message : '示例加载失败'
    } finally {
      loading.value = false
    }
  }
  async function save () {
    if (!policy.value) return
    saving.value = true
    saved.value = false
    error.value = ''
    try {
      await putDemoPolicy(props.siteId, policy.value)
      await load()
      saved.value = true
    } catch (error_) {
      error.value = error_ instanceof Error ? error_.message : '权限保存失败'
    } finally {
      saving.value = false
    }
  }

  onMounted(() => void load())
</script>

<style scoped>
.demo-body { max-width: 1100px; margin: 0 auto; }
</style>
