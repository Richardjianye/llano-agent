import { describe, expect, it } from 'vitest'
import { channelImageUrl } from './channel-image'

describe('channel image delivery', () => {
  it.each(['image/png', 'image/jpeg', 'image/webp'])('preserves decoded image bytes for preview and download (%s)', mime_type => {
    const original = String.fromCodePoint(0x89, 0x50, 0x4e, 0x47, 0x0a, 0xff, 0x00)
    const url = channelImageUrl({ mime_type, b64_json: btoa(original) })
    const [header, encoded] = url.split(',')
    expect(header).toBe(`data:${mime_type};base64`)
    expect(atob(encoded!)).toEqual(original)
    expect(url).not.toContain('\n')
  })
  it('never turns an unrecognized response into a remote or executable URL', () => {
    expect(channelImageUrl({ mime_type: 'text/html', b64_json: 'abc' })).toBe('')
    expect(channelImageUrl()).toBe('')
  })
})
