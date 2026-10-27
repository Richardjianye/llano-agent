import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { IS_BRANDED_PRODUCT } from '@/lib/product-policy'

type Report = Awaited<ReturnType<NonNullable<Window['hermesDesktop']['runtimeDoctor']>>>

export function RuntimeEnvironmentPanel() {
  const [report, setReport] = useState<Report | null>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')

  if (!IS_BRANDED_PRODUCT || !navigator.userAgent.includes('Windows') || !window.hermesDesktop?.runtimeDoctor) {
    return null
  }

  const run = async (repair: boolean) => {
    setBusy(true)
    setMessage(repair ? '正在准备运行环境，请保持网络连接…' : '正在检查运行环境…')

    try {
      const next = await window.hermesDesktop.runtimeDoctor!(repair)
      setReport(next)
      setMessage(repair ? '修复检查已完成。新增环境在重启工作台后生效。' : '环境检查已完成。')
    } catch (error) {
      setMessage(String(error))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="rounded-lg border p-4">
      <div className="mb-2 font-medium">运行环境</div>
      <p className="mb-3 text-sm text-muted-foreground">
        检查终端、Python、浏览器等依赖。缺失时优先从公司内网获取，并校验文件完整性。
      </p>
      <div className="flex gap-2">
        <Button disabled={busy} onClick={() => void run(false)} size="sm" variant="outline">
          检查环境
        </Button>
        <Button disabled={busy} onClick={() => void run(true)} size="sm" variant="outline">
          修复缺失项
        </Button>
      </div>
      {message && (
        <p className="mt-3 text-sm" role="status">
          {message}
        </p>
      )}
      {report && (
        <>
          <p className="mt-2 text-sm text-muted-foreground">
            公司内网：{report.intranet ? '可连接' : '暂不可连接，将尝试备用来源'}
          </p>
          <ul className="mt-2 space-y-2 text-sm">
            {report.items.map(item => (
              <li key={item.id}>
                <span className={item.ok ? '' : 'text-destructive'}>
                  {item.label}：{item.ok ? '正常' : '需要修复'}
                </span>
                {!item.ok && (
                  <details className="mt-1 text-muted-foreground">
                    <summary>查看原因</summary>
                    <p className="break-all">{item.detail}</p>
                  </details>
                )}
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  )
}
