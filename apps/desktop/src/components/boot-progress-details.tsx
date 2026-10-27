import { Progress } from '@/components/ui/progress'
import { useI18n } from '@/i18n'
import type { Translations } from '@/i18n/types'
import { cn } from '@/lib/utils'
import type { DesktopBootState } from '@/store/boot'

type BootSteps = Translations['boot']['steps']

/**
 * Convert internal boot phases into screenshot-safe, localized copy.
 *
 * Electron's diagnostic messages can contain a remote gateway URL or a local
 * executable path. Those belong in desktop.log, never on the primary boot
 * surface where a user may share a screenshot.
 */
export function bootStatusForPhase(phase: string, steps: BootSteps): string {
  if (phase.startsWith('runtime.windows') || phase === 'runtime.external' || phase === 'backend.runtime') {
    return steps.preparingRuntime
  }

  if (phase === 'backend.spawn' || phase === 'backend.port' || phase === 'backend.wait') {
    return steps.startingBackend
  }

  if (phase === 'backend.ready') {
    return steps.finalizingStartup
  }

  if (phase === 'backend.remote' || phase === 'renderer.gateway.connect') {
    return steps.connectingGateway
  }

  if (phase === 'renderer.config') {
    return steps.loadingSettings
  }

  if (phase === 'renderer.sessions') {
    return steps.loadingSessions
  }

  if (phase === 'backend.resolve' || phase === 'renderer.boot') {
    return steps.startingDesktopConnection
  }

  return steps.startingHermesDesktop
}

export interface BootProgressDetailsProps {
  boot: DesktopBootState
  className?: string
}

export function BootProgressDetails({ boot, className }: BootProgressDetailsProps) {
  const { t } = useI18n()
  const progress = Math.max(2, Math.min(100, Math.round(boot.progress)))
  const status = bootStatusForPhase(boot.phase, t.boot.steps)
  // Both runtime phases keep the hint up. It used to flash in and out as the
  // manager alternated check -> prepare -> check, which reads as a glitch.
  const preparingWindowsRuntime = boot.phase.startsWith('runtime.windows')
  const step = boot.step ?? null

  return (
    <div className={cn('w-full', className)}>
      <Progress aria-label={status} value={progress / 100} />
      <div className="mt-2 flex items-center justify-between gap-3 text-xs text-(--ui-text-tertiary)">
        <span aria-live="polite" className="min-w-0 truncate" role="status">
          {status}
        </span>
        <span aria-hidden="true" className="shrink-0 tabular-nums">
          {progress}%
        </span>
      </div>
      {step ? (
        // The one line support will ask for a screenshot of: which of the nine
        // components, what it is doing, and how far. A generic "preparing" for
        // five minutes is indistinguishable from a hang.
        <p aria-live="polite" className="mt-1 truncate text-xs text-(--ui-text-tertiary)" role="status">
          {t.boot.runtimeStep(
            step.index,
            step.total,
            step.label,
            step.kind === 'download' && typeof step.percent === 'number'
              ? `${t.boot.runtimeStepActions.download} ${step.percent}%`
              : t.boot.runtimeStepActions[step.kind]
          )}
        </p>
      ) : null}
      {preparingWindowsRuntime ? (
        <p className="mt-2 text-center text-xs leading-5 text-(--ui-text-tertiary)">{t.boot.runtimePreparationHint}</p>
      ) : null}
    </div>
  )
}
