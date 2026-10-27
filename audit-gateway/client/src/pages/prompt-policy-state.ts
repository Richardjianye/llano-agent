/**
 * 提示词策略页状态。
 *
 * 单独放在页面外是为了把「保存带 revision、发布后刷新、回滚带当前版本」这些并发
 * 约束做成可直接单测的行为，而不是只靠模板里几个按钮碰巧传对参数。
 */

import type {
  PromptPolicyDraft,
  PromptPolicyDraftUpdate,
  PromptPolicyHistory,
  PromptPolicyHistoryEvent,
  PromptPolicyOverview,
  PromptPolicyVersion,
} from '@/api/prompt-policy'
import { computed, reactive, ref } from 'vue'
import { ApiError } from '@/api/http'
import * as promptPolicyApi from '@/api/prompt-policy'

export interface PromptPolicyClient {
  fetchPromptPolicy: () => Promise<PromptPolicyOverview>
  fetchPromptPolicyHistory: () => Promise<PromptPolicyHistory>
  savePromptPolicyDraft: (
    payload: PromptPolicyDraftUpdate,
  ) => Promise<{ draft: PromptPolicyDraft }>
  publishPromptPolicy: (
    expectedRevision: number,
  ) => Promise<{ published: PromptPolicyVersion }>
  rollbackPromptPolicy: (
    version: number,
    expectedCurrentVersion: number,
  ) => Promise<{ published: PromptPolicyVersion }>
}

const EMPTY_DRAFT: PromptPolicyDraft = {
  company_core_policy: '',
  default_persona: '',
  content_hash: null,
  revision: 0,
  base_version: null,
  updated_at: null,
  updated_by: null,
  updated_by_uid: null,
}

export function validatePromptPolicyPackage (
  companyCorePolicy: string,
  defaultPersona: string,
): string[] {
  const errors: string[] = []
  if (!companyCorePolicy.trim()) {
    errors.push('公司统一规则不能为空')
  }
  if (!defaultPersona.trim()) {
    errors.push('默认人格不能为空')
  }
  return errors
}

function requiresAuthoritativeRefresh (error: unknown): boolean {
  if (!(error instanceof ApiError) || typeof error.data !== 'object' || error.data === null) {
    return false
  }
  const details = error.data as Record<string, unknown>
  return 'current_revision' in details || 'current_version' in details
}

export function usePromptPolicyState (
  client: PromptPolicyClient = promptPolicyApi,
) {
  const loading = ref(false)
  const loaded = ref(false)
  const saving = ref(false)
  const publishing = ref(false)
  const rollingBack = ref(false)
  const needsRefresh = ref(false)
  const published = ref<PromptPolicyVersion | null>(null)
  const history = ref<PromptPolicyVersion[]>([])
  const historyEvents = ref<PromptPolicyHistoryEvent[]>([])
  const draft = reactive<PromptPolicyDraft>({ ...EMPTY_DRAFT })
  const savedContents = ref({ company_core_policy: '', default_persona: '' })
  let loadAttempt = 0

  const dirty = computed(() =>
    draft.company_core_policy !== savedContents.value.company_core_policy
    || draft.default_persona !== savedContents.value.default_persona,
  )

  function applyDraft (next: PromptPolicyDraft) {
    Object.assign(draft, next)
    savedContents.value = {
      company_core_policy: next.company_core_policy,
      default_persona: next.default_persona,
    }
  }

  async function load () {
    const attempt = ++loadAttempt
    loading.value = true
    // 保留屏幕上的旧数据供对照，但在完整的 overview + history 都拿到以前禁止写入。
    // 否则一次刷新失败会让旧 revision 重新解锁，用户只能反复撞服务端 409。
    loaded.value = false
    try {
      const [overview, historyResult] = await Promise.all([
        client.fetchPromptPolicy(),
        client.fetchPromptPolicyHistory(),
      ])
      if (attempt !== loadAttempt) {
        return
      }
      published.value = overview.published
      applyDraft(overview.draft)
      history.value = historyResult.items
      historyEvents.value = historyResult.events
      needsRefresh.value = false
      loaded.value = true
    } catch (error) {
      // 并发刷新后，较早请求的失败不能覆盖较新请求的成功状态，也不该再弹错误。
      if (attempt !== loadAttempt) {
        return
      }
      throw error
    } finally {
      if (attempt === loadAttempt) {
        loading.value = false
      }
    }
  }

  async function saveDraft () {
    if (!loaded.value) {
      throw new Error('提示词策略尚未加载，请刷新后重试')
    }
    if (needsRefresh.value) {
      throw new Error('页面状态需要刷新后才能继续编辑')
    }
    saving.value = true
    try {
      const result = await client.savePromptPolicyDraft({
        company_core_policy: draft.company_core_policy,
        default_persona: draft.default_persona,
        expected_revision: draft.revision,
      })
      applyDraft(result.draft)
      return result.draft
    } catch (error) {
      if (requiresAuthoritativeRefresh(error)) {
        needsRefresh.value = true
      }
      throw error
    } finally {
      saving.value = false
    }
  }

  async function publish () {
    if (!loaded.value) {
      throw new Error('提示词策略尚未加载，请刷新后重试')
    }
    if (needsRefresh.value) {
      throw new Error('页面状态需要刷新后才能继续发布')
    }
    if (dirty.value) {
      throw new Error('草稿还有未保存的修改，请先保存')
    }
    const errors = validatePromptPolicyPackage(
      draft.company_core_policy,
      draft.default_persona,
    )
    if (errors.length > 0) {
      throw new Error(errors[0])
    }

    publishing.value = true
    try {
      const result = await client.publishPromptPolicy(draft.revision)
      published.value = result.published
      history.value = [
        result.published,
        ...history.value.filter(item => item.version !== result.published.version),
      ]
      // 发布成功后服务端会推进草稿基准与 revision；响应只带 published，不能在
      // 前端猜下一版 revision。刷新失败时锁住后续写操作，避免重复发布。
      needsRefresh.value = true
      return result.published
    } catch (error) {
      if (requiresAuthoritativeRefresh(error)) {
        needsRefresh.value = true
      }
      throw error
    } finally {
      publishing.value = false
    }
  }

  async function rollback (version: number) {
    if (!loaded.value) {
      throw new Error('提示词策略尚未加载，请刷新后重试')
    }
    if (needsRefresh.value) {
      throw new Error('页面状态需要刷新后才能继续回滚')
    }
    if (dirty.value) {
      throw new Error('草稿还有未保存的修改，请先保存或刷新')
    }
    const currentVersion = published.value?.version
    if (currentVersion === undefined) {
      throw new Error('当前还没有已发布策略，不能回滚')
    }

    rollingBack.value = true
    try {
      const result = await client.rollbackPromptPolicy(
        version,
        currentVersion,
      )
      published.value = result.published
      history.value = [
        result.published,
        ...history.value.filter(item => item.version !== result.published.version),
      ]
      needsRefresh.value = true
      return result.published
    } catch (error) {
      if (requiresAuthoritativeRefresh(error)) {
        needsRefresh.value = true
      }
      throw error
    } finally {
      rollingBack.value = false
    }
  }

  return {
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
    load,
    saveDraft,
    publish,
    rollback,
  }
}
