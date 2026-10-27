export function productNetworkUserAgent(currentUserAgent: string, displayName: string, brandId: string, version: string) {
  const asciiProductId = brandId.replace(/[^A-Za-z0-9._-]/g, '') || 'HermesDesktop'

  return currentUserAgent.replace(`${displayName}/${version}`, `${asciiProductId}/${version}`).replace(/[^\x20-\x7E]/g, '')
}
