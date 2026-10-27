import { requestRaw } from '@/api/http'

export type DemoComponent = 'welcome' | 'team' | 'finance'
export type DemoDataset = 'team' | 'finance'

export interface DemoCapabilities {
  site_id: string
  title: string
  role: string
  can_manage: boolean
  components: DemoComponent[]
  data: DemoDataset[]
}

export interface DemoPolicy {
  components: Record<DemoComponent, string[]>
  data: Record<DemoDataset, string[]>
}

export interface DemoPolicyResponse {
  roles: string[]
  policy: DemoPolicy
}

export interface TeamData {
  title: string
  completed: number
  total: number
}

export interface FinanceData {
  title: string
  used: number
  total: number
  unit: string
}

export function getDemoCapabilities (siteId: string) {
  return requestRaw<DemoCapabilities>(`/sites/${siteId}/demo/capabilities`)
}

export function getDemoData<T> (siteId: string, dataset: DemoDataset) {
  return requestRaw<T>(`/sites/${siteId}/demo/data/${dataset}`)
}

export function getDemoPolicy (siteId: string) {
  return requestRaw<DemoPolicyResponse>(`/sites/${siteId}/demo/policy`)
}

export function putDemoPolicy (siteId: string, policy: DemoPolicy) {
  return requestRaw<DemoPolicyResponse>(`/sites/${siteId}/demo/policy`, { method: 'PUT', body: policy })
}
