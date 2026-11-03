import type { ReactElement, ReactNode } from 'react'
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import {
  apiFetch,
  authPost,
  authPostWithToken,
  clearSession,
  hasSession,
  hasTabSession,
  onSessionChange,
  onUsageChanged,
  refreshSession,
  setSession,
} from './client'
import type { AuthResponse, AuthWithRecoveryCodes, Me, MfaChallenge, MfaEnrollStart } from './types'

type Status = 'loading' | 'signedOut' | 'signedIn'

interface AuthContextValue {
  status: Status
  me: Me | null
  /** Users are signed in directly; admins get an MFA challenge back. */
  login: (email: string, password: string) => Promise<MfaChallenge | null>
  signup: (email: string, password: string, fullName: string) => Promise<void>
  mfaEnrollStart: (mfaToken: string) => Promise<MfaEnrollStart>
  mfaEnrollConfirm: (mfaToken: string, code: string) => Promise<string[]>
  mfaVerify: (mfaToken: string, factor: { code?: string; recovery_code?: string }) => Promise<void>
  logout: () => Promise<void>
  logoutAll: () => Promise<void>
  changePassword: (current: string, next: string) => Promise<void>
  reloadMe: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }): ReactElement {
  const [status, setStatus] = useState<Status>('loading')
  const [me, setMe] = useState<Me | null>(null)
  const reloadTimer = useRef<number | undefined>(undefined)

  const reloadMe = useCallback(async () => {
    try {
      const profile = await apiFetch<Me>('/me')
      setMe(profile)
      setStatus('signedIn')
    } catch {
      if (!hasSession()) {
        setMe(null)
        setStatus('signedOut')
      }
    }
  }, [])

  const begin = useCallback(
    async (auth: AuthResponse) => {
      setSession(auth)
      await reloadMe()
    },
    [reloadMe],
  )

  // Restore a session from the refresh cookie on first load.
  useEffect(() => {
    let cancelled = false
    // A tab that never signed in itself does not borrow a session from another tab.
    if (!hasTabSession()) {
      setStatus('signedOut')
      return
    }
    void refreshSession().then(async (ok) => {
      if (cancelled) return
      if (ok) {
        await reloadMe()
      } else {
        setStatus('signedOut')
      }
    })
    return () => {
      cancelled = true
    }
  }, [reloadMe])

  // Signed out elsewhere (expired, revoked, other tab): drop the profile.
  useEffect(
    () =>
      onSessionChange((signedIn) => {
        if (!signedIn) {
          setMe(null)
          setStatus('signedOut')
        }
      }),
    [],
  )

  // After a tool call, refresh usage numbers (debounced).
  useEffect(
    () =>
      onUsageChanged(() => {
        window.clearTimeout(reloadTimer.current)
        reloadTimer.current = window.setTimeout(() => {
          if (hasSession()) void reloadMe()
        }, 400)
      }),
    [reloadMe],
  )

  const value = useMemo<AuthContextValue>(
    () => ({
      status,
      me,
      login: async (email, password) => {
        const result = await authPost<AuthResponse | MfaChallenge>('/login', { email, password })
        if ('mfa_required' in result) {
          return result
        }
        await begin(result)
        return null
      },
      signup: async (email, password, fullName) => {
        await begin(await authPost<AuthResponse>('/signup', { email, password, full_name: fullName }))
      },
      mfaEnrollStart: (mfaToken) => authPost<MfaEnrollStart>('/mfa/enroll/start', { mfa_token: mfaToken }),
      mfaEnrollConfirm: async (mfaToken, code) => {
        const result = await authPost<AuthWithRecoveryCodes>('/mfa/enroll/confirm', { mfa_token: mfaToken, code })
        setSession(result)
        return result.recovery_codes
      },
      mfaVerify: async (mfaToken, factor) => {
        await begin(await authPost<AuthResponse>('/mfa/verify', { mfa_token: mfaToken, ...factor }))
      },
      logout: async () => {
        try {
          // Sends this tab's token, so the server leaves another account's cookie alone.
          await authPostWithToken<void>('/logout')
        } catch {
          // Already signed out, or the shared cookie belongs to another account: nothing to end.
        } finally {
          clearSession()
          setMe(null)
          setStatus('signedOut')
        }
      },
      logoutAll: async () => {
        try {
          await authPostWithToken<void>('/logout-all')
        } finally {
          clearSession()
          setMe(null)
          setStatus('signedOut')
        }
      },
      changePassword: async (current, next) => {
        // Goes through the auth proxy: the response replaces the refresh cookie.
        const auth = await authPostWithToken<AuthResponse>('/change-password', {
          current_password: current,
          new_password: next,
        })
        await begin(auth)
      },
      reloadMe,
    }),
    [status, me, begin, reloadMe],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) {
    throw new Error('useAuth must be used inside <AuthProvider>')
  }
  return ctx
}
