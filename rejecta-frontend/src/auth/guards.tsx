/**
 * Route guards. These only shape the UI: the API enforces every rule itself.
 */
import type { ReactElement, ReactNode } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { PageLoader } from '../components/Loaders'
import { useAuth } from './AuthProvider'

function FullPageSpinner(): ReactElement {
  return <PageLoader label="Restoring your session" />
}

export function RequireAuth({ children }: { children: ReactNode }): ReactElement {
  const { status } = useAuth()
  const location = useLocation()
  if (status === 'loading') return <FullPageSpinner />
  if (status === 'signedOut') {
    const next = encodeURIComponent(location.pathname + location.search)
    return <Navigate to={`/login?next=${next}`} replace />
  }
  return <>{children}</>
}

export function RequireAdmin({ children }: { children: ReactNode }): ReactElement {
  const { status, me } = useAuth()
  const location = useLocation()
  if (status === 'loading' || (status === 'signedIn' && !me)) return <FullPageSpinner />
  if (status === 'signedOut') {
    return <Navigate to={`/login?next=${encodeURIComponent(location.pathname)}`} replace />
  }
  if (me?.role !== 'admin' || me.aal < 2) {
    return <Navigate to="/app" replace />
  }
  return <>{children}</>
}

/** Send signed-in people away from login/signup pages. */
export function GuestOnly({ children }: { children: ReactNode }): ReactElement {
  const { status } = useAuth()
  if (status === 'loading') return <FullPageSpinner />
  if (status === 'signedIn') return <Navigate to="/app" replace />
  return <>{children}</>
}
