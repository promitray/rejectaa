export type Role = 'admin' | 'user'

export interface UserPublic {
  id: string
  email: string
  full_name: string
  role: Role
  created_at: string
}

export interface AuthResponse {
  access_token: string
  token_type: 'Bearer'
  expires_in: number
  user: UserPublic
}

export interface AuthWithRecoveryCodes extends AuthResponse {
  recovery_codes: string[]
}

export interface MfaChallenge {
  mfa_required: true
  mfa_enrollment_required: boolean
  mfa_token: string
  expires_in: number
}

export interface MfaEnrollStart {
  secret: string
  otpauth_uri: string
  issuer: string
  account: string
}

export interface FeatureUsage {
  key: string
  name: string
  description: string
  enabled: boolean
  reason: string | null
  daily_limit: number | null
  monthly_limit: number | null
  used_today: number
  used_this_month: number
  remaining_today: number | null
  remaining_this_month: number | null
  day_resets_at: string
  month_resets_at: string
}

export interface Me extends UserPublic {
  status: 'active' | 'suspended'
  mfa_enabled: boolean
  aal: number
  plan: { id: string; key: string; name: string } | null
  features: FeatureUsage[]
}

export type FeatureKey = 'analyse_paper' | 'voice_calibrate' | 'abstract_forge' | 'rebuttal_writer' | 'ai_heatmap'
