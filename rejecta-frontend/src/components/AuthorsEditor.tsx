import type { ReactElement } from 'react'
import { useState } from 'react'
import { normaliseOrcid, normaliseScholarId, orcidUrl, scholarUrl } from '../authors/ids'
import { compareNames } from '../authors/nameMatch'
import type { AuthorEntry, AuthorRole, LookupState } from '../authors/types'
import { ROLE_LABEL } from '../authors/types'
import { MAX_AUTHORS, newAuthor } from '../store'

interface Props {
  authors: AuthorEntry[]
  onChange: (authors: AuthorEntry[]) => void
  /** Lookup state per normalised ORCID. */
  states: Record<string, LookupState>
  onRetry: (orcid: string) => void
  /** Runs the lookups for the valid ORCIDs. */
  onAnalyse: () => void
  busy: boolean
  /** Problem with the whole form (for example no ORCID entered). */
  error: string | null
}

const inputClass =
  'mt-1 w-full rounded-lg border border-gray-300 px-3 py-2 text-sm outline-none focus:border-gray-900'

export default function AuthorsEditor({ authors, onChange, states, onRetry, onAnalyse, busy, error }: Props): ReactElement {
  const [touched, setTouched] = useState<Set<string>>(new Set())

  function patch(key: string, change: Partial<AuthorEntry>): void {
    onChange(authors.map((author) => (author.key === key ? { ...author, ...change } : author)))
  }

  function touch(field: string): void {
    setTouched((current) => new Set(current).add(field))
  }

  function remove(key: string): void {
    onChange(authors.filter((author) => author.key !== key))
  }

  const firstIndexOf = (orcid: string): number => authors.findIndex((a) => normaliseOrcid(a.orcid) === orcid)

  return (
    <fieldset className="w-full rounded-xl border border-gray-200 bg-white p-6 shadow-sm">
      <legend className="sr-only">Authors</legend>
      <h2 className="text-base font-semibold text-gray-900">
        Authors <span className="text-sm font-normal text-gray-500">(optional)</span>
      </h2>
      <p className="mt-1 text-xs leading-5 text-gray-500">
        Add each author&apos;s ORCID to see their papers, citations and journals. Public records only. Looked-up authors
        are saved, so the next search is instant. This is separate from the paper analysis.
      </p>

      <div className="mt-3 space-y-3">
        {authors.map((author, index) => {
          const orcid = normaliseOrcid(author.orcid)
          const orcidTouched = touched.has(`${author.key}:orcid`)
          const scholar = normaliseScholarId(author.scholarId)
          const scholarTouched = touched.has(`${author.key}:scholar`)
          const duplicateOf = orcid ? firstIndexOf(orcid) : -1
          const state = orcid ? states[orcid] : undefined
          const resolvedName = state?.status === 'ok' ? state.data.profile?.name : null

          return (
            <div key={author.key} className="rounded-lg border border-gray-200 bg-gray-50/60 p-3">
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs font-semibold uppercase tracking-wide text-gray-600">Author {index + 1}</span>
                <div className="flex items-center gap-2">
                  <label className="sr-only" htmlFor={`${author.key}-role`}>
                    Role of author {index + 1}
                  </label>
                  <select
                    id={`${author.key}-role`}
                    value={author.role}
                    onChange={(event) => patch(author.key, { role: event.target.value as AuthorRole })}
                    className="rounded-md border border-gray-300 bg-white px-2 py-1 text-xs text-gray-700 outline-none focus:border-gray-900"
                  >
                    {(Object.keys(ROLE_LABEL) as AuthorRole[]).map((role) => (
                      <option key={role} value={role}>
                        {ROLE_LABEL[role]}
                      </option>
                    ))}
                  </select>
                  {authors.length > 1 ? (
                    <button
                      type="button"
                      onClick={() => remove(author.key)}
                      aria-label={`Remove author ${index + 1}`}
                      className="flex h-6 w-6 items-center justify-center rounded-full text-gray-500 hover:bg-gray-200 hover:text-gray-900"
                    >
                      ×
                    </button>
                  ) : null}
                </div>
              </div>

              <label className="mt-2 block text-xs text-gray-600" htmlFor={`${author.key}-name`}>
                Name
              </label>
              <input
                id={`${author.key}-name`}
                value={author.name}
                onChange={(event) => patch(author.key, { name: event.target.value })}
                placeholder={resolvedName ?? 'e.g. Jane Smith'}
                autoComplete="off"
                className={inputClass}
              />

              <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2">
                <div>
                  <label className="block text-xs text-gray-600" htmlFor={`${author.key}-orcid`}>
                    ORCID
                  </label>
                  <input
                    id={`${author.key}-orcid`}
                    value={author.orcid}
                    onChange={(event) => patch(author.key, { orcid: event.target.value })}
                    onBlur={() => {
                      touch(`${author.key}:orcid`)
                      if (orcid && orcid !== author.orcid) patch(author.key, { orcid })
                    }}
                    placeholder="0000-0002-1825-0097"
                    inputMode="text"
                    autoComplete="off"
                    spellCheck={false}
                    aria-invalid={Boolean(author.orcid.trim() && !orcid && orcidTouched)}
                    className={inputClass}
                  />
                </div>
                <div>
                  <label className="block text-xs text-gray-600" htmlFor={`${author.key}-scholar`}>
                    Google Scholar ID
                  </label>
                  <input
                    id={`${author.key}-scholar`}
                    value={author.scholarId}
                    onChange={(event) => patch(author.key, { scholarId: event.target.value })}
                    onBlur={() => {
                      touch(`${author.key}:scholar`)
                      if (scholar && scholar !== author.scholarId) patch(author.key, { scholarId: scholar })
                    }}
                    placeholder="12-character id or profile link"
                    autoComplete="off"
                    spellCheck={false}
                    aria-invalid={Boolean(author.scholarId.trim() && !scholar && scholarTouched)}
                    className={inputClass}
                  />
                </div>
              </div>

              <div className="mt-2 min-h-[1rem] space-y-1 text-xs" aria-live="polite">
                {author.orcid.trim() && !orcid && orcidTouched ? (
                  <p className="text-red-600">
                    Not a valid ORCID. It looks like 0000-0002-1825-0097; check the last digit.
                  </p>
                ) : null}
                {orcid && duplicateOf !== index ? (
                  <p className="text-amber-700">Same ORCID as author {duplicateOf + 1}.</p>
                ) : null}
                {orcid && duplicateOf === index ? (
                  <LookupLine orcid={orcid} state={state} typedName={author.name} onRetry={onRetry} />
                ) : null}
                {author.scholarId.trim() && !scholar && scholarTouched ? (
                  <p className="text-red-600">
                    Google Scholar IDs are 12 characters (the part after <span className="font-mono">user=</span>).
                  </p>
                ) : null}
                {scholar ? (
                  <p className="text-gray-500">
                    <a
                      href={scholarUrl(scholar)}
                      target="_blank"
                      rel="noreferrer noopener"
                      className="underline underline-offset-2 hover:text-gray-900"
                    >
                      Open Google Scholar profile ↗
                    </a>
                  </p>
                ) : null}
              </div>
            </div>
          )
        })}
      </div>

      <button
        type="button"
        onClick={() => onChange([...authors, newAuthor('co')])}
        disabled={authors.length >= MAX_AUTHORS}
        className="mt-3 w-full rounded-lg border border-dashed border-gray-300 px-3 py-2 text-sm text-gray-700 hover:border-gray-500 hover:text-gray-900 disabled:cursor-not-allowed disabled:opacity-50"
      >
        + Add author
      </button>

      {error ? (
        <p className="mt-3 text-sm text-red-600" role="alert">
          {error}
        </p>
      ) : null}
      <button
        type="button"
        onClick={onAnalyse}
        disabled={busy}
        className="mt-4 flex w-full items-center justify-center gap-2 rounded-lg bg-black px-4 py-2.5 text-sm font-medium text-white disabled:cursor-not-allowed disabled:opacity-70"
      >
        {busy ? (
          <span
            className="h-4 w-4 animate-spin rounded-full border-2 border-white/30 border-t-white"
            aria-hidden="true"
          />
        ) : null}
        {busy ? 'Looking up authors…' : 'Analyse authors →'}
      </button>
    </fieldset>
  )
}

function LookupLine({
  orcid,
  state,
  typedName,
  onRetry,
}: {
  orcid: string
  state: LookupState | undefined
  typedName: string
  onRetry: (orcid: string) => void
}): ReactElement | null {
  if (!state) {
    return null // not analysed yet
  }
  if (state.status === 'loading') {
    return (
      <p className="flex items-center gap-2 text-gray-500">
        <span className="h-3 w-3 animate-spin rounded-full border-2 border-gray-300 border-t-gray-700" aria-hidden="true" />
        Looking up this author…
      </p>
    )
  }
  if (state.status === 'error') {
    return (
      <p className="text-red-600">
        {state.message}{' '}
        <button type="button" onClick={() => onRetry(orcid)} className="underline underline-offset-2">
          Try again
        </button>
      </p>
    )
  }
  const { data } = state
  if (!data.found || !data.profile) {
    return <p className="text-amber-700">{data.message ?? 'No record found for this ORCID.'}</p>
  }
  const nameCheck = compareNames(typedName, data.profile.name)
  return (
    <>
    <p className="text-gray-600">
      <span className="text-green-700">✓</span>{' '}
      <a href={orcidUrl(orcid)} target="_blank" rel="noreferrer noopener" className="underline underline-offset-2">
        {data.profile.name ?? orcid}
      </a>
      {data.profile.institution ? ` · ${data.profile.institution}` : ''}
      {data.source !== 'live' ? (
        <span
          className="ml-2 rounded-full bg-gray-200 px-1.5 py-0.5 text-[10px] text-gray-600"
          title={
            data.source === 'stale'
              ? 'The data sources did not answer, so an older saved record is shown.'
              : 'Loaded from Rejecta’s saved records, not fetched again.'
          }
        >
          {data.source === 'stale' ? 'older record' : 'saved record'}
        </span>
      ) : null}
    </p>
    {typedName.trim() && nameCheck === 'mismatch' ? (
      <p className="rounded-md bg-amber-50 px-2 py-1.5 text-amber-800" role="alert">
        You entered “{typedName.trim()}”, but this ORCID belongs to{' '}
        <strong className="font-semibold">{data.profile.name}</strong>
        {data.profile.institution ? ` (${data.profile.institution})` : ''}. Check the ORCID.
      </p>
    ) : null}
    {typedName.trim() && nameCheck === 'unknown' ? (
      <p className="text-gray-500">This ORCID shows no name, so the name you entered can’t be checked.</p>
    ) : null}
    </>
  )
}
