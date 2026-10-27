/**
 * 把 Electron IPC 的包装从错误消息里剥掉。
 *
 * `ipcRenderer.invoke` 在主进程抛错时，会把原始错误重新包一层再拒绝，形如：
 *
 *     Error invoking remote method 'hermes:product-auth:login': ProductGatewayError: 用户名或密码错误
 *
 * 直接 `reason.message` 显示出去，员工在登录框里看到的就是上面这一整串。
 * 真正有用的只有最后那五个字，前面全是我们自己的实现细节 —— 频道名、
 * 异常类名。而这是**每个人输错一次密码就会看到的东西**。
 *
 * 剥掉包装之后，我们自己给用户看的中文原文就露出来了；万一剥完什么都不剩
 * （主进程抛的是空错误），交给调用方的兜底文案。
 */

/** Electron 的包装前缀：`Error invoking remote method '<频道>': ` */
const INVOKE_PREFIX = /^Error invoking remote method\s+'[^']*':\s*/

/**
 * 异常类名前缀，如 `ProductGatewayError: `。
 *
 * 刻意要求以 Error / Exception 结尾：普通消息里也会出现「冒号」
 * （比如「连接失败: 超时」），不加这个约束会把消息本身的第一段吃掉。
 */
const ERROR_CLASS_PREFIX = /^(?:[A-Z][A-Za-z0-9_]*)?(?:Error|Exception):\s*/

export function unwrapIpcError(raw: unknown, fallback: string): string {
  const message = raw instanceof Error ? raw.message : typeof raw === 'string' ? raw : ''

  let text = message.replace(INVOKE_PREFIX, '')

  // 主进程里再包一层的情况有过（handler 里 catch 之后重新抛），所以循环剥，
  // 但设上限 —— 一个畸形的消息不该让这里转不出来。
  for (let i = 0; i < 3; i += 1) {
    const next = text.replace(ERROR_CLASS_PREFIX, '')

    if (next === text) {break}
    text = next
  }

  const trimmed = text.trim()

  return trimmed || fallback
}
