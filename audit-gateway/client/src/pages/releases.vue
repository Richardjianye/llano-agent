<template>
  <v-card border flat rounded="lg">
    <v-card-item>
      <v-card-title class="text-subtitle-1">桌面端版本</v-card-title>

      <v-card-subtitle>
        客户端更新的唯一来源。清单在离线签名机上签好后贴进来，网关只验签、不签名
      </v-card-subtitle>

      <template #append>
        <v-btn color="primary" prepend-icon="mdi-upload" variant="tonal" @click="openPublish">
          发布新版本
        </v-btn>
      </template>
    </v-card-item>

    <v-divider />

    <v-alert
      v-if="!gatewayKeyFp"
      class="m-4"
      density="compact"
      type="error"
      variant="tonal"
    >
      网关未配置 <code>UPDATE_PUBLIC_KEY</code>，无法校验清单签名，当前发不了版。
      在服务端 <code>.env</code> 里填入签名机产出的公钥后重启网关。
    </v-alert>

    <v-alert
      v-else
      class="m-4"
      density="compact"
      type="info"
      variant="tonal"
    >
      网关公钥指纹 <code>{{ gatewayKeyFp }}</code>。
      它必须与客户端内置的那把一致，不一致时发出去的版本谁也装不上。
    </v-alert>

    <v-data-table
      density="comfortable"
      :headers="headers"
      item-value="id"
      :items="releases"
      :items-per-page="20"
      :loading="loading"
      no-data-text="尚未发布任何版本"
    >
      <template #[`item.version`]="{ item }">
        <div class="font-weight-medium">{{ item.version }}</div>

        <div class="text-caption text-medium-emphasis">
          {{ item.platform }} / {{ item.arch }}
        </div>
      </template>

      <template #[`item.policy`]="{ item }">
        <v-chip
          :color="item.policy === 'required' ? 'error' : 'default'"
          size="small"
          :text="item.policy === 'required' ? '强制更新' : '可选更新'"
          variant="tonal"
        />

        <div class="text-caption text-medium-emphasis mt-1">
          低于 {{ item.min_supported_version }} 强制
        </div>
      </template>

      <template #[`item.artifact`]="{ item }">
        <div class="text-caption text-truncate release-url">{{ item.artifact_url }}</div>

        <div class="text-caption text-medium-emphasis">
          {{ formatSize(item.artifact_size) }} · {{ item.artifact_sha256.slice(0, 12) }}…
        </div>
      </template>

      <template #[`item.released_at`]="{ item }">
        <span class="text-caption">{{ formatDateTime(item.released_at) }}</span>
      </template>

      <template #[`item.status`]="{ item }">
        <v-chip
          :color="item.status === 1 ? 'success' : 'grey'"
          size="small"
          :text="item.status === 1 ? '上架中' : '已下架'"
          variant="tonal"
        />
      </template>

      <template #[`item.actions`]="{ item }">
        <v-btn
          size="small"
          :text="item.status === 1 ? '下架' : '上架'"
          variant="text"
          @click="toggleStatus(item)"
        />

        <v-btn size="small" text="详情" variant="text" @click="openDetail(item)" />

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

  <v-dialog v-model="publishDialog" max-width="760" persistent>
    <v-card title="发布新版本">
      <v-card-text>
        <p class="text-body-2 mb-3">
          在签名机上执行
          <code>sign_release.py sign …</code>，把输出的 JSON 整个贴进来。
          网关会用公钥验一遍，验不过不入库。
        </p>

        <v-textarea
          v-model="publishText"
          auto-grow
          :error-messages="publishError"
          label="已签名清单 JSON"
          max-rows="20"
          rows="12"
          spellcheck="false"
          variant="outlined"
        />
      </v-card-text>

      <v-card-actions>
        <v-spacer />
        <v-btn text="取消" @click="publishDialog = false" />

        <v-btn
          color="primary"
          :loading="submitting"
          text="发布"
          variant="tonal"
          @click="submitPublish"
        />
      </v-card-actions>
    </v-card>
  </v-dialog>

  <v-dialog v-model="detailDialog" max-width="760">
    <v-card :title="`${current?.version ?? ''} 清单原文`">
      <v-card-text>
        <p class="text-body-2 mb-3">
          客户端验签用的就是这一份。排查「客户端说验不过」时，先比对签名机那边的
          输出与这里是否逐字相同。
        </p>

        <pre class="release-detail">{{ detailText }}</pre>
      </v-card-text>

      <v-card-actions>
        <v-spacer />
        <v-btn text="关闭" @click="detailDialog = false" />
      </v-card-actions>
    </v-card>
  </v-dialog>

  <v-dialog v-model="deleteDialog" max-width="460">
    <v-card title="删除版本">
      <v-card-text>
        确认删除 {{ current?.version }}（{{ current?.platform }}/{{ current?.arch }}）？
        已经装上这一版的客户端不受影响，只是之后不会再被分发。
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
  import type { ClientRelease } from '@/types/api'
  import { onMounted, ref } from 'vue'
  import * as releaseApi from '@/api/release'
  import { formatDateTime } from '@/lib/datetime'
  import { useToastStore } from '@/stores/toast'

  const toast = useToastStore()
  const headers = [
    { title: '版本', key: 'version', width: 150 },
    { title: '更新策略', key: 'policy', width: 160, sortable: false },
    { title: '构建物', key: 'artifact', sortable: false },
    { title: '发布时间', key: 'released_at', width: 190 },
    { title: '状态', key: 'status', width: 110 },
    { title: '操作', key: 'actions', align: 'end' as const, sortable: false, width: 200 },
  ]

  const releases = ref<ClientRelease[]>([])
  const gatewayKeyFp = ref<string | null>(null)
  const loading = ref(false)
  const submitting = ref(false)
  const publishDialog = ref(false)
  const detailDialog = ref(false)
  const deleteDialog = ref(false)
  const publishText = ref('')
  const publishError = ref('')
  const detailText = ref('')
  const current = ref<ClientRelease | null>(null)

  function formatSize (bytes: number): string {
    if (bytes >= 1024 * 1024) {
      return `${(bytes / 1024 / 1024).toFixed(1)} MB`
    }
    return `${Math.max(1, Math.round(bytes / 1024))} KB`
  }

  async function load () {
    loading.value = true
    try {
      const result = await releaseApi.listReleases()
      releases.value = result.items ?? []
      gatewayKeyFp.value = result.gateway_public_key_fp ?? null
    } catch (error) {
      toast.error(error, '加载版本列表失败')
    } finally {
      loading.value = false
    }
  }

  function openPublish () {
    publishText.value = ''
    publishError.value = ''
    publishDialog.value = true
  }

  async function submitPublish () {
    publishError.value = ''

    let payload: releaseApi.ReleasePublishPayload
    try {
      payload = JSON.parse(publishText.value)
    } catch {
      publishError.value = '不是合法的 JSON，请直接粘贴签名机输出的整个文件内容'
      return
    }

    // 只检查这两个键在不在。清单字段规则的真源在网关，前端不复述——
    // 复述出来的第二份规则迟早会和网关那份漂开。
    if (!payload?.manifest || !payload?.signature) {
      publishError.value = '缺少 manifest 或 signature 字段'
      return
    }

    submitting.value = true
    try {
      await releaseApi.publishRelease(payload)
      toast.success('版本已发布')
      publishDialog.value = false
      await load()
    } catch (error) {
      toast.error(error, '发布失败')
    } finally {
      submitting.value = false
    }
  }

  async function toggleStatus (item: ClientRelease) {
    try {
      await releaseApi.setReleaseStatus(item.id, item.status === 1 ? 0 : 1)
      await load()
    } catch (error) {
      toast.error(error, '状态变更失败')
    }
  }

  async function openDetail (item: ClientRelease) {
    current.value = item
    detailText.value = ''
    detailDialog.value = true
    try {
      const detail = await releaseApi.getReleaseDetail(item.id)
      detailText.value = JSON.stringify(
        { manifest: detail.manifest, signature: detail.signature }, null, 2,
      )
    } catch (error) {
      toast.error(error, '加载清单失败')
      detailDialog.value = false
    }
  }

  function confirmDelete (item: ClientRelease) {
    current.value = item
    deleteDialog.value = true
  }

  async function submitDelete () {
    if (!current.value) {
      return
    }
    submitting.value = true
    try {
      await releaseApi.deleteRelease(current.value.id)
      toast.success('版本已删除')
      deleteDialog.value = false
      await load()
    } catch (error) {
      toast.error(error, '删除失败')
    } finally {
      submitting.value = false
    }
  }

  onMounted(load)
</script>

<style scoped>
.release-url {
  max-width: 420px;
}

.release-detail {
  max-height: 420px;
  overflow: auto;
  padding: 12px;
  background: rgb(var(--v-theme-surface-light));
  border-radius: 6px;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
