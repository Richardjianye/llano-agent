import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import { $desktopBoot } from '@/store/boot'
import { $notifications, notify } from '@/store/notifications'
import { $productAuthenticated } from '@/store/product-auth'
import { $productModelAvailability, resetProductModelAvailability } from '@/store/product-model'

import { ProductLoginOverlay } from './product-login-overlay'

afterEach(() => {
  cleanup()
  $productAuthenticated.set(null)
  resetProductModelAvailability()
})

const banner = () => $notifications.get().find(item => item.id === 'product-model-availability')

describe('ProductLoginOverlay', () => {
  it('shows business-facing forced sign-out copy instead of a raw credential error', async () => {
    const rawGatewayError = 'API Key revoked (HTTP 401): gateway_auth_failed'
    const original = window.hermesDesktop

    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {
        productAuth: {
          login: vi.fn(),
          logout: vi.fn(),
          onProgress: vi.fn(() => () => undefined),
          onChanged: vi.fn(() => () => undefined),
          status: vi.fn(async () => ({
            authenticated: false,
            message: rawGatewayError,
            reason: 'session-invalid',
            username: null
          }))
        }
      }
    })

    try {
      notify({ kind: 'error', message: 'Sign in before starting Hermes', title: 'Desktop boot failed' })
      render(<ProductLoginOverlay />)

      await waitFor(() => expect($productAuthenticated.get()).toBe(false))
      expect($notifications.get()).toEqual([])
      expect(screen.queryByText(rawGatewayError)).toBeNull()
      expect(screen.getByText(/credentials are no longer valid|登录凭证已失效/)).toBeTruthy()
      expect(screen.queryByText(/Hermes.*(?:failed|无法启动)/i)).toBeNull()
    } finally {
      Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: original })
    }
  })

  it('distinguishes credential verification from configuration sync', async () => {
    const original = window.hermesDesktop
    let reportProgress: ((progress: { phase: 'syncing' | 'verifying' }) => void) | undefined
    let finishLogin: ((status: { authenticated: boolean; username: string }) => void) | undefined

    const login = vi.fn(
      () =>
        new Promise<{ authenticated: boolean; username: string }>(resolve => {
          finishLogin = resolve
        })
    )

    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {
        productAuth: {
          login,
          logout: vi.fn(),
          onProgress: vi.fn(callback => {
            reportProgress = callback

            return () => undefined
          }),
          onChanged: vi.fn(() => () => undefined),
          status: vi.fn(async () => ({ authenticated: false, username: null }))
        }
      }
    })

    try {
      render(<ProductLoginOverlay />)
      await screen.findByLabelText(/Username|用户名/)
      fireEvent.change(screen.getByLabelText(/Username|用户名/), { target: { value: 'doupa' } })
      fireEvent.change(screen.getByLabelText(/Password|密码/), { target: { value: 'secret' } })
      fireEvent.click(screen.getByRole('button', { name: /Sign in|登录/ }))

      expect(await screen.findByText(/Verifying account|校验账号密码/)).toBeTruthy()
      act(() => {
        $desktopBoot.set({
          ...$desktopBoot.get(),
          message: 'Using C:\\Users\\alice\\private\\python.exe',
          phase: 'runtime.windows.prepare',
          progress: 47,
          running: true,
          visible: true
        })
        reportProgress?.({ phase: 'syncing' })
      })
      expect(await screen.findByText(/Syncing configuration|同步配置信息/)).toBeTruthy()
      expect(screen.getByText('Preparing the local runtime')).toBeTruthy()
      expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBe('47')
      expect(screen.queryByText(/C:\\Users\\alice/)).toBeNull()
      expect(screen.getByText(/^Please wait a moment$/)).toBeTruthy()

      await act(async () => finishLogin?.({ authenticated: true, username: 'doupa' }))
    } finally {
      Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: original })
    }
  })

  it('maps a post-auth runtime failure code to safe copy without diagnostic paths', async () => {
    const original = window.hermesDesktop
    const rawPath = 'C:\\Users\\alice\\private\\python.exe'

    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {
        productAuth: {
          login: vi.fn(async () => {
            throw new Error(
              "Error invoking remote method 'hermes:product-auth:login': Error: PRODUCT_CONFIGURATION_SYNC_FAILED"
            )
          }),
          logout: vi.fn(),
          onProgress: vi.fn(() => () => undefined),
          onChanged: vi.fn(() => () => undefined),
          status: vi.fn(async () => ({ authenticated: false, username: null }))
        }
      }
    })

    try {
      render(<ProductLoginOverlay />)
      await screen.findByLabelText(/Username|用户名/)
      fireEvent.change(screen.getByLabelText(/Username|用户名/), { target: { value: 'doupa' } })
      fireEvent.change(screen.getByLabelText(/Password|密码/), { target: { value: rawPath } })
      fireEvent.click(screen.getByRole('button', { name: /Sign in|登录/ }))

      expect(await screen.findByText(/local runtime could not start|本地运行环境未能启动/)).toBeTruthy()
      expect(screen.queryByText(/C:\\Users\\alice/)).toBeNull()
      expect(screen.queryByText('PRODUCT_CONFIGURATION_SYNC_FAILED')).toBeNull()
    } finally {
      Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: original })
    }
  })

  it('登录成功但账号没开通模型：照样放他进主界面，同时明确告诉他去找谁', async () => {
    // 不让他进的话，只是想翻历史对话的人也被挡在外面，那是另一类投诉
    const original = window.hermesDesktop

    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {
        productAuth: {
          login: vi.fn(async () => ({
            authenticated: true,
            username: 'doupa',
            models: { reason: 'no-grants', model: null }
          })),
          logout: vi.fn(),
          onProgress: vi.fn(() => () => undefined),
          onChanged: vi.fn(() => () => undefined),
          status: vi.fn(async () => ({ authenticated: false, username: null }))
        }
      }
    })

    try {
      render(<ProductLoginOverlay />)
      await screen.findByLabelText(/Username|用户名/)
      fireEvent.change(screen.getByLabelText(/Username|用户名/), { target: { value: 'doupa' } })
      fireEvent.change(screen.getByLabelText(/Password|密码/), { target: { value: 'secret' } })
      fireEvent.click(screen.getByRole('button', { name: /Sign in|登录/ }))

      await waitFor(() => expect($productAuthenticated.get()).toBe(true))
      expect($productModelAvailability.get()).toBe('no-grants')
      expect(banner()?.message).toMatch(/administrator|管理员/)
    } finally {
      Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: original })
    }
  })

  it('登录时没问出来（网关抖动）说的是重试，不是「联系管理员开通」', async () => {
    // 说成权限问题的话，他找过去管理员一看权限明明有，两边都白跑一趟
    const original = window.hermesDesktop

    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {
        productAuth: {
          login: vi.fn(async () => ({
            authenticated: true,
            username: 'doupa',
            models: { reason: 'unavailable', model: null }
          })),
          logout: vi.fn(),
          onProgress: vi.fn(() => () => undefined),
          onChanged: vi.fn(() => () => undefined),
          status: vi.fn(async () => ({ authenticated: false, username: null }))
        }
      }
    })

    try {
      render(<ProductLoginOverlay />)
      await screen.findByLabelText(/Username|用户名/)
      fireEvent.change(screen.getByLabelText(/Username|用户名/), { target: { value: 'doupa' } })
      fireEvent.change(screen.getByLabelText(/Password|密码/), { target: { value: 'secret' } })
      fireEvent.click(screen.getByRole('button', { name: /Sign in|登录/ }))

      await waitFor(() => expect($productModelAvailability.get()).toBe('unavailable'))
      expect(banner()?.message).not.toMatch(/administrator|管理员/)
    } finally {
      Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: original })
    }
  })

  it('恢复出来的会话没跑登录流程，仍然会问一次有没有可用模型', async () => {
    // 不问的话，横幅要等到他第一条消息发不出去才出现
    const original = window.hermesDesktop
    const ensure = vi.fn(async () => ({ status: 'no-grants' }))

    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {
        productModel: { ensure },
        productAuth: {
          login: vi.fn(),
          logout: vi.fn(),
          onProgress: vi.fn(() => () => undefined),
          onChanged: vi.fn(() => () => undefined),
          status: vi.fn(async () => ({ authenticated: true, username: 'doupa' }))
        }
      }
    })

    try {
      render(<ProductLoginOverlay />)

      await waitFor(() => expect($productModelAvailability.get()).toBe('no-grants'))
      // 60 秒轮询和窗口 focus 都会再调 status()；那是登录态检查，
      // 不该顺带把网关模型清单也变成一个心跳
      expect(ensure).toHaveBeenCalledTimes(1)
    } finally {
      Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: original })
    }
  })

  it('输错密码时，界面上不该出现 Electron 的 IPC 包装', async () => {
    // 真机实测到的原文（test 包，2026-08-26）：整串都被直接塞进了登录框，
    // 而员工需要看的只有最后五个字。这是**每个人输错一次密码就会看到的东西**。
    const wrapped = new Error(
      "Error invoking remote method 'hermes:product-auth:login': ProductGatewayError: 用户名或密码错误"
    )

    const original = window.hermesDesktop

    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {
        productAuth: {
          login: vi.fn(async () => {
            throw wrapped
          }),
          logout: vi.fn(),
          onProgress: vi.fn(() => () => undefined),
          onChanged: vi.fn(() => () => undefined),
          status: vi.fn(async () => ({ authenticated: false, username: null }))
        }
      }
    })

    try {
      render(<ProductLoginOverlay />)
      await screen.findByLabelText(/Username|用户名/)
      fireEvent.change(screen.getByLabelText(/Username|用户名/), { target: { value: 'x' } })
      fireEvent.change(screen.getByLabelText(/Password|密码/), { target: { value: 'wrong' } })
      fireEvent.click(screen.getByRole('button', { name: /Sign in|登录/ }))

      const shown = await screen.findByText(/用户名或密码错误/)

      expect(shown.textContent).toBe('用户名或密码错误')
      expect(shown.textContent).not.toMatch(/invoking remote method/)
      expect(shown.textContent).not.toMatch(/ProductGatewayError/)
      expect(shown.textContent).not.toMatch(/hermes:product-auth/)
    } finally {
      Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: original })
    }
  })
})

describe('记住密码与自动恢复登录', () => {
  beforeAll(() => {
    // 「记住密码」是 Radix Checkbox，jsdom 没有 ResizeObserver
    vi.stubGlobal(
      'ResizeObserver',
      class {
        observe() {}
        unobserve() {}
        disconnect() {}
      }
    )
  })

  const stub = (overrides: Record<string, unknown> = {}) => {
    const productAuth = {
      login: vi.fn(async () => ({ authenticated: true, username: 'demo-admin' })),
      logout: vi.fn(),
      onProgress: vi.fn(() => () => undefined),
      onChanged: vi.fn(() => () => undefined),
      status: vi.fn(async () => ({ authenticated: false, username: null })),
      savedAccounts: vi.fn(async () => ({
        accounts: [
          { hasSavedPassword: true, username: 'demo-admin' },
          { hasSavedPassword: false, username: '悟空' }
        ],
        canRememberPassword: true,
        rememberPassword: true
      })),
      forgetAccount: vi.fn(async () => ({ accounts: [], canRememberPassword: true, rememberPassword: true })),
      ...overrides
    }

    const original = window.hermesDesktop
    Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: { productAuth } })

    return { productAuth, restore: () => Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: original }) }
  }

  const passwordInput = () => screen.getByLabelText(/^(Password|密码)$/) as HTMLInputElement

  it('预填最近的账号；记住的密码只以占位显示，登录时由主进程解出', async () => {
    const { productAuth, restore } = stub()

    try {
      render(<ProductLoginOverlay />)

      await waitFor(() => expect((screen.getByLabelText(/Username|用户名/) as HTMLInputElement).value).toBe('demo-admin'))
      expect(passwordInput().value).not.toBe('')
      expect(passwordInput().value).toMatch(/^•+$/)

      fireEvent.click(screen.getByRole('button', { name: /^(Sign in|登录)$/ }))

      await waitFor(() => expect(productAuth.login).toHaveBeenCalled())
      expect(productAuth.login).toHaveBeenCalledWith({
        password: '',
        rememberPassword: true,
        useSavedPassword: true,
        username: 'demo-admin'
      })
    } finally {
      restore()
    }
  })

  it('在占位上一改动就整框换成新输入的内容，不再用记住的密码', async () => {
    const { productAuth, restore } = stub()

    try {
      render(<ProductLoginOverlay />)
      await waitFor(() => expect(passwordInput().value).toMatch(/^•+$/))

      fireEvent.change(passwordInput(), { target: { value: `${passwordInput().value}n` } })
      expect(passwordInput().value).toBe('n')

      fireEvent.change(passwordInput(), { target: { value: 'new-pass' } })
      fireEvent.click(screen.getByRole('button', { name: /^(Sign in|登录)$/ }))

      await waitFor(() => expect(productAuth.login).toHaveBeenCalled())
      expect(productAuth.login).toHaveBeenCalledWith(
        expect.objectContaining({ password: 'new-pass', useSavedPassword: false, username: 'demo-admin' })
      )
    } finally {
      restore()
    }
  })

  it('记住的密码被拒：提示重新输入并清空密码框', async () => {
    const { restore } = stub({
      login: vi.fn(async () => {
        throw new Error("Error invoking remote method 'hermes:product-auth:login': Error: PRODUCT_SAVED_PASSWORD_REJECTED")
      })
    })

    try {
      render(<ProductLoginOverlay />)
      await waitFor(() => expect(passwordInput().value).toMatch(/^•+$/))

      fireEvent.click(screen.getByRole('button', { name: /^(Sign in|登录)$/ }))

      expect(await screen.findByText(/remembered password no longer works|记住的密码已失效/)).toBeTruthy()
      expect(passwordInput().value).toBe('')
    } finally {
      restore()
    }
  })

  it('登录过期且记住了密码：自动重登一次', async () => {
    const { productAuth, restore } = stub({
      status: vi.fn(async () => ({
        authenticated: false,
        expiredUsername: 'demo-admin',
        reason: 'session-expired',
        username: null
      }))
    })

    try {
      render(<ProductLoginOverlay />)

      await waitFor(() =>
        expect(productAuth.login).toHaveBeenCalledWith(expect.objectContaining({ useSavedPassword: true, username: 'demo-admin' }))
      )
      await waitFor(() => expect($productAuthenticated.get()).toBe(true))
    } finally {
      restore()
    }
  })

  it('登录过期但没记住密码：说清楚是过期，不说成管理员动作', async () => {
    const { productAuth, restore } = stub({
      status: vi.fn(async () => ({
        authenticated: false,
        expiredUsername: '悟空',
        reason: 'session-expired',
        username: null
      }))
    })

    try {
      render(<ProductLoginOverlay />)

      expect(await screen.findByText(/sign-in has expired|登录已过期，请重新登录/)).toBeTruthy()
      expect(productAuth.login).not.toHaveBeenCalled()
      expect(screen.queryByText(/登录凭证已失效/)).toBeNull()
    } finally {
      restore()
    }
  })

  it('冷启动断网但本机登录还在：显示「正在恢复登录」而不是登录表单', async () => {
    const { restore } = stub({
      status: vi.fn(async () => ({
        authenticated: false,
        gatewayFailure: 'connection',
        gatewayReachable: false,
        sessionStored: true,
        username: null
      }))
    })

    try {
      render(<ProductLoginOverlay />)

      expect(await screen.findByText(/Restoring your sign-in|正在恢复登录/)).toBeTruthy()
      expect(screen.queryByLabelText(/^(Password|密码)$/)).toBeNull()

      fireEvent.click(screen.getByRole('button', { name: /another account|换个账号登录/ }))
      expect(passwordInput()).toBeTruthy()
    } finally {
      restore()
    }
  })

  it('下拉里点 × 移除账号', async () => {
    const { productAuth, restore } = stub()

    try {
      render(<ProductLoginOverlay />)
      await waitFor(() => expect(screen.getByRole('button', { name: /Recent accounts|最近登录的账号/ })).toBeTruthy())

      fireEvent.click(screen.getByRole('button', { name: /Recent accounts|最近登录的账号/ }))
      fireEvent.click(screen.getByRole('button', { name: /Remove demo-admin|移除 demo-admin/ }))

      await waitFor(() => expect(productAuth.forgetAccount).toHaveBeenCalledWith('demo-admin'))
      await waitFor(() => expect((screen.getByLabelText(/Username|用户名/) as HTMLInputElement).value).toBe(''))
    } finally {
      restore()
    }
  })
})

it('offers DingTalk and returns to password login after cancelling the browser flow', async () => {
  const original = window.hermesDesktop
  let rejectLogin: (error: Error) => void = () => undefined

  const native = vi.fn(
    () =>
      new Promise((_, reject) => {
        rejectLogin = reject
      })
  )

  const cancel = vi.fn(async () => {
    rejectLogin(new Error('钉钉登录已取消'))
  })

  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: {
      productAuth: {
        status: vi.fn(async () => ({ authenticated: false })),
        dingtalkConfig: vi.fn(async () => true),
        dingtalkLogin: native,
        cancelDingtalk: cancel,
        login: vi.fn(),
        onChanged: vi.fn(() => () => undefined),
        onProgress: vi.fn(() => () => undefined)
      }
    }
  })

  try {
    render(<ProductLoginOverlay />)
    fireEvent.click(await screen.findByRole('button', { name: /^(Sign in with DingTalk|使用钉钉登录)$/ }))
    await screen.findByRole('button', { name: /Cancel DingTalk|取消钉钉/ })
    expect(screen.getByLabelText(/^(Username|用户名)$/).hasAttribute('disabled')).toBe(true)
    expect(native.mock.calls).toEqual([[]])
    fireEvent.click(screen.getByRole('button', { name: /Cancel DingTalk|取消钉钉/ }))
    await waitFor(() => expect(screen.getByLabelText(/^(Username|用户名)$/).hasAttribute('disabled')).toBe(false))
    expect(cancel).toHaveBeenCalledOnce()
    expect(screen.getByText('钉钉登录已取消')).toBeTruthy()
  } finally {
    Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: original })
  }
})

it('asks for a personal password only when the gateway requires an initial password change', async () => {
  const original = window.hermesDesktop

  const login = vi
    .fn()
    .mockRejectedValueOnce(new Error('请先修改初始密码'))
    .mockResolvedValueOnce({ authenticated: true, username: 'new-user' })

  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: {
      productAuth: {
        status: vi.fn(async () => ({ authenticated: false })),
        login,
        onChanged: vi.fn(() => () => undefined),
        onProgress: vi.fn(() => () => undefined)
      }
    }
  })

  try {
    render(<ProductLoginOverlay />)
    fireEvent.change(await screen.findByLabelText(/^(Username|用户名)$/), { target: { value: 'new-user' } })
    fireEvent.change(screen.getByLabelText(/^(Password|密码)$/), { target: { value: 'Initial-test-password' } })
    fireEvent.click(screen.getByRole('button', { name: /^(Sign in|登录)$/ }))
    const personal = await screen.findByLabelText(/Set a personal password|设置个人密码/)
    fireEvent.change(personal, { target: { value: 'Personal-test-password' } })
    fireEvent.click(screen.getByRole('button', { name: /^(Sign in|登录)$/ }))
    await waitFor(() =>
      expect(login).toHaveBeenLastCalledWith({
        username: 'new-user',
        password: 'Initial-test-password',
        newPassword: 'Personal-test-password',
        rememberPassword: false,
        useSavedPassword: false
      })
    )
  } finally {
    Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: original })
  }
})


it('keeps an unbound DingTalk error above the sign-in controls with password fallback', async () => {
  const original = window.hermesDesktop
  Object.defineProperty(window, 'hermesDesktop', {configurable:true, value:{productAuth:{
    status: vi.fn(async()=>({authenticated:false})), dingtalkConfig:vi.fn(async()=>true),
    dingtalkLogin:vi.fn(async()=>{throw new Error("Error invoking remote method 'hermes:product-auth:dingtalk-login': Error: 此钉钉账号尚未关联工作台账号。请使用账号密码登录，然后在右上角账号菜单中选择“绑定钉钉账号”。")}),
    login:vi.fn(), onChanged:vi.fn(()=>()=>undefined), onProgress:vi.fn(()=>()=>undefined)
  }}})

  try {
    render(<ProductLoginOverlay />)
    fireEvent.click(await screen.findByRole('button',{name:/^(Sign in with DingTalk|使用钉钉登录)$/}))
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('尚未关联工作台账号')
    expect(alert.textContent).not.toContain('Error:')
    expect(alert.textContent).not.toContain('invoking remote')
    fireEvent.click(screen.getByRole('button',{name:/Use account and password|使用账号密码登录/}))
    const username = screen.getByLabelText(/^(Username|用户名)$/)
    expect(username.ownerDocument.activeElement).toBe(username)
  } finally {Object.defineProperty(window,'hermesDesktop',{configurable:true,value:original})}
})
