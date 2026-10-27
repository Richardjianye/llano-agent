export interface ProductEnvironment {
  allowedMessagingPlatforms: readonly string[]
  brandId: string
  companyName: string
  disabledCapabilities: readonly string[]
  environment: 'development' | 'production' | 'test'
  gatewayOrigin: string
  modelApiBaseUrl: string
  productName: string
  providerId: string
  providerName: string
  skillHubUrl: string
  updateManifestUrl: string
}

export const PRODUCT_ENVIRONMENTS: readonly string[]
export const configPath: string
export const desktopRoot: string
export function selectedProductEnvironment(env?: NodeJS.ProcessEnv): string
export function loadProductEnvironment(env?: NodeJS.ProcessEnv): ProductEnvironment
