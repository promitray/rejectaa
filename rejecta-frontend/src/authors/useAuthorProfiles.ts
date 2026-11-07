import { useCallback, useState } from 'react'
import { apiFetch } from '../auth/client'
import type { AuthorProfileResponse, LookupState } from './types'

/** Answers kept for this page load, so a second click does not look the same author up again. */
const answered = new Map<string, AuthorProfileResponse>()

export function getAuthorProfile(orcid: string): Promise<AuthorProfileResponse> {
  return apiFetch<AuthorProfileResponse>(`/authors/profile?orcid=${encodeURIComponent(orcid)}`)
}

/**
 * Author lookups run when the user asks (the "Analyse authors" button), not while typing.
 * The API stores every result for all users, so a repeat lookup is a database read.
 */
export function useAuthorProfiles(): {
  states: Record<string, LookupState>
  busy: boolean
  lookup: (orcids: string[]) => void
} {
  const [states, setStates] = useState<Record<string, LookupState>>({})

  const load = useCallback((orcid: string) => {
    const known = answered.get(orcid)
    if (known) {
      setStates((current) => ({ ...current, [orcid]: { status: 'ok', data: known } }))
      return
    }
    setStates((current) => ({ ...current, [orcid]: { status: 'loading' } }))
    getAuthorProfile(orcid)
      .then((data) => {
        answered.set(orcid, data)
        setStates((current) => ({ ...current, [orcid]: { status: 'ok', data } }))
      })
      .catch((err: unknown) => {
        const message = err instanceof Error ? err.message : 'Could not load this author'
        setStates((current) => ({ ...current, [orcid]: { status: 'error', message } }))
      })
  }, [])

  const lookup = useCallback(
    (orcids: string[]) => {
      for (const orcid of new Set(orcids)) {
        load(orcid)
      }
    },
    [load],
  )

  const busy = Object.values(states).some((state) => state.status === 'loading')
  return { states, busy, lookup }
}
