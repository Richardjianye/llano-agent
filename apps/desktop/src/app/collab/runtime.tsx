import { useStore } from '@nanostores/react'
import { useEffect } from 'react'

import { resetCollab } from '@/store/collab'
import { startCollabRuntime } from '@/store/collab-runtime'
import { $productAuthenticated } from '@/store/product-auth'

/** Mounted once by the application shell, never by a selected project. */
export function CollabRuntime() {
  const authenticated = useStore($productAuthenticated)
  useEffect(() => {
    if (authenticated === true) {
      return startCollabRuntime()
    }

    if (authenticated === false) {
      void resetCollab()
    }
  }, [authenticated])

  return null
}
