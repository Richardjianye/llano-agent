import { cn } from '@/lib/utils'

const assetPath = (path: string) => `${import.meta.env.BASE_URL}${path.replace(/^\/+/, '')}`

// Brand badge: 产品 Logo，白底方块上，明暗主题一致。
// Fills the tile (softly rounded); size via className (default size-14)。
//
// 这里原本是上游的 `nous-girl.jpg`。它出现在关于页、更新提示（含强制更新的
// 全屏遮罩）、首次运行、安装引导 —— 五处用户一定看得到的地方，却活过了
// 「修完安装包残留 Hermes/Nous 元数据」那一轮，因为那轮查的是元数据字段，
// 不是图片资源。2026-08-28 是从一张更新提示的截图里发现的。
export function BrandMark({ className, ...props }: React.ComponentProps<'span'>) {
  return (
    <span
      className={cn(
        'inline-flex size-14 shrink-0 items-center justify-center overflow-hidden rounded-md bg-white',
        className
      )}
      {...props}
    >
      <img alt="" className="size-full object-contain" src={assetPath('brand-mark.png')} />
    </span>
  )
}
