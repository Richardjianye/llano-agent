import { useStore } from '@nanostores/react'
import { useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { getGlobalModelInfo, getGlobalModelOptions, setGlobalModel } from '@/hermes'
import { useI18n } from '@/i18n'
import { $collabState, updateRespondTo } from '@/store/collab'
import type { CollabMember, ModelOptionsResponse } from '@/types/hermes'

export function AgentSettings({ code, member }: { code: string; member: CollabMember }) {
  const { t } = useI18n()
  const c = t.collab.functional
  const members = useStore($collabState)[code]?.members ?? []
  const [open, setOpen] = useState(false)
  const [mode, setMode] = useState(member.respond_to || 'project_members')
  const [allowlist, setAllowlist] = useState<string[]>(member.allowlist || [])
  const [models, setModels] = useState<ModelOptionsResponse | null>(null)
  const [model, setModel] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [reload, setReload] = useState(0)

  useEffect(() => {
    if (!open) {
      return
    }

    let live = true
    setModels(null)
    setError('')
    void Promise.all([getGlobalModelInfo(), getGlobalModelOptions()])
      .then(([info, options]) => {
        if (live) {
          setModels(options)
          setModel(info.model)
        }
      })
      .catch(() => {
        if (live) {
          setError(c.loadFailed)
        }
      })

    return () => {
      live = false
    }
  }, [open, reload, c.loadFailed])

  return (
    <>
      <Button
        onClick={() => {
          setMode(member.respond_to || 'project_members')
          setAllowlist(member.allowlist || [])
          setOpen(true)
        }}
        size="sm"
        variant="secondary"
      >
        {c.agentSettings}
      </Button>
      <Dialog
        onOpenChange={value => {
          if (!busy) {
            setOpen(value)
          }
        }}
        open={open}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {member.agent_label} · {c.agentSettings}
            </DialogTitle>
          </DialogHeader>
          <label>{c.respondTo}</label>
          <Select disabled={busy} onValueChange={setMode} value={mode}>
            <SelectTrigger aria-label={c.respondTo}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {['owner_only', 'allowlist', 'project_members', 'nobody'].map(value => (
                <SelectItem key={value} value={value}>
                  {t.collab.respondTo[value]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {mode === 'allowlist' ? (
            <fieldset>
              <legend>{c.allowlist}</legend>
              {members
                .filter(m => m.member_kind !== 'agent' && !m.is_me)
                .map(m => (
                  <label className="flex items-center gap-2" key={m.id}>
                    <input
                      checked={allowlist.includes(m.user_uid)}
                      disabled={busy}
                      onChange={e =>
                        setAllowlist(old =>
                          e.target.checked ? [...old, m.user_uid] : old.filter(uid => uid !== m.user_uid)
                        )
                      }
                      type="checkbox"
                    />
                    {m.username || m.user_uid}
                  </label>
                ))}
            </fieldset>
          ) : null}
          <Button
            disabled={busy}
            onClick={() => {
              setBusy(true)
              setError('')
              void updateRespondTo(code, member.id, mode, allowlist)
                .then(() => setOpen(false))
                .catch(e => setError(String(e.message || e)))
                .finally(() => setBusy(false))
            }}
          >
            {busy ? c.saving : c.save}
          </Button>
          <label>
            {c.model} · {c.defaultModel}
          </label>
          <Select
            disabled={busy || !models}
            onValueChange={value => {
              const provider = models?.providers?.find(p => p.models?.includes(value))

              if (!provider) {
                return
              }

              setBusy(true)
              setError('')
              void setGlobalModel(provider.slug, value)
                .then(() => setModel(value))
                .catch(e => setError(String(e.message || e)))
                .finally(() => setBusy(false))
            }}
            value={model}
          >
            <SelectTrigger aria-label={c.model}>
              <SelectValue placeholder={c.noModels} />
            </SelectTrigger>
            <SelectContent>
              {models?.providers?.flatMap(p =>
                (p.models || []).map(value => (
                  <SelectItem disabled={p.unavailable_models?.includes(value)} key={`${p.slug}/${value}`} value={value}>
                    {value}
                  </SelectItem>
                ))
              )}
            </SelectContent>
          </Select>
          <p className="text-sm text-(--ui-text-secondary)">{c.modelHint}</p>
          {error ? (
            <div role="alert">
              <p className="text-destructive">{error}</p>
              <Button onClick={() => setReload(n => n + 1)} variant="secondary">
                {c.retry}
              </Button>
            </div>
          ) : null}
        </DialogContent>
      </Dialog>
    </>
  )
}
