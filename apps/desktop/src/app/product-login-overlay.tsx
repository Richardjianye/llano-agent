import { useStore } from '@nanostores/react'
import { type FormEvent, useEffect, useRef, useState } from 'react'

import { BootProgressDetails } from '@/components/boot-progress-details'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import type {
  ProductAuthProgress,
  ProductAuthStatus,
  ProductLoginCredentials,
  ProductSavedAccount,
  ProductSavedAccountsSnapshot
} from '@/global'
import { useI18n } from '@/i18n'
import { ChevronDown, Loader2, X } from '@/lib/icons'
import { unwrapIpcError } from '@/lib/ipc-error'
import { PRODUCT_POLICY } from '@/lib/product-policy'
import { $desktopBoot } from '@/store/boot'
import { clearNotifications } from '@/store/notifications'
import { $productAuthenticated } from '@/store/product-auth'
import {
  applyGatewayReachability,
  productGatewayFailureMessage,
  resetProductGatewayReachable
} from '@/store/product-gateway'
import {
  $productModelAvailability,
  availabilityFromSync,
  refreshProductModelAvailability,
  resetProductModelAvailability,
  setProductModelAvailability
} from '@/store/product-model'

const PRODUCT_CONFIGURATION_SYNC_FAILED = 'PRODUCT_CONFIGURATION_SYNC_FAILED'
const PRODUCT_SAVED_PASSWORD_REJECTED = 'PRODUCT_SAVED_PASSWORD_REJECTED'

// 记住了密码时密码框里显示的占位。固定长度，不透露真实密码有几位；
// 真密码只在主进程里解出来（见 electron/product-saved-accounts.ts）。
const SAVED_PASSWORD_MASK = '••••••••'

// 冷启动断网时多久再试一次。平时 60 秒一轮是心跳，这里是等网络回来。
const RESTORING_POLL_MS = 5_000

function isIpcCode(message: string, code: string) {
  return message === code || message.endsWith(`: ${code}`)
}

/** 用户在「记住的密码」占位上敲键：整框换成他新输入的内容（仿 QQ）。 */
export function passwordAfterEditingMask(value: string): string {
  if (value.startsWith(SAVED_PASSWORD_MASK)) {
    return value.slice(SAVED_PASSWORD_MASK.length)
  }

  return SAVED_PASSWORD_MASK.startsWith(value) ? '' : value.replaceAll('•', '')
}

export function ProductLoginOverlay() {
  const { t } = useI18n()
  const boot = useStore($desktopBoot)
  const copy = t.productAuth
  const [authenticated, setAuthenticated] = useState<boolean | null>(null)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [usingSavedPassword, setUsingSavedPassword] = useState(false)
  const [rememberPassword, setRememberPassword] = useState(false)
  const [savedAccounts, setSavedAccounts] = useState<ProductSavedAccountsSnapshot | null>(null)
  const [accountsOpen, setAccountsOpen] = useState(false)
  const [restoring, setRestoring] = useState(false)
  const [showFormWhileRestoring, setShowFormWhileRestoring] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [dingtalkEnabled, setDingtalkEnabled] = useState(false)
  const [dingtalkPending, setDingtalkPending] = useState(false)
  const [requiresPasswordChange, setRequiresPasswordChange] = useState(false)
  const [newPassword, setNewPassword] = useState('')
  const [loginPhase, setLoginPhase] = useState<ProductAuthProgress['phase']>('verifying')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [recheckStatus, setRecheckStatus] = useState<() => void>(() => () => undefined)
  const usernameRef = useRef('')

  usernameRef.current = username

  const applyAccount = (account: ProductSavedAccount | undefined, name: string) => {
    setUsername(name)

    if (account?.hasSavedPassword) {
      setUsingSavedPassword(true)
      setPassword(SAVED_PASSWORD_MASK)
    } else {
      setUsingSavedPassword(false)
      setPassword('')
    }
  }

  const loadSavedAccounts = async (prefill: boolean) => {
    const snapshot = await window.hermesDesktop.productAuth.savedAccounts?.().catch(() => null)

    if (!snapshot) {
      return null
    }

    setSavedAccounts(snapshot)
    setRememberPassword(snapshot.rememberPassword)

    if (prefill && !usernameRef.current && snapshot.accounts[0]) {
      applyAccount(snapshot.accounts[0], snapshot.accounts[0].username)
    }

    return snapshot
  }

  const performLogin = async (credentials: ProductLoginCredentials) => {
    setSubmitting(true)
    setLoginPhase('verifying')
    setError('')

    try {
      const status = await window.hermesDesktop.productAuth.login(credentials)
      setPassword('')
      setNewPassword('')
      setRequiresPasswordChange(false)
      setUsingSavedPassword(false)
      setNotice('')
      setAuthenticated(status.authenticated)
      $productAuthenticated.set(status.authenticated)
      // The gateway may have granted this account no models at all, or we may
      // have failed to find out. Either way the chat surface opens — he might
      // only be here to read past conversations — but he must be told which of
      // the two it is before he types a message into a void.
      setProductModelAvailability(availabilityFromSync(status.models))
    } catch (reason) {
      // 直接用 reason.message 的话，员工看到的是 Electron 的 IPC 包装：
      //   Error invoking remote method 'hermes:product-auth:login': ProductGatewayError: 用户名或密码错误
      // 真正有用的只有最后五个字，前面全是频道名和异常类名 —— 而这是
      // **每个人输错一次密码就会看到的东西**。
      const message = unwrapIpcError(reason, copy.failed)

      if (message.includes('请先修改初始密码')) {setRequiresPasswordChange(true)}

      setNotice('')

      if (isIpcCode(message, PRODUCT_SAVED_PASSWORD_REJECTED)) {
        setUsingSavedPassword(false)
        setPassword('')
        setError(copy.savedPasswordRejected)
        void loadSavedAccounts(false)
      } else {
        setError(isIpcCode(message, PRODUCT_CONFIGURATION_SYNC_FAILED) ? copy.syncFailed : message)
      }
    } finally {
      setSubmitting(false)
    }
  }

  // 挂载期只订阅一次；登录动作要读最新的 state，所以每次渲染换成新的一份
  const actionsRef = useRef({ applyAccount, loadSavedAccounts, performLogin })

  actionsRef.current = { applyAccount, loadSavedAccounts, performLogin }

  useEffect(() => {
    let active = true
    // 同一次过期只自动重登一次；重新登录成功后复位
    let autoReloginTried = false
    // 冷启动断网时加快轮询，等网络一回来就进去
    let restoringNow = false

    // 登录过期（不是管理员动作）且这个账号记住了密码：自动重登一次，
    // 员工看到的只是一行「正在自动重新登录」，而不是又一次掉登录。
    const tryAutoRelogin = async (expiredUsername: string) => {
      autoReloginTried = true
      const { applyAccount, loadSavedAccounts, performLogin } = actionsRef.current
      const snapshot = await loadSavedAccounts(false)
      const account = snapshot?.accounts.find(item => item.username === expiredUsername)

      if (!active || !account?.hasSavedPassword) {
        setError(copy.sessionExpired)

        return
      }

      applyAccount(account, account.username)
      setNotice(copy.autoRelogin)
      await performLogin({
        password: '',
        rememberPassword: true,
        useSavedPassword: true,
        username: account.username
      })
    }

    const applyStatus = (status: ProductAuthStatus) => {
      if (!active) {
        return
      }

      // 网关连不上时横幅带一个「重试」按钮：固定 60 秒轮询是节奏，
      // 这个按钮才是用户点得到的出口
      applyGatewayReachability(status.gatewayReachable, {
        failureKind: status.gatewayFailure,
        onRetry: () => checkStatus()
      })

      setAuthenticated(status.authenticated)
      $productAuthenticated.set(status.authenticated)
      restoringNow = !status.authenticated && status.sessionStored === true
      setRestoring(restoringNow)

      if (status.authenticated) {
        autoReloginTried = false

        // A session restored from disk never runs the login flow, so nobody has
        // asked the gateway which models this account may use. Ask once per
        // launch — otherwise the banner only appears after his first message
        // silently fails to send. `unknown` also gates the periodic re-poll
        // below from turning this into a gateway heartbeat.
        if ($productModelAvailability.get() === 'unknown') {
          void refreshProductModelAvailability()
        }
      } else {
        // A signed-out screen must not carry the previous account's model
        // banner into the next sign-in.
        resetProductModelAvailability()

        // 登录页自己会显示连不上的提示，不需要横幅再说一遍
        if (status.gatewayReachable !== false) {
          resetProductGatewayReachable()
        }

        if (status.gatewayReachable === false && !status.reason) {
          // 「连不上网关」和「你的账号不能用了」要给相反的指示。
          // 把网络故障说成账号问题，用户会去找管理员，管理员一看账号好好的
          setError(productGatewayFailureMessage(status.gatewayFailure))
        } else if (status.reason === 'session-invalid') {
          // Gateway messages are useful diagnostic detail but can contain
          // terse/English credential errors. A revoked device or disabled
          // account is a deliberate administrator action, so present one
          // stable business-facing instruction and keep the raw error in the
          // Electron logs.
          setError(copy.sessionInvalid)
          // A boot/config error can race the authoritative auth check. It is
          // stale once an administrator-forced sign-out is confirmed and must
          // not float above the login instructions as a second diagnosis.
          clearNotifications()
        } else if (status.reason === 'session-expired') {
          if (status.expiredUsername && !autoReloginTried) {
            void tryAutoRelogin(status.expiredUsername)
          } else if (!autoReloginTried) {
            setError(copy.sessionExpired)
          }
        } else if (status.revocationPending) {
          setError(copy.logoutPending)
        }
      }
    }

    const checkStatus = (initial = false) => {
      void window.hermesDesktop.productAuth
        .status()
        .then(applyStatus)
        .catch(() => {
          // A gateway outage must not masquerade as an invalid account. Keep
          // the current screen and let normal connection recovery own it.
          if (active && initial) {
            setAuthenticated(false)
            $productAuthenticated.set(false)
            setError(copy.failed)
          }
        })
    }

    setRecheckStatus(() => () => checkStatus())
    void window.hermesDesktop.productAuth
      .dingtalkConfig?.()
      .then(enabled => {
        if (active) {setDingtalkEnabled(enabled)}
      })
      .catch(() => undefined)
    checkStatus(true)
    void actionsRef.current.loadSavedAccounts(true)
    const unsubscribe = window.hermesDesktop.productAuth.onChanged(applyStatus)

    // During desktop development the renderer can hot-reload a moment before
    // Electron reloads the updated preload. Keep that brief mixed-version
    // window usable; packaged builds always provide the progress channel.
    const unsubscribeProgress =
      window.hermesDesktop.productAuth.onProgress?.(progress => setLoginPhase(progress.phase)) ?? (() => undefined)

    const interval = window.setInterval(checkStatus, 60_000)

    const restoringInterval = window.setInterval(() => {
      if (restoringNow) {
        checkStatus()
      }
    }, RESTORING_POLL_MS)

    const checkOnFocus = () => checkStatus()

    window.addEventListener('focus', checkOnFocus)

    return () => {
      active = false
      unsubscribe()
      unsubscribeProgress()
      window.clearInterval(interval)
      window.clearInterval(restoringInterval)
      window.removeEventListener('focus', checkOnFocus)
    }
    // 账号与登录动作走 actionsRef，这里只跟文案变
  }, [copy.autoRelogin, copy.failed, copy.logoutPending, copy.sessionExpired, copy.sessionInvalid])

  if (authenticated === true) {
    return null
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()

    if (submitting || dingtalkPending || !username.trim() || (!usingSavedPassword && !password)) {
      return
    }

    await performLogin({
      password: usingSavedPassword ? '' : password,
      rememberPassword: Boolean(savedAccounts?.canRememberPassword && rememberPassword),
      useSavedPassword: usingSavedPassword,
      ...(requiresPasswordChange ? { newPassword } : {}),
      username
    })
  }

  const onUsernameChange = (value: string) => {
    const match = savedAccounts?.accounts.find(item => item.username === value.trim())

    if (match?.hasSavedPassword) {
      applyAccount(match, value)
    } else {
      setUsername(value)

      if (usingSavedPassword) {
        setUsingSavedPassword(false)
        setPassword('')
      }
    }
  }

  const onPasswordChange = (value: string) => {
    if (usingSavedPassword) {
      setUsingSavedPassword(false)
      setPassword(passwordAfterEditingMask(value))

      return
    }

    setPassword(value)
  }

  const forgetAccount = async (name: string) => {
    const snapshot = await window.hermesDesktop.productAuth.forgetAccount?.(name).catch(() => null)

    if (snapshot) {
      setSavedAccounts(snapshot)
    }

    if (username === name) {
      applyAccount(undefined, '')
    }
  }

  const accounts = savedAccounts?.accounts ?? []

  const signInDingtalk = async () => {
    if (submitting || dingtalkPending) {return}
    setDingtalkPending(true)
    setLoginPhase('verifying')
    setError('')

    try {
      const status = await window.hermesDesktop.productAuth.dingtalkLogin?.()

      if (status) {
        setAuthenticated(status.authenticated)
        $productAuthenticated.set(status.authenticated)
        setProductModelAvailability(availabilityFromSync(status.models))
      }
    } catch (reason) {
      const message = unwrapIpcError(reason, copy.failed)
      setError(isIpcCode(message, PRODUCT_CONFIGURATION_SYNC_FAILED) ? copy.syncFailed : message)
    } finally {
      setDingtalkPending(false)
    }
  }

  return (
    <div
      className="fixed inset-0 z-[100] grid place-items-center bg-(--ui-chat-surface-background) px-6"
      data-overlay-surface=""
    >
      {authenticated === null ? (
        <Loader2 className="size-5 animate-spin text-muted-foreground" />
      ) : restoring && !showFormWhileRestoring ? (
        <div
          aria-live="polite"
          className="w-full max-w-sm rounded-2xl border border-(--ui-stroke-secondary) bg-background/80 p-7 text-center shadow-lg"
          role="status"
        >
          <p className="text-xs font-medium tracking-wide text-(--ui-accent)">{PRODUCT_POLICY.productName}</p>
          <Loader2 className="mx-auto mt-5 size-5 animate-spin text-muted-foreground" />
          <h1 className="mt-4 text-lg font-semibold text-foreground">{copy.restoringTitle}</h1>
          <p className="mt-2 text-sm text-muted-foreground">{copy.restoringBody}</p>
          <Button className="mt-6 w-full" onClick={recheckStatus} type="button">
            {copy.restoringRetry}
          </Button>
          <button
            className="mt-3 text-xs text-muted-foreground underline-offset-4 hover:underline"
            onClick={() => setShowFormWhileRestoring(true)}
            type="button"
          >
            {copy.switchAccount}
          </button>
        </div>
      ) : (
        <form
          className="w-full max-w-sm rounded-2xl border border-(--ui-stroke-secondary) bg-background/80 p-7 shadow-lg"
          onSubmit={submit}
        >
          <p className="text-xs font-medium tracking-wide text-(--ui-accent)">{PRODUCT_POLICY.productName}</p>
          <h1 className="mt-3 text-xl font-semibold text-foreground">{copy.title}</h1>
          <p className="mt-1 text-sm text-muted-foreground">{copy.subtitle}</p>
          {error && (
            <div aria-live="assertive" className="mt-4 rounded-md border border-(--ui-stroke-secondary) bg-(--ui-bg-hover) p-3" role="alert">
              <p className="text-sm font-medium">{error.includes('尚未关联') || error.includes('尚未绑定') ? copy.dingtalkUnboundTitle : copy.loginFailed}</p>
              <p className="mt-2 text-sm text-destructive">{error}</p>
              {(error.includes('尚未关联') || error.includes('尚未绑定')) && (
                <Button className="mt-3" onClick={() => document.getElementById('product-username')?.focus()} type="button" variant="outline">{copy.passwordFallback}</Button>
              )}
            </div>
          )}

          {(submitting || dingtalkPending) && loginPhase === 'syncing' ? <BootProgressDetails boot={boot} className="mt-5" /> : null}
          {dingtalkEnabled && (
            <>
              <Button
                className="mt-6 w-full"
                disabled={submitting || dingtalkPending}
                onClick={() => void signInDingtalk()}
                type="button"
              >
                {dingtalkPending && <Loader2 className="size-3.5 animate-spin" />}
                {dingtalkPending ? (loginPhase === 'syncing' ? copy.syncing : copy.dingtalkWaiting) : copy.dingtalkLogin}
              </Button>
              {dingtalkPending && (
                <Button
                  className="mt-2 w-full"
                  onClick={() => void window.hermesDesktop.productAuth.cancelDingtalk?.()}
                  type="button"
                  variant="ghost"
                >
                  {copy.cancelDingtalk}
                </Button>
              )}
            </>
          )}

          <label className="mt-6 block text-xs font-medium text-foreground" htmlFor="product-username">
            {copy.username}
          </label>
          <div className="relative mt-2">
            <Input
              autoComplete="username"
              autoFocus
              className={accounts.length ? 'w-full pr-9' : 'w-full'}
              disabled={submitting || dingtalkPending}
              id="product-username"
              onChange={event => onUsernameChange(event.target.value)}
              value={username}
            />
            {accounts.length > 0 && (
              <button
                aria-expanded={accountsOpen}
                aria-label={copy.recentAccounts}
                className="absolute inset-y-0 right-0 grid w-9 place-items-center text-muted-foreground hover:text-foreground"
                disabled={submitting || dingtalkPending}
                onClick={() => setAccountsOpen(open => !open)}
                type="button"
              >
                <ChevronDown className="size-4" />
              </button>
            )}
            {accountsOpen && accounts.length > 0 && (
              <ul
                aria-label={copy.recentAccounts}
                className="absolute inset-x-0 top-full z-10 mt-1 overflow-hidden rounded-md border border-(--ui-stroke-secondary) bg-background shadow-md"
              >
                {accounts.map(account => (
                  <li className="flex items-center" key={account.username}>
                    <button
                      className="min-w-0 flex-1 truncate px-3 py-2 text-left text-sm hover:bg-(--ui-bg-hover)"
                      onClick={() => {
                        applyAccount(account, account.username)
                        setAccountsOpen(false)
                      }}
                      type="button"
                    >
                      {account.username}
                    </button>
                    <button
                      aria-label={copy.removeAccount(account.username)}
                      className="grid size-8 place-items-center text-muted-foreground hover:text-foreground"
                      onClick={() => void forgetAccount(account.username)}
                      type="button"
                    >
                      <X className="size-3.5" />
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <label className="mt-4 block text-xs font-medium text-foreground" htmlFor="product-password">
            {copy.password}
          </label>
          <Input
            autoComplete={usingSavedPassword ? 'off' : 'current-password'}
            className="mt-2 w-full"
            disabled={submitting || dingtalkPending}
            id="product-password"
            onChange={event => onPasswordChange(event.target.value)}
            type="password"
            value={password}
          />

          {savedAccounts?.canRememberPassword && (
            <div className="mt-3 flex items-center gap-2">
              <Checkbox
                checked={rememberPassword}
                disabled={submitting || dingtalkPending}
                id="product-remember-password"
                onCheckedChange={checked => setRememberPassword(checked === true)}
              />
              <label className="text-xs text-muted-foreground" htmlFor="product-remember-password">
                {copy.rememberPassword}
              </label>
            </div>
          )}

          {notice && !error && <p className="mt-3 text-xs text-muted-foreground">{notice}</p>}
          {requiresPasswordChange && (
            <>
              <label className="mt-4 block text-xs font-medium text-foreground" htmlFor="product-new-password">
                {copy.newPassword}
              </label>
              <Input
                autoComplete="new-password"
                className="mt-2 w-full"
                disabled={submitting || dingtalkPending}
                id="product-new-password"
                onChange={event => setNewPassword(event.target.value)}
                type="password"
                value={newPassword}
              />
            </>
          )}


          <Button
            className="mt-6 w-full"
            disabled={submitting || dingtalkPending || !username.trim() || (!usingSavedPassword && !password) || (requiresPasswordChange && newPassword.length < 8)}
            type="submit"
          >
            {submitting && <Loader2 className="size-3.5 animate-spin" />}
            {submitting ? (loginPhase === 'syncing' ? copy.syncing : copy.signingIn) : copy.submit}
          </Button>
        </form>
      )}
    </div>
  )
}
