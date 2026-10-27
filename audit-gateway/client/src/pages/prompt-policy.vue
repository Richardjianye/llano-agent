<template>
  <div class="flex flex-col gap-4">
    <v-alert density="compact" type="info" variant="tonal">
      公司统一规则会排在默认人格（SOUL）之前。发布只影响之后创建的新会话；正在进行、
      恢复或压缩的会话继续使用创建时固定的版本，不会在对话中途换提示词。
    </v-alert>

    <v-alert
      v-if="!canEdit && !canPublish"
      density="compact"
      type="info"
      variant="outlined"
    >
      当前账号为只读权限，可以查看发布版、草稿与历史记录，不能保存、发布或回滚。
    </v-alert>

    <v-alert
      v-else-if="!canEdit && canPublish"
      density="compact"
      type="info"
      variant="outlined"
    >
      当前账号可以发布和回滚，但不能修改草稿；草稿正文由开发者或管理部门负责人维护。
    </v-alert>

    <v-alert
      v-else-if="canEdit && !canPublish"
      density="compact"
      type="info"
      variant="outlined"
    >
      当前账号可以维护草稿，但不能发布或回滚；保存不会影响员工的新会话。
    </v-alert>

    <v-alert
      v-if="needsRefresh"
      density="compact"
      type="warning"
      variant="tonal"
    >
      页面中的草稿 revision 或发布版本已不是可安全写入的最新状态。为避免覆盖他人修改或
      重复发布，写操作已暂停；请点击刷新后继续。
    </v-alert>

    <v-card border flat rounded="lg">
      <v-card-item>
        <v-card-title class="text-subtitle-1">当前发布版</v-card-title>

        <v-card-subtitle>新会话当前采用的不可变策略快照</v-card-subtitle>

        <template #append>
          <div class="flex items-center gap-2">
            <v-chip
              v-if="published"
              color="success"
              size="small"
              :text="`v${published.version}`"
              variant="tonal"
            />

            <v-btn
              icon="mdi-refresh"
              :loading="loading"
              size="small"
              title="刷新"
              variant="text"
              @click="reload"
            />
          </div>
        </template>
      </v-card-item>

      <v-divider />

      <v-progress-linear v-if="loading" color="primary" indeterminate />

      <v-card-text v-if="published" class="flex flex-col gap-4">
        <div class="policy-meta-grid text-body-2">
          <div>
            <div class="text-caption text-medium-emphasis">发布时间</div>

            <div>{{ formatTime(published.published_at) }}</div>
          </div>

          <div>
            <div class="text-caption text-medium-emphasis">发布人</div>

            <div>{{ published.published_by || '—' }}</div>
          </div>

          <div>
            <div class="text-caption text-medium-emphasis">内容哈希</div>

            <code :title="published.content_hash">{{ shortHash(published.content_hash) }}</code>
          </div>

          <div>
            <div class="text-caption text-medium-emphasis">版本来源</div>

            <div>{{ sourceLabel(published) }}</div>
          </div>
        </div>

        <v-expansion-panels variant="accordion">
          <v-expansion-panel title="查看公司统一规则">
            <v-expansion-panel-text>
              <pre class="policy-preview">{{ published.company_core_policy }}</pre>
            </v-expansion-panel-text>
          </v-expansion-panel>

          <v-expansion-panel title="查看默认人格（SOUL）">
            <v-expansion-panel-text>
              <pre class="policy-preview">{{ published.default_persona }}</pre>
            </v-expansion-panel-text>
          </v-expansion-panel>
        </v-expansion-panels>
      </v-card-text>

      <v-card-text v-else-if="loaded && !loading">
        <v-alert density="compact" type="warning" variant="tonal">
          尚无发布版本。安装包内置基线仍会兜底；完成草稿后由具备发布权限的人员发布首版。
        </v-alert>
      </v-card-text>
    </v-card>

    <v-card border flat rounded="lg">
      <v-card-item>
        <v-card-title class="text-subtitle-1">工作草稿</v-card-title>

        <v-card-subtitle>
          草稿 revision {{ draft.revision }} · 基于
          {{ draft.base_version === null ? '安装包基线' : `v${draft.base_version}` }}
        </v-card-subtitle>

        <template #append>
          <v-chip
            v-if="dirty"
            color="warning"
            size="small"
            text="有未保存修改"
            variant="tonal"
          />

          <v-chip
            v-else
            color="success"
            size="small"
            text="已保存"
            variant="tonal"
          />
        </template>
      </v-card-item>

      <v-divider />

      <v-card-text class="flex flex-col gap-4">
        <v-textarea
          v-model="draft.company_core_policy"
          auto-grow
          counter
          label="公司统一规则（最先注入）"
          max-rows="24"
          :readonly="editorLocked"
          rows="12"
          spellcheck="false"
          variant="outlined"
        />

        <v-textarea
          v-model="draft.default_persona"
          auto-grow
          counter
          label="默认人格（SOUL）"
          max-rows="24"
          :readonly="editorLocked"
          rows="12"
          spellcheck="false"
          variant="outlined"
        />

        <div class="text-caption text-medium-emphasis">
          草稿状态最后更新：{{ formatTime(draft.updated_at) }} · {{ draft.updated_by || '尚未保存' }}
        </div>
      </v-card-text>

      <v-divider v-if="canEdit || canPublish" />

      <v-card-actions v-if="canEdit || canPublish" class="flex-wrap gap-2">
        <span class="text-caption text-medium-emphasis">
          保存草稿不会影响员工；发布后只在新会话生效。
        </span>

        <v-spacer />

        <v-btn
          v-if="canEdit"
          color="primary"
          :disabled="!dirty || writeLocked"
          :loading="saving"
          prepend-icon="mdi-content-save-outline"
          text="保存草稿"
          variant="tonal"
          @click="saveDraft"
        />

        <v-btn
          v-if="canPublish"
          color="success"
          :disabled="dirty || writeLocked"
          prepend-icon="mdi-publish"
          text="发布草稿"
          variant="tonal"
          @click="publishDialog = true"
        />
      </v-card-actions>
    </v-card>

    <v-card border flat rounded="lg">
      <v-card-item>
        <v-card-title class="text-subtitle-1">版本历史</v-card-title>

        <v-card-subtitle>
          历史版本不可修改；回滚会用旧内容创建一个有完整审计记录的新版本
        </v-card-subtitle>
      </v-card-item>

      <v-divider />

      <v-data-table
        density="comfortable"
        :headers="historyHeaders"
        item-value="version"
        :items="history"
        :items-per-page="10"
        :loading="loading"
        no-data-text="还没有发布历史"
      >
        <template #[`item.version`]="{ item }">
          <div class="flex items-center gap-2">
            <span class="font-weight-medium">v{{ item.version }}</span>

            <v-chip
              v-if="item.version === published?.version"
              color="success"
              size="x-small"
              text="当前"
              variant="tonal"
            />
          </div>
        </template>

        <template #[`item.content_hash`]="{ item }">
          <code :title="item.content_hash">{{ shortHash(item.content_hash) }}</code>
        </template>

        <template #[`item.source_version`]="{ item }">
          {{ sourceLabel(item) }}
        </template>

        <template #[`item.published_at`]="{ item }">
          {{ formatTime(item.published_at) }}
        </template>

        <template #[`item.actions`]="{ item }">
          <v-btn
            size="small"
            text="查看内容"
            variant="text"
            @click="openHistoryDetail(item)"
          />

          <v-btn
            v-if="canPublish && item.version !== published?.version"
            color="warning"
            :disabled="dirty || writeLocked"
            size="small"
            text="回滚至此内容"
            variant="text"
            @click="openRollback(item)"
          />
        </template>
      </v-data-table>
    </v-card>

    <v-card border flat rounded="lg">
      <v-card-item>
        <v-card-title class="text-subtitle-1">操作审计</v-card-title>

        <v-card-subtitle>
          仅记录操作人、版本和内容哈希，不在审计日志中复制提示词正文
        </v-card-subtitle>
      </v-card-item>

      <v-divider />

      <v-data-table
        density="comfortable"
        :headers="eventHeaders"
        item-value="id"
        :items="historyEvents"
        :items-per-page="10"
        :loading="loading"
        no-data-text="还没有操作记录"
      >
        <template #[`item.action`]="{ item }">
          {{ eventActionLabel(item.action) }}
        </template>

        <template #[`item.target`]="{ item }">
          {{ eventTarget(item) }}
        </template>

        <template #[`item.content_hash`]="{ item }">
          <code :title="item.content_hash">{{ shortHash(item.content_hash) }}</code>
        </template>

        <template #[`item.created_at`]="{ item }">
          {{ formatTime(item.created_at) }}
        </template>
      </v-data-table>
    </v-card>

    <v-dialog v-model="historyDetailDialog" max-width="900">
      <v-card :title="`v${historyTarget?.version ?? ''} 策略内容`">
        <v-card-text v-if="historyTarget" class="flex flex-col gap-4">
          <div class="text-caption text-medium-emphasis">
            {{ formatTime(historyTarget.published_at) }} ·
            {{ historyTarget.published_by || '—' }} ·
            {{ historyTarget.content_hash }}
          </div>

          <div>
            <div class="text-subtitle-2 mb-2">公司统一规则</div>

            <pre class="policy-preview">{{ historyTarget.company_core_policy }}</pre>
          </div>

          <div>
            <div class="text-subtitle-2 mb-2">默认人格（SOUL）</div>

            <pre class="policy-preview">{{ historyTarget.default_persona }}</pre>
          </div>
        </v-card-text>

        <v-card-actions>
          <v-spacer />

          <v-btn text="关闭" variant="text" @click="historyDetailDialog = false" />
        </v-card-actions>
      </v-card>
    </v-dialog>

    <v-dialog v-model="publishDialog" max-width="520" persistent>
      <v-card title="发布提示词策略">
        <v-card-text>
          确认发布 revision {{ draft.revision }}？发布会创建
          v{{ (published?.version ?? 0) + 1 }}，之后创建的新会话将使用这版规则和默认人格。
          已存在的会话不受影响。
        </v-card-text>

        <v-card-actions>
          <v-spacer />

          <v-btn text="取消" variant="text" @click="publishDialog = false" />

          <v-btn
            color="success"
            :loading="publishing"
            text="确认发布"
            variant="tonal"
            @click="publishDraft"
          />
        </v-card-actions>
      </v-card>
    </v-dialog>

    <v-dialog v-model="rollbackDialog" max-width="520" persistent>
      <v-card title="回滚提示词策略">
        <v-card-text>
          确认复用 v{{ rollbackTarget?.version }} 的内容？历史记录不会被改写；系统会创建
          v{{ (published?.version ?? 0) + 1 }} 并记录回滚来源。新版本仍只影响之后创建的会话。
        </v-card-text>

        <v-card-actions>
          <v-spacer />

          <v-btn text="取消" variant="text" @click="rollbackDialog = false" />

          <v-btn
            color="warning"
            :loading="rollingBack"
            text="确认回滚"
            variant="tonal"
            @click="rollbackVersion"
          />
        </v-card-actions>
      </v-card>
    </v-dialog>
  </div>
</template>

<script lang="ts" setup>
  import type { PromptPolicyHistoryEvent, PromptPolicyVersion } from '@/api/prompt-policy'
  import { computed, onMounted, ref } from 'vue'
  import { onBeforeRouteLeave } from 'vue-router'
  import { usePermission } from '@/composables/usePermission'
  import { usePromptPolicyState } from '@/pages/prompt-policy-state'
  import { useToastStore } from '@/stores/toast'

  const historyHeaders = [
    { title: '版本', key: 'version', width: 110 },
    { title: '内容哈希', key: 'content_hash', width: 180, sortable: false },
    { title: '来源', key: 'source_version', width: 140, sortable: false },
    { title: '发布人', key: 'published_by', width: 160 },
    { title: '发布时间', key: 'published_at', width: 190 },
    { title: '操作', key: 'actions', width: 260, sortable: false, align: 'end' as const },
  ]
  const eventHeaders = [
    { title: '操作', key: 'action', width: 130 },
    { title: '目标', key: 'target', width: 160, sortable: false },
    { title: '内容哈希', key: 'content_hash', width: 180, sortable: false },
    { title: '操作人', key: 'actor_name', width: 160 },
    { title: '时间', key: 'created_at', width: 190 },
  ]

  const toast = useToastStore()
  const permission = usePermission()
  const canEdit = computed(() => permission.can('prompt_policy:edit'))
  const canPublish = computed(() => permission.can('prompt_policy:publish'))
  const state = usePromptPolicyState()
  const {
    loading,
    loaded,
    saving,
    publishing,
    rollingBack,
    needsRefresh,
    published,
    history,
    historyEvents,
    draft,
    dirty,
  } = state
  const writeLocked = computed(() =>
    !loaded.value
    || loading.value
    || saving.value
    || publishing.value
    || rollingBack.value
    || needsRefresh.value,
  )
  const editorLocked = computed(() => !canEdit.value || writeLocked.value)

  const publishDialog = ref(false)
  const rollbackDialog = ref(false)
  const historyDetailDialog = ref(false)
  const rollbackTarget = ref<PromptPolicyVersion | null>(null)
  const historyTarget = ref<PromptPolicyVersion | null>(null)

  function shortHash (value: string): string {
    if (!value) {
      return '—'
    }
    return value.length > 18 ? `${value.slice(0, 18)}…` : value
  }

  function sourceLabel (item: PromptPolicyVersion): string {
    return item.source_version === null ? '正常发布' : `回滚自 v${item.source_version}`
  }

  function eventActionLabel (action: string): string {
    const labels: Record<string, string> = {
      draft_created: '创建草稿',
      draft_updated: '更新草稿',
      published: '发布',
      rolled_back: '回滚',
    }
    return labels[action] ?? action
  }

  function eventTarget (event: PromptPolicyHistoryEvent): string {
    if (event.version !== null) {
      return event.source_version === null
        ? `v${event.version}`
        : `v${event.version}（回滚自 v${event.source_version}）`
    }
    return event.draft_revision === null ? '草稿' : `草稿 revision ${event.draft_revision}`
  }

  function formatTime (value: string | null): string {
    if (!value) {
      return '—'
    }
    const timestamp = new Date(value)
    return Number.isNaN(timestamp.getTime())
      ? value
      : timestamp.toLocaleString('zh-CN', { hour12: false })
  }

  async function reload () {
    if (dirty.value && !window.confirm('刷新会丢弃当前未保存的修改，确定继续？')) {
      return
    }
    try {
      await state.load()
    } catch (error) {
      toast.error(error, '加载提示词策略失败')
    }
  }

  async function saveDraft () {
    try {
      await state.saveDraft()
      toast.success('草稿已保存')
    } catch (error) {
      toast.error(error, '保存草稿失败')
    }
  }

  async function publishDraft () {
    try {
      const version = await state.publish()
      publishDialog.value = false
      toast.success(`提示词策略 v${version.version} 已发布`)
      try {
        await state.load()
      } catch {
        toast.warn(`v${version.version} 已发布，但页面刷新失败；请点击刷新后继续`)
      }
    } catch (error) {
      toast.error(error, '发布失败')
    }
  }

  function openRollback (item: PromptPolicyVersion) {
    rollbackTarget.value = item
    rollbackDialog.value = true
  }

  function openHistoryDetail (item: PromptPolicyVersion) {
    historyTarget.value = item
    historyDetailDialog.value = true
  }

  async function rollbackVersion () {
    if (!rollbackTarget.value) {
      return
    }
    try {
      const version = await state.rollback(rollbackTarget.value.version)
      rollbackDialog.value = false
      rollbackTarget.value = null
      toast.success(`已创建回滚版本 v${version.version}`)
      try {
        await state.load()
      } catch {
        toast.warn(`回滚版本 v${version.version} 已创建，但页面刷新失败；请点击刷新后继续`)
      }
    } catch (error) {
      toast.error(error, '回滚失败')
    }
  }

  onBeforeRouteLeave(() => {
    if (!dirty.value) {
      return true
    }
    return window.confirm('草稿还有未保存的修改，确定离开？')
  })

  onMounted(reload)
</script>

<style scoped>
.policy-meta-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px 24px;
}

.policy-preview {
  max-height: 420px;
  overflow: auto;
  padding: 12px;
  background: rgb(var(--v-theme-surface-light));
  border-radius: 6px;
  font-family: "Roboto Mono", monospace;
  font-size: 12px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
