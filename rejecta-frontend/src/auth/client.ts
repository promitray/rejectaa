/**
 * HTTP client with Rejecta auth.
 *
 * - The access token lives only in memory (never localStorage), so a script
 *   injected into the page cannot read a stored token.
 * - The refresh token is an HttpOnly cookie on the auth path. Auth calls go to
 *   AUTH_BASE (/api/auth), which Netlify (prod) and Vite (dev) proxy to the API,
 *   so the cookie is first-party.
 * - Refreshes are single-flight across tabs (Web Locks) and retried once when
 *   another tab rotated the cookie at the same moment ("refresh_superseded").
 */
import type { AuthResponse } from './types'

export const API_BASE: string = import.meta.env.VITE_API_URL ?? 'http://localhost:8000'
export const AUTH_BASE: string = import.meta.env.VITE_AUTH_BASE ?? '/api/auth'
const CLIENT_HEADERS = { 'Content-Type': 'application/json', 'X-Rejecta-Client': 'web' }
const EXPIRY_MARGIN_MS = 30_000

export class ApiError extends Error {
  readonly status: number
  readonly code: string | undefined
  readonly data: Record<string, unknown>

  constructor(message: string, status: number, data: Record<string, unknown> = {}) {
    super(message)
    this.status = status
    this.code = typeof data.code === 'string' ? data.code : undefined
    this.data = data
  }
}

export function errorMessage(body: unknown, status: number): string {
  if (typeof body !== 'object' || body === null || !('detail' in body)) {
    return `Request failed: ${status}`
  }
  const detail = (body as { detail: unknown }).detail
  if (typeof detail === 'string' && detail.length > 0) {
    return detail
  }
  if (Array.isArray(detail)) {
    const messages = detail.flatMap((item) => {
      if (typeof item === 'object' && item !== null && 'msg' in item) {
        const msg = (item as { msg: unknown }).msg
        return typeof msg === 'string' ? [msg.replace(/^Value error, /, '')] : []
      }
      return []
    })
    if (messages.length > 0) {
      return messages.join('; ')
    }
  }
  return `Request failed: ${status}`
}

async function toError(res: Response): Promise<ApiError> {
  const body: unknown = await res.json().catch(() => ({}))
  const data = typeof body === 'object' && body !== null ? (body as Record<string, unknown>) : {}
  return new ApiError(errorMessage(body, res.status), res.status, data)
}

// --- session state -----------------------------------------------------------

let accessToken: string | null = null
let expiresAt = 0
type Listener = (signedIn: boolean) => void
const listeners = new Set<Listener>()

export function onSessionChange(listener: Listener): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

// --- per-tab sign-in ------------------------------------------------------------
//
// The refresh cookie is shared by every tab of the browser, so on its own a new
// tab would silently sign in as whoever is signed in elsewhere. Each tab
// therefore remembers (in sessionStorage, which is private to the tab) that
// IT signed in, and as whom. A tab without that marker never uses the cookie.
// If the cookie later turns out to belong to another account (someone signed in
// as a different user in another tab), this tab signs itself out instead of
// quietly becoming that user.

const TAB_KEY = 'rejecta.tab'
const NOTICE_KEY = 'rejecta.notice'
const ACCOUNT_CHANGED_NOTICE =
  'Another account signed in on this browser, so this tab was signed out. Sign in again to continue here.'

let memoryTabUser: string | null = null

function storageGet(key: string): string | null {
  try {
    return sessionStorage.getItem(key)
  } catch {
    return null
  }
}

function storageSet(key: string, value: string | null): void {
  try {
    if (value === null) sessionStorage.removeItem(key)
    else sessionStorage.setItem(key, value)
  } catch {
    // Storage blocked (private mode): the tab still works until it is reloaded.
  }
}

/** The account this tab signed in as, or null if this tab never signed in. */
export function tabUserId(): string | null {
  return memoryTabUser ?? storageGet(TAB_KEY)
}

export function hasTabSession(): boolean {
  return tabUserId() !== null
}

/** Why this tab was signed out, if it was not the user's own doing. */
export function peekSignInNotice(): string | null {
  return storageGet(NOTICE_KEY)
}

const channel: BroadcastChannel | null = typeof BroadcastChannel !== 'undefined' ? new BroadcastChannel('rejecta-auth') : null

/** Sign this tab out locally (never revokes the server session: it may belong to someone else). */
function endTab(notice: string): void {
  storageSet(NOTICE_KEY, notice)
  clearSession()
}

if (channel) {
  channel.onmessage = (event: MessageEvent<{ userId?: string }>) => {
    const other = event.data?.userId
    const mine = tabUserId()
    if (mine !== null && typeof other === 'string' && other !== mine) {
      endTab(ACCOUNT_CHANGED_NOTICE)
    }
  }
}

export function setSession(auth: Pick<AuthResponse, 'access_token' | 'expires_in'> & { user?: { id: string } }): void {
  accessToken = auth.access_token
  expiresAt = Date.now() + auth.expires_in * 1000
  if (auth.user) {
    const changed = tabUserId() !== auth.user.id
    memoryTabUser = auth.user.id
    storageSet(TAB_KEY, auth.user.id)
    storageSet(NOTICE_KEY, null)
    // Tell the other tabs, so one signed in as someone else signs out right away.
    if (changed) channel?.postMessage({ userId: auth.user.id })
  }
  listeners.forEach((l) => l(true))
}

/** Forget the access token. `keepTab` keeps the marker (a temporary failure, not a sign-out). */
export function clearSession(keepTab = false): void {
  const wasSignedIn = accessToken !== null
  accessToken = null
  expiresAt = 0
  if (!keepTab) {
    memoryTabUser = null
    storageSet(TAB_KEY, null)
  }
  if (wasSignedIn) {
    listeners.forEach((l) => l(false))
  }
}

export function hasSession(): boolean {
  return accessToken !== null
}

// --- auth endpoints (cookie routes) -------------------------------------------

export async function authPost<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${AUTH_BASE}${path}`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: CLIENT_HEADERS,
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!res.ok) {
    throw await toError(res)
  }
  return (res.status === 204 ? undefined : await res.json()) as T
}

let refreshing: Promise<boolean> | null = null

async function doRefresh(retry = true): Promise<boolean> {
  try {
    const auth = await authPost<AuthResponse>('/refresh')
    const known = tabUserId()
    if (known !== null && known !== auth.user.id) {
      // The shared cookie now belongs to another account: this tab does not follow it.
      endTab(ACCOUNT_CHANGED_NOTICE)
      return false
    }
    setSession(auth)
    return true
  } catch (err) {
    if (err instanceof ApiError && err.code === 'refresh_superseded' && retry) {
      await new Promise((r) => setTimeout(r, 300))
      return doRefresh(false)
    }
    // A real "not signed in" ends this tab's sign-in; a network or server hiccup does not.
    const ended = err instanceof ApiError && (err.status === 401 || err.status === 403)
    clearSession(!ended)
    return false
  }
}

/** Get a new access token from the refresh cookie. Resolves false if signed out. */
export function refreshSession(): Promise<boolean> {
  if (refreshing) {
    return refreshing
  }
  const locks = typeof navigator !== 'undefined' ? navigator.locks : undefined
  // Web Locks typings wrap the callback's promise; at runtime it is flattened.
  const run: Promise<boolean> = locks
    ? locks.request('rejecta-refresh', () => doRefresh()).then((result) => result)
    : doRefresh()
  const current = run.finally(() => {
    refreshing = null
  })
  refreshing = current
  return current
}

/**
 * Bearer-authenticated call on the auth proxy path. Used for routes whose
 * response sets or clears the refresh cookie (change-password, logout-all),
 * so the cookie stays first-party.
 */
export async function authPostWithToken<T>(path: string, body?: unknown): Promise<T> {
  if (!accessToken || Date.now() > expiresAt - EXPIRY_MARGIN_MS) {
    if (!(await refreshSession())) {
      throw new ApiError('Please sign in to continue.', 401, { code: 'missing_token' })
    }
  }
  const res = await fetch(`${AUTH_BASE}${path}`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { ...CLIENT_HEADERS, Authorization: `Bearer ${accessToken ?? ''}` },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!res.ok) {
    throw await toError(res)
  }
  return (res.status === 204 ? undefined : await res.json()) as T
}

// --- API calls ---------------------------------------------------------------

export interface FetchOptions {
  auth?: boolean // send the access token (default true)
}

/** Fetch an API path with the access token, refreshing it when needed. */
export async function apiFetch<T>(path: string, init: RequestInit = {}, options: FetchOptions = {}): Promise<T> {
  const useAuth = options.auth !== false
  let refreshed = false
  if (useAuth && (!accessToken || Date.now() > expiresAt - EXPIRY_MARGIN_MS)) {
    refreshed = true
    if (!(await refreshSession())) {
      throw new ApiError('Please sign in to continue.', 401, { code: 'missing_token' })
    }
  }

  const send = (): Promise<Response> => {
    const headers = new Headers(init.headers)
    if (init.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) {
      headers.set('Content-Type', 'application/json')
    }
    if (useAuth && accessToken) {
      headers.set('Authorization', `Bearer ${accessToken}`)
    }
    return fetch(`${API_BASE}${path}`, { ...init, headers })
  }

  let res = await send()
  if (res.status === 401 && useAuth) {
    if (!refreshed && (await refreshSession())) {
      res = await send()
    }
    if (res.status === 401) {
      clearSession()
    }
  }
  if (!res.ok) {
    throw await toError(res)
  }
  return (res.status === 204 ? undefined : await res.json()) as T
}

export function apiJson<T>(path: string, method: string, body?: unknown): Promise<T> {
  return apiFetch<T>(path, { method, body: body === undefined ? undefined : JSON.stringify(body) })
}

// --- usage refresh signal ----------------------------------------------------

const usageListeners = new Set<() => void>()

export function onUsageChanged(listener: () => void): () => void {
  usageListeners.add(listener)
  return () => usageListeners.delete(listener)
}

/** Tool calls consume quota; tell the app to re-read /me. */
export function notifyUsageChanged(): void {
  usageListeners.forEach((l) => l())
}
