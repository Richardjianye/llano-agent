export type ProductEnvironmentName = 'development' | 'production' | 'test'

export interface ProductPolicy {
  allowedMessagingPlatforms: readonly string[]
  disabledCapabilities: readonly ProductCapability[]
  brandId: string
  companyName: string
  /** 全新安装时的界面语言。见 hermes_cli/product_policy.py 的同名字段。 */
  defaultLanguage: string
  deepLinkProtocol: string
  environment: ProductEnvironmentName
  gatewayOrigin: string
  modelApiBaseUrl: string
  productName: string
  providerId: string
  providerName: string
  updateManifestUrl: string
}

export type ProductCapability =
  | 'nous_services'
  | 'provider_management'
  | 'provider_oauth'
  | 'custom_model_providers'
  | 'moa'
  | 'builtin_image_generation'
  | 'pet_ai_generation'

declare const __LLANO_PRODUCT_POLICY__: ProductPolicy

/** Build-time policy for the branded desktop. It is deliberately not user-editable. */
export const PRODUCT_POLICY: ProductPolicy = Object.freeze({
  ...__LLANO_PRODUCT_POLICY__,
  allowedMessagingPlatforms: Object.freeze([...__LLANO_PRODUCT_POLICY__.allowedMessagingPlatforms]),
  disabledCapabilities: Object.freeze([...__LLANO_PRODUCT_POLICY__.disabledCapabilities])
})

export const IS_BRANDED_PRODUCT = Boolean(PRODUCT_POLICY.brandId)

/** An empty allowlist keeps every registered messaging connector available. */
export const isAllowedMessagingPlatform = (
  platformId: string,
  policy: ProductPolicy = PRODUCT_POLICY
): boolean =>
  policy.allowedMessagingPlatforms.length === 0 ||
  policy.allowedMessagingPlatforms.includes(platformId.trim().toLowerCase())

export const isCapabilityAllowed = (
  capability: ProductCapability,
  policy: ProductPolicy = PRODUCT_POLICY
): boolean => !policy.disabledCapabilities.includes(capability)

const ENV_VAR_CAPABILITIES: Readonly<Record<string, ProductCapability>> = {
  KREA_API_KEY: 'builtin_image_generation'
}

/** Keep capability-specific credentials off surfaces where that capability is disabled. */
export const isEnvVarAllowed = (key: string, policy: ProductPolicy = PRODUCT_POLICY): boolean => {
  const capability = ENV_VAR_CAPABILITIES[key]

  return !capability || isCapabilityAllowed(capability, policy)
}

export const visibleSettingsSections = <T extends { id: string }>(
  sections: readonly T[],
  policy: ProductPolicy = PRODUCT_POLICY
): T[] => sections.filter(section => section.id !== 'model' || isCapabilityAllowed('provider_management', policy))

export const isSettingsDestinationAllowed = (
  destination: string,
  policy: ProductPolicy = PRODUCT_POLICY
): boolean => {
  if (destination.startsWith('providers')) {
    return isCapabilityAllowed('provider_management', policy)
  }

  if (destination === 'billing') {
    return isCapabilityAllowed('nous_services', policy)
  }

  return true
}
