/** Typed client for /admin/* (requires an admin with a two-factor session). */
import { apiFetch, apiJson } from '../auth/client'
import type { FeatureUsage } from '../auth/types'

export type Limit = number | null

export interface PlanRef {
  id: string
  key: string
  name: string
}

export interface AdminUser {
  id: string
  email: string
  full_name: string
  role: 'admin' | 'user'
  status: 'active' | 'suspended'
  plan: PlanRef
  mfa_enabled: boolean
  locked: boolean
  created_at: string
  last_login_at: string | null
}

export interface UserOverride {
  feature_key: string
  enabled: boolean | null
  daily_limit_set: boolean
  daily_limit: Limit
  monthly_limit_set: boolean
  monthly_limit: Limit
  expires_at: string | null
  reason: string
  updated_at: string
}

export interface AdminUserDetail extends AdminUser {
  features: FeatureUsage[]
  overrides: UserOverride[]
  active_sessions: number
  recovery_codes_left: number | null
}

export interface PasswordLink {
  url: string
  expires_at: string
}

export interface PlanFeature {
  feature_key: string
  name: string
  globally_enabled: boolean
  enabled: boolean
  daily_limit: Limit
  monthly_limit: Limit
}

export interface AdminPlan {
  id: string
  key: string
  name: string
  description: string
  is_default: boolean
  status: 'active' | 'archived'
  sort_order: number
  version: number
  user_count: number
  client_count: number
  usage_this_month: number
  created_at: string
  updated_at: string
  features: PlanFeature[]
}

export interface AdminFeature {
  key: string
  name: string
  description: string
  globally_enabled: boolean
  sort_order: number
}

export interface Stats {
  users_total: number
  users_active: number
  users_suspended: number
  admins: number
  signups_last_7_days: number
  logins_today: number
  usage_today: number
  usage_this_month: number
  users_by_plan: { key: string; name: string; status: string; users: number }[]
}

export interface UsageReport {
  days: number
  series: { day: string; feature_key: string; committed: number; refunded: number }[]
  by_plan: { plan_key: string; plan_name: string; feature_key: string; committed: number }[]
}

export interface AuditEntry {
  id: number
  created_at: string
  actor_type: string
  actor_id: string | null
  actor_email: string | null
  action: string
  target_type: string | null
  target_id: string | null
  before: unknown
  after: unknown
  ip: string | null
  user_agent: string | null
}

export interface PartnerSecret {
  id: string
  prefix: string
  last4: string
  created_at: string
  expires_at: string | null
  revoked_at: string | null
  last_used_at: string | null
}

export interface AdminPartner {
  id: string
  client_id: string
  name: string
  description: string
  plan: PlanRef
  scopes: string[]
  status: 'active' | 'disabled'
  created_at: string
  last_used_at: string | null
  secrets: PartnerSecret[]
}

export interface NewPartnerSecret {
  client_id: string
  client_secret: string
  secret: PartnerSecret
}

function query(params: Record<string, string | number | undefined | null>): string {
  const q = new URLSearchParams()
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') q.set(k, String(v))
  })
  const s = q.toString()
  return s ? `?${s}` : ''
}

export const adminApi = {
  stats: () => apiFetch<Stats>('/admin/stats'),
  usage: (days: number) => apiFetch<UsageReport>(`/admin/usage${query({ days })}`),
  audit: (params: { action?: string; target_id?: string; before_id?: number; limit?: number; page?: number; page_size?: number }) =>
    apiFetch<{ items: AuditEntry[]; total: number; page: number; page_size: number; next_before_id: number | null }>(`/admin/audit-log${query(params)}`),

  users: (params: { search?: string; role?: string; status?: string; plan_id?: string; page?: number; page_size?: number }) =>
    apiFetch<{ items: AdminUser[]; total: number; page: number; page_size: number }>(`/admin/users${query(params)}`),
  user: (id: string) => apiFetch<AdminUserDetail>(`/admin/users/${id}`),
  createUser: (body: { email: string; full_name: string; role: string; plan_id?: string | null }) =>
    apiJson<{ user: AdminUser; password_setup: PasswordLink }>('/admin/users', 'POST', body),
  updateUser: (id: string, body: Partial<{ full_name: string; role: string; status: string; plan_id: string }>) =>
    apiJson<AdminUser>(`/admin/users/${id}`, 'PATCH', body),
  resetLink: (id: string) => apiJson<PasswordLink>(`/admin/users/${id}/password-reset-link`, 'POST'),
  revokeSessions: (id: string) => apiJson<void>(`/admin/users/${id}/revoke-sessions`, 'POST'),
  resetMfa: (id: string) => apiJson<void>(`/admin/users/${id}/mfa/reset`, 'POST'),
  setOverride: (id: string, feature: string, body: Omit<UserOverride, 'feature_key' | 'updated_at'>) =>
    apiJson<UserOverride>(`/admin/users/${id}/overrides/${feature}`, 'PUT', body),
  deleteOverride: (id: string, feature: string) => apiJson<void>(`/admin/users/${id}/overrides/${feature}`, 'DELETE'),
  resetUsage: (id: string, body: { feature_key?: string | null; period?: 'day' | 'month' | null }) =>
    apiJson<void>(`/admin/users/${id}/usage/reset`, 'POST', body),

  plans: () => apiFetch<AdminPlan[]>('/admin/plans'),
  plan: (id: string) => apiFetch<AdminPlan>(`/admin/plans/${id}`),
  createPlan: (body: { key: string; name: string; description?: string; copy_from_plan_id?: string | null }) =>
    apiJson<AdminPlan>('/admin/plans', 'POST', body),
  updatePlan: (id: string, body: { version: number; name?: string; description?: string; sort_order?: number; is_default?: true; status?: string }) =>
    apiJson<AdminPlan>(`/admin/plans/${id}`, 'PATCH', body),
  savePlanFeatures: (id: string, version: number, features: Omit<PlanFeature, 'name' | 'globally_enabled'>[]) =>
    apiJson<AdminPlan>(`/admin/plans/${id}/features`, 'PUT', { version, features }),
  duplicatePlan: (id: string, body: { key: string; name: string }) =>
    apiJson<AdminPlan>(`/admin/plans/${id}/duplicate`, 'POST', body),
  migratePlan: (id: string, toPlanId: string) =>
    apiJson<{ moved_users: number; moved_clients: number }>(`/admin/plans/${id}/migrate-users`, 'POST', { to_plan_id: toPlanId }),
  planUsage: (id: string, period: 'day' | 'month') =>
    apiFetch<{ rows: { feature_key: string; name: string; used: number; subjects: number }[] }>(
      `/admin/plans/${id}/usage${query({ period })}`,
    ),

  features: () => apiFetch<AdminFeature[]>('/admin/features'),
  updateFeature: (key: string, body: Partial<{ name: string; description: string; globally_enabled: boolean }>) =>
    apiJson<AdminFeature>(`/admin/features/${key}`, 'PATCH', body),

  partners: (params: { search?: string; status?: string; page?: number; page_size?: number } = {}) =>
    apiFetch<{ items: AdminPartner[]; total: number; page: number; page_size: number }>(`/admin/partners${query(params)}`),
  partner: (id: string) => apiFetch<AdminPartner & { features: FeatureUsage[] }>(`/admin/partners/${id}`),
  createPartner: (body: { name: string; description: string; plan_id: string; scopes: string[] }) =>
    apiJson<{ partner: AdminPartner; credentials: NewPartnerSecret }>('/admin/partners', 'POST', body),
  updatePartner: (id: string, body: Partial<{ name: string; description: string; plan_id: string; scopes: string[]; status: string }>) =>
    apiJson<AdminPartner>(`/admin/partners/${id}`, 'PATCH', body),
  addSecret: (id: string) => apiJson<NewPartnerSecret>(`/admin/partners/${id}/secrets`, 'POST'),
  revokeSecret: (id: string, secretId: string) => apiJson<void>(`/admin/partners/${id}/secrets/${secretId}`, 'DELETE'),
}
