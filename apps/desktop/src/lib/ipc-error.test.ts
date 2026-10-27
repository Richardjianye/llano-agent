import { describe, expect, it } from 'vitest'

import { unwrapIpcError } from './ipc-error'

describe('unwrapIpcError', () => {
  it('剥掉 Electron 的包装，只留下我们自己写给用户的话', () => {
    // 这一串是真机上实测到的原文（test 包，2026-08-26）。
    const raw = new Error(
      "Error invoking remote method 'hermes:product-auth:login': ProductGatewayError: 用户名或密码错误",
    )

    expect(unwrapIpcError(raw, '登录失败')).toBe('用户名或密码错误')
  })

  it('没有包装时原样返回', () => {
    expect(unwrapIpcError(new Error('网络不可达'), '登录失败')).toBe('网络不可达')
  })

  it('钉钉 IPC 的普通 Error 包装不显示在业务提示中', () => {
    const message = '此钉钉账号尚未关联工作台账号。请使用账号密码登录后绑定。'
    const raw = new Error(`Error invoking remote method 'hermes:product-auth:dingtalk-login': Error: ${message}`)

    expect(unwrapIpcError(raw, '登录失败')).toBe(message)
    expect(unwrapIpcError(new Error('Error: Error: ' + message), '登录失败')).toBe(message)
    expect(unwrapIpcError(new Error('Exception: '), '登录失败')).toBe('登录失败')
  })

  it('不吃掉消息本身的冒号', () => {
    // 「连接失败」不以 Error/Exception 结尾，不该被当成异常类名剥掉 ——
    // 剥了的话用户只看到「超时」，丢掉了「连接」这个关键信息。
    expect(unwrapIpcError(new Error('连接失败: 超时'), '登录失败')).toBe('连接失败: 超时')
  })

  it('剥掉嵌套的异常类名', () => {
    const raw = new Error(
      "Error invoking remote method 'x': WrapError: ProductGatewayError: 账号已被停用",
    )

    expect(unwrapIpcError(raw, '登录失败')).toBe('账号已被停用')
  })

  it('剥完什么都不剩时用兜底文案', () => {
    const raw = new Error("Error invoking remote method 'x': ProductGatewayError: ")

    expect(unwrapIpcError(raw, '登录失败')).toBe('登录失败')
  })

  it('不是 Error 也不炸', () => {
    expect(unwrapIpcError(undefined, '登录失败')).toBe('登录失败')
    expect(unwrapIpcError('直接是字符串', '登录失败')).toBe('直接是字符串')
  })
})
