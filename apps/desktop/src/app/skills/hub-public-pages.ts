import { useInfiniteQuery } from '@tanstack/react-query'
import { useId, useState } from 'react'

import { getSkillHubCatalog } from '@/hermes'

const CACHE_KEY = 'skillhub-public-page-counts-v1'
const MAX_AGE = 24 * 60 * 60 * 1000

type Section = 'recommended' | 'packages'

function cachedPages(section: Section): number {
  try {
    const entry = JSON.parse(localStorage.getItem(CACHE_KEY) ?? '{}')[section]

    return entry && Date.now() - entry.at < MAX_AGE && Number.isSafeInteger(entry.pages) && entry.pages > 0
      ? entry.pages
      : 1
  } catch {
    return 1
  }
}

function rememberPages(section: Section, total: number, size: number) {
  if (!Number.isSafeInteger(total) || total < 0) {
    return
  }

  try {
    const cache = JSON.parse(localStorage.getItem(CACHE_KEY) ?? '{}')
    cache[section] = { pages: Math.max(1, Math.ceil(total / size)), at: Date.now() }
    localStorage.setItem(CACHE_KEY, JSON.stringify(cache))
  } catch {
    // Browsing remains available when storage is disabled.
  }
}

/** Each visit chooses one start page, then follows the source's natural order. */
export function usePublicCatalog(section: Section, size: number, profile: string | null) {
  const visit = useId()
  const [start] = useState(() => Math.floor(Math.random() * cachedPages(section)) * size)

  return useInfiniteQuery({
    queryKey: ['skill-hub-catalog', profile, section, visit, start],
    queryFn: async ({ pageParam }) => {
      let page = await getSkillHubCatalog(section, pageParam)
      rememberPages(section, page.total, size)

      // The cached total may be older than a newly reduced catalog.
      if (pageParam === start && start > 0 && pageParam >= page.total) {
        page = await getSkillHubCatalog(section, 0)
        rememberPages(section, page.total, size)

        return { ...page, offset: 0 }
      }

      return { ...page, offset: pageParam }
    },
    initialPageParam: start,
    getNextPageParam: page => {
      const items = section === 'recommended' ? page.skills : page.packages
      const next = page.offset + size

      return items.length > 0 && next < page.total ? next : undefined
    },
    staleTime: 300_000,
    refetchOnWindowFocus: false
  })
}
