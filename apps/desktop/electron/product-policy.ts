export interface ElectronProductPolicy {
  allowedMessagingPlatforms: readonly string[]
  /**
   * 构建物允许挂在哪些 origin 上。网关自己永远是第一项，配置里不用写。
   *
   * 存在的理由是分发与网关分家：生产网关在 Zeabur，安装包放内网分发机，
   * 全公司拉 180 MB 不必绕公网。放宽的只是「去哪儿拿字节」——签名和
   * SHA-256 一步没少，见 update-download.ts 文件头。
   */
  artifactOrigins: readonly string[]
  disabledCapabilities: readonly string[]
  brandId: string
  deepLinkProtocol: string
  environment: 'development' | 'production' | 'test'
  gatewayOrigin: string
  modelApiBaseUrl: string
  productName: string
  providerId: string
  providerName: string
  updateManifestUrl: string
  /**
   * 本构建信任的更新签名公钥（base64 的 32 字节 Ed25519 原始公钥）。
   *
   * 编译进构建物，**不从网关取** —— 能改清单的人自然也能改公钥，那样签名就
   * 退化成「网关说这份清单是对的」，而签名要证明的恰恰是「即使网关被控制，
   * 客户端也能识别出这不是我们发的」。
   *
   * 与网关 `.env` 里的 `UPDATE_PUBLIC_KEY` 必须是同一把；不一致时发出去的
   * 版本谁也装不上，后台版本页显示的指纹是唯一能看出来的地方。
   */
  updateSigningPublicKey: string
}

declare const __LLANO_PRODUCT_POLICY__: ElectronProductPolicy

export const PRODUCT_POLICY: ElectronProductPolicy = Object.freeze({
  ...__LLANO_PRODUCT_POLICY__,
  allowedMessagingPlatforms: Object.freeze([...__LLANO_PRODUCT_POLICY__.allowedMessagingPlatforms]),
  artifactOrigins: Object.freeze([...__LLANO_PRODUCT_POLICY__.artifactOrigins]),
  disabledCapabilities: Object.freeze([...__LLANO_PRODUCT_POLICY__.disabledCapabilities])
})
