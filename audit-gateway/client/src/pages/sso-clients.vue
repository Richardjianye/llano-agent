<template>
  <div class="flex flex-col gap-4">
    <v-card border flat rounded="lg">
      <v-card-item>
        <v-card-title>统一登录应用</v-card-title>
        <v-card-subtitle>登记接入方的回调地址和权限范围；应用密钥仅在创建或轮换后显示一次。</v-card-subtitle>

        <template #append>
          <v-btn v-if="canManage" color="primary" prepend-icon="mdi-plus" @click="openCreate()">登记应用</v-btn>
        </template>
      </v-card-item>
    </v-card>

    <v-alert v-if="error" closable type="error" @click:close="error = ''">{{ error }}</v-alert>
    <v-alert v-if="notice" closable type="success" @click:close="notice = ''">{{ notice }}</v-alert>

    <v-card border flat rounded="lg">
      <v-data-table :headers="headers" :items="clients" :loading="loading" no-data-text="尚未登记接入应用">
        <template #[`item.name`]="{ item }">
          <div class="font-weight-medium">{{ item.name }}</div>
          <div class="text-caption text-medium-emphasis">{{ item.client_id }}</div>
        </template>

        <template #[`item.redirect_uris`]="{ item }">
          <div v-for="uri in item.redirect_uris" :key="uri" class="text-caption">{{ uri }}</div>
        </template>

        <template #[`item.enabled`]="{ item }">
          <v-chip :color="item.enabled ? 'success' : 'warning'" size="small" variant="tonal">{{ item.enabled ? '已启用' : '已停用' }}</v-chip>
        </template>

        <template #[`item.actions`]="{ item }">
          <v-btn v-if="canManage" size="small" variant="text" @click="openEdit(item)">编辑</v-btn>

          <v-btn v-if="canManage && item.enabled && item.client_type === 'confidential'" size="small" variant="text" @click="pendingRotate = item">轮换密钥</v-btn>

          <v-btn
            v-if="canManage && item.enabled"
            color="warning"
            size="small"
            variant="text"
            @click="pendingDisable = item"
          >停用</v-btn>

          <v-btn
            v-if="canManage && !item.enabled"
            :loading="acting"
            size="small"
            variant="text"
            @click="void enable(item)"
          >重新启用</v-btn>
        </template>
      </v-data-table>
    </v-card>

    <v-dialog v-model="createOpen" max-width="640" persistent scrollable>
      <v-card :title="editingId ? '修改统一登录应用' : '登记统一登录应用'">
        <v-card-text class="flex flex-col gap-3">
          <v-text-field
            v-model="form.client_id"
            :disabled="Boolean(editingId)"
            hide-details
            label="应用 ID"
            variant="outlined"
          />

          <v-text-field v-model="form.name" hide-details label="应用名称" variant="outlined" />

          <v-text-field
            v-model="form.owner_uid"
            hide-details="auto"
            hint="填写网关账号的稳定 UID"
            label="负责人 UID"
            persistent-hint
            variant="outlined"
          />

          <v-text-field v-model="form.audit_contact" hide-details label="审计联系人" variant="outlined" />

          <v-select
            v-model="form.environment"
            :disabled="Boolean(editingId)"
            hide-details
            :items="environmentOptions"
            label="环境"
            variant="outlined"
          />

          <v-select
            v-model="form.client_type"
            :disabled="Boolean(editingId)"
            hide-details
            :items="clientTypeOptions"
            label="客户端类型"
            variant="outlined"
          />

          <v-textarea
            v-model="redirectText"
            auto-grow
            hide-details="auto"
            hint="每行一个完整回调地址，须与接入方实际地址完全一致"
            label="回调地址"
            persistent-hint
            rows="2"
            variant="outlined"
          />

          <v-select
            v-model="form.allowed_scopes"
            chips
            hide-details
            :items="scopeOptions"
            label="允许的 scope"
            multiple
            variant="outlined"
          />

          <v-alert density="compact" type="info" variant="tonal">
            正式环境只能登记 HTTPS 回调；网页登录使用授权码与 PKCE S256。成员搜索需机密应用并单独勾选 directory.read，该权限仅用于服务端应用凭据，不能经网页登录授权。应用 ID、环境与客户端类型登记后不可修改，需要变更请登记新应用。
          </v-alert>
        </v-card-text>

        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="createOpen = false">取消</v-btn>
          <v-btn color="primary" :disabled="!canCreate" :loading="acting" @click="void save()">{{ editingId ? '保存' : '登记' }}</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <v-dialog max-width="480" :model-value="pendingDisable !== null" @update:model-value="pendingDisable = null">
      <v-card title="停用接入应用">
        <v-card-text>停用 {{ pendingDisable?.name }} 后，新的授权请求、令牌兑换和 UserInfo 查询将被拒绝。</v-card-text>

        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="pendingDisable = null">取消</v-btn>
          <v-btn color="warning" :loading="acting" @click="void disable()">确认停用</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <v-dialog max-width="480" :model-value="pendingRotate !== null" @update:model-value="pendingRotate = null">
      <v-card title="轮换应用密钥">
        <v-card-text>轮换 {{ pendingRotate?.name }} 的密钥后，旧密钥立即无法用于新的令牌兑换。请先准备好接入方的密钥更新流程。</v-card-text>

        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="pendingRotate = null">取消</v-btn>
          <v-btn color="primary" :loading="acting" @click="void rotate()">确认轮换</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <v-dialog max-width="640" :model-value="secret !== null" persistent>
      <v-card title="保存应用密钥">
        <v-card-text class="flex flex-col gap-3">
          <v-alert type="warning" variant="tonal">密钥只在此处显示一次。请保存到接入方服务端的密钥存储，关闭后无法再次查看。</v-alert>
          <div class="text-body-2">应用 ID：{{ secret?.client_id }}</div>

          <v-textarea
            auto-grow
            label="客户端密钥"
            :model-value="secret?.client_secret"
            readonly
            rows="2"
            variant="outlined"
          />
        </v-card-text>

        <v-card-actions><v-spacer /><v-btn color="primary" @click="secret = null">已保存，关闭</v-btn></v-card-actions>
      </v-card>
    </v-dialog>
  </div>
</template>

<script setup lang="ts">
  import { computed, onMounted, reactive, ref } from 'vue'
  import {
    type ClientRegistration,
    type ClientSecretResult,
    disableSSOClient,
    enableSSOClient,
    listSSOClients,
    registerSSOClient,
    rotateSSOClientSecret,
    type SSOClient,
    updateSSOClient,
  } from '@/api/sso-clients'
  import { roleHasPermission } from '@/constants/roles'
  import { useAuthStore } from '@/stores/auth'

  const auth = useAuthStore()
  const canManage = computed(() => roleHasPermission(auth.user?.permission, 'sso:manage'))

  const clients = ref<SSOClient[]>([])
  const loading = ref(false)
  const acting = ref(false)
  const createOpen = ref(false)
  const editingId = ref('')
  const pendingDisable = ref<SSOClient | null>(null)
  const pendingRotate = ref<SSOClient | null>(null)
  const secret = ref<ClientSecretResult | null>(null)
  const error = ref('')
  const notice = ref('')
  const redirectText = ref('')
  function emptyForm (): Omit<ClientRegistration, 'redirect_uris'> {
    return {
      client_id: '', name: '', owner_uid: '', environment: 'dev', audit_contact: '',
      client_type: 'confidential', allowed_scopes: ['openid', 'profile', 'org'],
    }
  }
  const form = reactive(emptyForm())
  const headers = [
    { title: '应用', key: 'name' }, { title: '环境', key: 'environment' },
    { title: '负责人 UID', key: 'owner_uid' }, { title: '回调地址', key: 'redirect_uris' },
    { title: '状态', key: 'enabled' }, { title: '', key: 'actions', sortable: false },
  ]
  const environmentOptions = [{ title: '开发', value: 'dev' }, { title: '正式', value: 'prod' }]
  const clientTypeOptions = [{ title: '机密应用（服务端保存密钥）', value: 'confidential' }, { title: '公开客户端（无密钥）', value: 'public' }]
  const scopeOptions = ['openid', 'profile', 'org', 'directory.read']
  const redirects = computed(() => redirectText.value.split('\n').map(uri => uri.trim()).filter(Boolean))
  const canCreate = computed(() => Boolean(form.client_id.trim() && form.name.trim() && form.owner_uid.trim()
    && form.audit_contact.trim() && redirects.value.length > 0 && form.allowed_scopes.includes('openid')))

  function messageOf (cause: unknown) {
    return cause instanceof Error ? cause.message : '操作失败，请稍后重试'
  }

  async function load () {
    loading.value = true
    try {
      clients.value = await listSSOClients()
    } catch (error_) {
      error.value = messageOf(error_)
    } finally {
      loading.value = false
    }
  }

  function openCreate () {
    editingId.value = ''
    Object.assign(form, emptyForm())
    redirectText.value = ''
    createOpen.value = true
  }

  function openEdit (item: SSOClient) {
    editingId.value = item.client_id
    const { enabled: _enabled, redirect_uris: uris, ...rest } = item
    Object.assign(form, { ...rest, allowed_scopes: [...item.allowed_scopes] })
    redirectText.value = uris.join('\n')
    createOpen.value = true
  }

  async function save () {
    acting.value = true
    error.value = ''
    try {
      if (editingId.value) {
        const { name, owner_uid, audit_contact, allowed_scopes } = form
        await updateSSOClient(editingId.value, { name, owner_uid, audit_contact, allowed_scopes, redirect_uris: redirects.value })
        createOpen.value = false
        notice.value = `${name} 已更新`
      } else {
        const result = await registerSSOClient({ ...form, redirect_uris: redirects.value })
        createOpen.value = false
        if (result.client_secret) secret.value = result
        else notice.value = '公开客户端已登记'
      }
      await load()
    } catch (error_) {
      error.value = messageOf(error_)
    } finally {
      acting.value = false
    }
  }

  async function disable () {
    const target = pendingDisable.value
    if (!target) return
    acting.value = true
    error.value = ''
    try {
      await disableSSOClient(target.client_id)
      pendingDisable.value = null
      notice.value = `${target.name} 已停用`
      await load()
    } catch (error_) {
      error.value = messageOf(error_)
    } finally {
      acting.value = false
    }
  }

  async function enable (target: SSOClient) {
    acting.value = true
    error.value = ''
    try {
      await enableSSOClient(target.client_id)
      notice.value = `${target.name} 已重新启用`
      await load()
    } catch (error_) {
      error.value = messageOf(error_)
    } finally {
      acting.value = false
    }
  }

  async function rotate () {
    const target = pendingRotate.value
    if (!target) return
    acting.value = true
    error.value = ''
    try {
      secret.value = await rotateSSOClientSecret(target.client_id)
      pendingRotate.value = null
    } catch (error_) {
      error.value = messageOf(error_)
    } finally {
      acting.value = false
    }
  }

  onMounted(() => void load())
</script>
