/** Mirrors rejecta-api/services/authors.py. */

export interface AuthorWork {
  id: string
  year: number | null
  type: string
  cites: number
  venue_id: string | null
}

export interface VenueInfo {
  id: string
  name: string
  kind: string | null
  works: number
  /** OpenAlex 2-year mean citedness. Close to, but not, the JCR Impact Factor. */
  impact: number | null
}

export interface TopicInfo {
  name: string
  count: number
}

export interface AuthorProfile {
  orcid: string
  name: string | null
  openalex_id: string | null
  institution: string | null
  works_count: number
  cited_by_count: number
  h_index: number
  i10_index: number
  mean_citedness_2y: number | null
  works: AuthorWork[]
  works_truncated: boolean
  venues: VenueInfo[]
  topics: TopicInfo[]
  has_publications: boolean
}

export interface AuthorProfileResponse {
  orcid: string
  found: boolean
  /** `cache` = answered from Rejecta's saved records, `live` = just fetched, `stale` = saved record served because the sources were down. */
  source: 'cache' | 'live' | 'stale'
  fetched_at: string
  profile: AuthorProfile | null
  message: string | null
}

export type AuthorRole = 'first' | 'corresponding' | 'co'

/** One row of the author form. */
export interface AuthorEntry {
  key: string
  role: AuthorRole
  name: string
  orcid: string
  scholarId: string
}

export type LookupState =
  | { status: 'loading' }
  | { status: 'ok'; data: AuthorProfileResponse }
  | { status: 'error'; message: string }

export const ROLE_LABEL: Record<AuthorRole, string> = {
  first: 'First author',
  corresponding: 'Corresponding author',
  co: 'Co-author',
}
