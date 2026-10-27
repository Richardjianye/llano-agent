/**
 * 表格里的时间统一显示成北京时间「2026-09-23 17:46:09」。
 *
 * 两种来源：
 * - 网关自己落库的时间是服务器本地时间（决策 #26，库与容器都是 +08:00），接口回不带时区的
 *   ISO 串（`2026-09-23T17:46:09`）——本来就是北京时间，只去掉「T」。
 * - 签名清单里的 `released_at` 是 UTC（`2026-09-20T05:03:52Z`）——必须换算成 +08:00，
 *   否则只去掉 T 会显示成早 8 小时。换算固定用 Asia/Shanghai，不看浏览器所在时区。
 */
const SHANGHAI = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'Asia/Shanghai',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hourCycle: 'h23',
})

export function formatDateTime (value: string | null | undefined): string {
  if (!value) {
    return '—'
  }
  const text = String(value).trim()
  if (/(?:Z|[+-]\d{2}:?\d{2})$/.test(text)) {
    const date = new Date(text)
    if (!Number.isNaN(date.getTime())) {
      const parts = Object.fromEntries(SHANGHAI.formatToParts(date).map(p => [p.type, p.value]))
      return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`
    }
  }
  return text.replace('T', ' ').slice(0, 19)
}
