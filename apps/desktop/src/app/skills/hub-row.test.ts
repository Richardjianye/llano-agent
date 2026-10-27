import { describe, expect, it } from 'vitest'

import { hubByline } from './hub-row'

const publishedBy = (owner: string, when: string) => [owner, when && `${when} 发布`].filter(Boolean).join(' · ')

describe('hubByline', () => {
  it('shows who published the company skill and when, in Beijing time to the minute', () => {
    expect(hubByline({ owner: 'demo-admin', published_at: '2026-09-23T15:39:06' }, publishedBy)).toBe(
      'demo-admin · 2026-09-23 15:39 发布'
    )
  })

  it('drops whichever half is missing, and says nothing for public-hub skills', () => {
    expect(hubByline({ owner: 'demo-admin', published_at: '' }, publishedBy)).toBe('demo-admin')
    expect(hubByline({ owner: '', published_at: '2026-09-23T15:39:06' }, publishedBy)).toBe('2026-09-23 15:39 发布')
    expect(hubByline({}, publishedBy)).toBe('')
  })
})
