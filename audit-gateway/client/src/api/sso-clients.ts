import { requestRaw } from '@/api/http'

export interface SSOClient {
  client_id: string
  name: string
  owner_uid: string
  environment: 'dev' | 'prod'
  audit_contact: string
  client_type: 'public' | 'confidential'
  redirect_uris: string[]
  allowed_scopes: string[]
  enabled: boolean
}

export interface ClientRegistration extends Omit<SSOClient, 'enabled'> {}

export interface ClientUpdate extends Pick<SSOClient, 'name' | 'owner_uid' | 'audit_contact' | 'redirect_uris' | 'allowed_scopes'> {}

export interface ClientSecretResult {
  client_id: string
  client_secret: string | null
  message: string
}

export function listSSOClients () {
  return requestRaw<SSOClient[]>('/admin/sso/clients')
}

export function registerSSOClient (body: ClientRegistration) {
  return requestRaw<ClientSecretResult>('/admin/sso/clients', { method: 'POST', body })
}

export function disableSSOClient (clientId: string) {
  return requestRaw<{ client_id: string, enabled: boolean }>(`/admin/sso/clients/${encodeURIComponent(clientId)}/disable`, { method: 'POST' })
}

export function rotateSSOClientSecret (clientId: string) {
  return requestRaw<ClientSecretResult>(`/admin/sso/clients/${encodeURIComponent(clientId)}/rotate-secret`, { method: 'POST' })
}

export function updateSSOClient (clientId: string, body: ClientUpdate) {
  return requestRaw<{ client_id: string }>(`/admin/sso/clients/${encodeURIComponent(clientId)}`, { method: 'PUT', body })
}

export function enableSSOClient (clientId: string) {
  return requestRaw<{ client_id: string, enabled: boolean }>(`/admin/sso/clients/${encodeURIComponent(clientId)}/enable`, { method: 'POST' })
}
