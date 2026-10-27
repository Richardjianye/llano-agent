import { useStore } from '@nanostores/react'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import {
  changeCollabProjectStatus,
  removeCollabMember,
  retryCollabRepository,
  transferCollabProject,
  updateCollabProject
} from '@/hermes'
import { useI18n } from '@/i18n'
import { openExternalLink } from '@/lib/external-link'
import {
  $collabState,
  forgetCollabProject,
  loadCollabProjects,
  loadCollabSidebars,
  selectCollabProject
} from '@/store/collab'
import type { CollabProject } from '@/types/hermes'

export function ProjectManagement({ project }: { project: CollabProject }) {
  const { t } = useI18n()
  const c = t.collab.functional
  const members = useStore($collabState)[project.code]?.members ?? []
  const [open, setOpen] = useState(false)
  const [name, setName] = useState(project.name || project.code)
  const [description, setDescription] = useState(project.description || '')
  const [target, setTarget] = useState('')
  const [action, setAction] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const me = members.find(m => m.is_me && m.member_kind !== 'agent')
  const canManage = project.can_manage ?? (project.my_role === 'owner' || me?.role === 'owner')

  async function run(operation: () => Promise<unknown>, leave = false) {
    setBusy(true)
    setError('')

    try {
      await operation()

      if (leave) {
        forgetCollabProject(project.code)
        selectCollabProject('')
      }

      // A successful write must not be reported as failed because the refresh failed.
      await Promise.allSettled([loadCollabProjects(), ...(leave ? [] : [loadCollabSidebars(project.code)])])
      setAction('')
      setOpen(false)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <Button
        onClick={() => {
          setName(project.name || project.code)
          setDescription(project.description || '')
          setOpen(true)
        }}
        size="sm"
        variant="secondary"
      >
        {c.settings}
      </Button>
      <Dialog
        onOpenChange={value => {
          if (!busy) {
            setOpen(value)
            setAction('')
            setError('')
          }
        }}
        open={open}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {project.name || project.code} · {c.settings}
            </DialogTitle>
          </DialogHeader>
          {canManage ? (
            <>
              <label>
                {c.name}
                <Input disabled={busy} onChange={e => setName(e.target.value)} value={name} />
              </label>
              <label>
                {c.description}
                <Textarea disabled={busy} onChange={e => setDescription(e.target.value)} value={description} />
              </label>
              <Button
                disabled={busy || !name.trim()}
                onClick={() => void run(() => updateCollabProject(project.code, name, description))}
              >
                {c.save}
              </Button>
              <Button
                disabled={busy}
                onClick={() => setAction(project.status === 'archived' ? 'restore' : 'archive')}
                variant="secondary"
              >
                {project.status === 'archived' ? c.restore : c.archive}
              </Button>
              <Select disabled={busy} onValueChange={setTarget} value={target}>
                <SelectTrigger aria-label={c.transfer}>
                  <SelectValue placeholder={c.selectMember} />
                </SelectTrigger>
                <SelectContent>
                  {members
                    .filter(m => m.member_kind !== 'agent' && !m.is_me)
                    .map(m => (
                      <SelectItem key={m.id} value={String(m.id)}>
                        {m.username || m.user_uid}
                      </SelectItem>
                    ))}
                </SelectContent>
              </Select>
              <Button disabled={busy || !target} onClick={() => setAction('transfer')} variant="secondary">
                {c.transfer}
              </Button>
            </>
          ) : null}
          <div className="space-y-2 rounded border border-(--ui-stroke-tertiary) p-3 text-sm">
            <div className="font-medium">{c.repository}</div>
            {project.git_repo_status === 'ready' && project.git_repo_url ? (
              <Button disabled={busy} onClick={() => openExternalLink(project.git_repo_url || '')} variant="secondary">
                {c.openRepository}
              </Button>
            ) : project.git_repo_status === 'failed' ? (
              <>
                <p className="text-destructive" role="alert">
                  {c.repoFailed}
                  {project.git_repo_error ? `：${project.git_repo_error}` : ''}
                </p>
                {canManage ? (
                  <Button
                    disabled={busy}
                    onClick={() => void run(() => retryCollabRepository(project.code))}
                    variant="secondary"
                  >
                    {c.retryRepository}
                  </Button>
                ) : null}
              </>
            ) : (
              <p className="text-(--ui-text-tertiary)">
                {project.git_repo_status === 'provisioning' ? c.repoProvisioning : c.repoPending}
              </p>
            )}
          </div>
          {me ? (
            <Button disabled={busy} onClick={() => setAction('leave')} variant="destructive">
              {c.leave}
            </Button>
          ) : null}
          {action ? (
            <div className="space-y-2" role="alert">
              <p>{c.confirmHint}</p>
              <Button
                disabled={busy}
                onClick={() =>
                  void run(() => {
                    if (action === 'leave' && me) {
                      return removeCollabMember(project.code, me.id)
                    }

                    if (action === 'transfer') {
                      return transferCollabProject(project.code, Number(target))
                    }

                    return changeCollabProjectStatus(project.code, action === 'archive')
                  }, action === 'leave')
                }
              >
                {c.confirm}
              </Button>
              <Button disabled={busy} onClick={() => setAction('')} variant="secondary">
                {c.cancel}
              </Button>
            </div>
          ) : null}
          {error ? (
            <p className="text-destructive" role="alert">
              {error}
            </p>
          ) : null}
        </DialogContent>
      </Dialog>
    </>
  )
}
