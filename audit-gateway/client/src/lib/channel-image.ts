/** Build an inline image URL without changing the provider's validated image bytes. */
export function channelImageUrl (image?: { mime_type: string, b64_json: string }): string {
  if (!image || !['image/png', 'image/jpeg', 'image/webp'].includes(image.mime_type)) {
    return ''
  }
  return `data:${image.mime_type};base64,${image.b64_json}`
}
