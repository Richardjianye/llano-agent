import { type ExternalStoreAdapter, useExternalStoreRuntime } from '@assistant-ui/react'
import { useMemo } from 'react'

import { cacheRuntimeSnapshot } from '@/lib/incremental-external-store-runtime'

// The stock runtime allocates its lazy API snapshots before tap subscribes.
// Apply the same compatibility shim as the app's incremental runtime while
// retaining the stock core as an independent rendering/editing control.
export function useStableStockRuntime<T>(adapter: ExternalStoreAdapter<T>) {
  const runtime = useExternalStoreRuntime(adapter)

  return useMemo(() => {
    cacheRuntimeSnapshot(runtime.threads)
    cacheRuntimeSnapshot(runtime.thread)
    cacheRuntimeSnapshot(runtime.thread.composer)

    return runtime
  }, [runtime])
}
