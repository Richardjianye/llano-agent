import { useStore } from '@nanostores/react'

import { $backdrop, $backdropImage, $backdropOpacity } from '@/store/backdrop'

const assetPath = (path: string) => `${import.meta.env.BASE_URL}${path.replace(/^\/+/, '')}`

export function Backdrop() {
  const on = useStore($backdrop)
  const custom = useStore($backdropImage)
  const opacity = useStore($backdropOpacity)

  if (!on) {
    return null
  }

  // A user's own photo is meant to be SEEN, so it gets plain opacity and no
  // colour tricks. The built-in statue keeps the tuned 2.5% difference-blend
  // wash it shipped with — running it through the slider instead would change
  // the default look for everyone who never picks an image.
  if (custom) {
    return (
      <div aria-hidden className="pointer-events-none absolute inset-0 z-2" style={{ opacity: opacity / 100 }}>
        <img alt="" className="size-full object-cover object-center" fetchPriority="low" src={custom} />
      </div>
    )
  }

  return (
    <div aria-hidden className="pointer-events-none absolute inset-0 z-2 opacity-[0.025] mix-blend-difference">
      <img
        alt=""
        className="h-[160dvh] w-auto min-w-dvw object-cover object-left-top [filter:invert(var(--backdrop-invert-mul,1))]"
        fetchPriority="low"
        src={assetPath('ds-assets/filler-bg0.jpg')}
      />
    </div>
  )
}
