import { $draftConversationMode, $draftRelatedSessionId } from '@/store/conversation-mode'
import { $activeGatewayProfile, $newChatProfile, ensureGatewayProfile, normalizeProfileKey } from '@/store/profile'
import { $currentFastMode, $currentModel, $currentProvider, $currentReasoningEffort } from '@/store/session'

// `session.create` params from the current profile + sticky-UI model/effort/fast,
// ensuring the gateway is on that profile first. Shared by the primary send path
// and the "open in split" tile path; `cwd` is the one thing that differs (the
// live composer cwd for a send, the resolved new-session cwd for a fresh tile).
//
// Resolving null profile to the active gateway's is load-bearing: in global-remote
// mode one backend serves every profile, so an omitted profile silently lands the
// chat on the launch (default) profile — the "rubberbands back to default" bug.
// A no-op for single-profile/local-pooled users (a backend resolves its own launch
// profile to None). The sticky UI model/effort/fast ride as per-session overrides,
// never the profile default (that lives in Settings → Model).
export async function desktopSessionCreateParams(
  cwd: string,
  includeRelatedSession = false
): Promise<Record<string, unknown>> {
  // Treat Send as the linearization point for the visible selector state. The
  // profile handshake below can yield long enough for background config/model
  // refreshes to finish; reading atoms afterward would silently create the
  // session with a different selection than the one the user submitted.
  const selection = {
    mode: $draftConversationMode.get(),
    relatedSessionId: includeRelatedSession ? $draftRelatedSessionId.get() : null,
    effort: $currentReasoningEffort.get().trim(),
    fast: $currentFastMode.get(),
    model: $currentModel.get().trim(),
    provider: $currentProvider.get().trim()
  }

  const profile = $newChatProfile.get() ?? normalizeProfileKey($activeGatewayProfile.get())
  await ensureGatewayProfile(profile)

  return {
    cols: 96,
    source: 'desktop',
    mode: selection.mode,
    ...(selection.relatedSessionId ? { related_session_id: selection.relatedSessionId } : {}),
    ...(cwd && { cwd }),
    ...(profile ? { profile } : {}),
    ...(selection.model
      ? { model: selection.model, ...(selection.provider ? { provider: selection.provider } : {}) }
      : {}),
    ...(selection.effort ? { reasoning_effort: selection.effort } : {}),
    fast: selection.fast
  }
}
