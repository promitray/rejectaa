import type { ReactElement, ReactNode } from 'react'
import { useMemo, useState } from 'react'
import { Bar, BarChart, Cell, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { aggregate, shortVenueName } from '../authors/aggregate'
import type { Aggregate } from '../authors/aggregate'
import { normaliseOrcid, orcidUrl } from '../authors/ids'
import { compareNames } from '../authors/nameMatch'
import type { AuthorEntry, AuthorProfile, LookupState } from '../authors/types'
import { ROLE_LABEL } from '../authors/types'

interface Props {
  authors: AuthorEntry[]
  states: Record<string, LookupState>
}

interface Loaded {
  key: string
  label: string
  typedName: string
  role: AuthorEntry['role']
  profile: AuthorProfile
}

const COMBINED = 'combined'
const SLICE_COLORS = ['#111827', '#2563eb', '#16a34a', '#d97706', '#7c3aed', '#9ca3af']
const nf = new Intl.NumberFormat('en')

export default function AuthorInsights({ authors, states }: Props): ReactElement {
  const loaded = useMemo<Loaded[]>(() => {
    const seen = new Set<string>()
    const rows: Loaded[] = []
    authors.forEach((author, index) => {
      const orcid = normaliseOrcid(author.orcid)
      const state = orcid ? states[orcid] : undefined
      if (!orcid || seen.has(orcid) || state?.status !== 'ok' || !state.data.profile) return
      seen.add(orcid)
      rows.push({
        key: author.key,
        // The ORCID owner's name, so tabs and header never disagree with the data shown.
        label: state.data.profile.name || author.name.trim() || `Author ${index + 1}`,
        typedName: author.name.trim(),
        role: author.role,
        profile: state.data.profile,
      })
    })
    return rows
  }, [authors, states])

  const pending = authors.filter((author) => {
    const orcid = normaliseOrcid(author.orcid)
    return orcid && states[orcid]?.status === 'loading'
  }).length

  const [selected, setSelected] = useState<string>(COMBINED)
  const showCombined = loaded.length > 1
  const active =
    selected === COMBINED && showCombined
      ? COMBINED
      : (loaded.find((row) => row.key === selected)?.key ?? loaded[0]?.key ?? COMBINED)
  const isCombined = active === COMBINED && showCombined
  const current = isCombined ? null : loaded.find((row) => row.key === active)
  const profiles = useMemo(
    () => (isCombined ? loaded.map((row) => row.profile) : current ? [current.profile] : []),
    [isCombined, loaded, current],
  )
  const agg = useMemo(() => aggregate(profiles), [profiles])

  return (
    <section
      aria-label="Author publication record"
      className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm"
    >
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold text-gray-900">Author record</h2>
        {pending > 0 ? <span className="text-xs text-gray-500">Loading {pending}…</span> : null}
      </div>

      {loaded.length === 0 ? (
        <EmptyState pending={pending > 0} />
      ) : (
        <>
          {loaded.length > 1 ? (
            <div role="tablist" aria-label="Authors" className="mt-3 flex flex-wrap gap-1.5">
              {showCombined ? (
                <Tab active={isCombined} onClick={() => setSelected(COMBINED)}>
                  Combined
                </Tab>
              ) : null}
              {loaded.map((row) => (
                <Tab key={row.key} active={!isCombined && active === row.key} onClick={() => setSelected(row.key)}>
                  {row.label}
                </Tab>
              ))}
            </div>
          ) : null}

          {current ? <ProfileHeader row={current} /> : <CombinedHeader count={loaded.length} />}

          {current && !current.profile.has_publications ? (
            <p className="mt-4 rounded-lg bg-amber-50 p-3 text-xs text-amber-800">
              OpenAlex has no publications for this ORCID yet, so there is nothing to chart.
            </p>
          ) : (
            <>
              <Tiles isCombined={isCombined} profiles={profiles} agg={agg} />
              <Charts agg={agg} />
            </>
          )}
          <Footnote profiles={profiles} />
        </>
      )}
    </section>
  )
}

function EmptyState({ pending }: { pending: boolean }): ReactElement {
  return (
    <p className="mt-4 text-sm leading-6 text-gray-500">
      {pending
        ? 'Fetching the publication record…'
        : 'Enter a valid ORCID for an author to see their papers, citations and journals here.'}
    </p>
  )
}

function Tab({ active, onClick, children }: { active: boolean; onClick: () => void; children: ReactNode }): ReactElement {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      onClick={onClick}
      className={`max-w-[10rem] truncate rounded-full px-3 py-1 text-xs ${
        active ? 'bg-black text-white' : 'border border-gray-300 bg-white text-gray-600 hover:border-gray-500'
      }`}
    >
      {children}
    </button>
  )
}

function ProfileHeader({ row }: { row: Loaded }): ReactElement {
  const { profile } = row
  return (
    <div className="mt-4">
      <p className="text-base font-semibold text-gray-900">{profile.name ?? row.label}</p>
      <p className="mt-0.5 text-xs text-gray-500">
        {ROLE_LABEL[row.role]}
        {profile.institution ? ` · ${profile.institution}` : ''}
      </p>
      <a
        href={orcidUrl(profile.orcid)}
        target="_blank"
        rel="noreferrer noopener"
        className="mt-1 inline-block font-mono text-xs text-gray-500 underline underline-offset-2 hover:text-gray-900"
      >
        {profile.orcid} ↗
      </a>
      {row.typedName && compareNames(row.typedName, profile.name) === 'mismatch' ? (
        <p className="mt-2 rounded-md bg-amber-50 px-2 py-1.5 text-xs text-amber-800">
          You entered “{row.typedName}”. This record belongs to the owner of this ORCID, which may be a different person.
        </p>
      ) : null}
    </div>
  )
}

function CombinedHeader({ count }: { count: number }): ReactElement {
  return (
    <div className="mt-4">
      <p className="text-base font-semibold text-gray-900">Whole author team</p>
      <p className="mt-0.5 text-xs text-gray-500">{count} authors · papers shared between them are counted once</p>
    </div>
  )
}

function Tiles({
  isCombined,
  profiles,
  agg,
}: {
  isCombined: boolean
  profiles: AuthorProfile[]
  agg: Aggregate
}): ReactElement {
  const single = profiles[0]
  const tiles = isCombined
    ? [
        { label: 'Unique papers', value: agg.papers },
        { label: 'Citations', value: agg.citations },
        { label: 'Highest h-index', value: Math.max(0, ...profiles.map((p) => p.h_index)) },
        { label: 'Authors', value: profiles.length },
      ]
    : [
        { label: 'Papers', value: single?.works_count ?? 0 },
        { label: 'Citations', value: single?.cited_by_count ?? 0 },
        { label: 'h-index', value: single?.h_index ?? 0 },
        { label: 'i10-index', value: single?.i10_index ?? 0 },
      ]
  return (
    <dl className="mt-4 grid grid-cols-2 gap-2">
      {tiles.map((tile) => (
        <div key={tile.label} className="rounded-lg bg-gray-50 px-3 py-2.5">
          <dd className="text-xl font-semibold tabular-nums text-gray-900">{nf.format(tile.value)}</dd>
          <dt className="text-xs text-gray-500">{tile.label}</dt>
        </div>
      ))}
    </dl>
  )
}

function ChartBlock({ title, note, children }: { title: string; note?: string; children: ReactNode }): ReactElement {
  return (
    <div className="mt-5">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-600">{title}</h3>
      {note ? <p className="text-xs text-gray-500">{note}</p> : null}
      <div className="mt-2">{children}</div>
    </div>
  )
}

const tooltipStyle = {
  fontSize: 12,
  borderRadius: 8,
  border: '1px solid #e5e7eb',
  boxShadow: '0 1px 2px rgba(0,0,0,0.06)',
}

function Charts({ agg }: { agg: Aggregate }): ReactElement {
  const typeTotal = agg.types.reduce((sum, slice) => sum + slice.value, 0)
  const topJournalWorks = Math.max(1, ...agg.journals.map((j) => j.works))

  return (
    <>
      <ChartBlock title="Papers per year">
        {agg.perYear.length ? (
          <div role="img" aria-label={`Papers per year, ${agg.perYear[0].year} to ${agg.perYear[agg.perYear.length - 1].year}`}>
            <ResponsiveContainer width="100%" height={150}>
              <BarChart data={agg.perYear} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
                <XAxis dataKey="year" tick={{ fontSize: 11 }} tickLine={false} interval="preserveStartEnd" minTickGap={16} />
                <YAxis allowDecimals={false} width={40} tick={{ fontSize: 11 }} tickLine={false} axisLine={false} />
                <Tooltip cursor={{ fill: '#f3f4f6' }} contentStyle={tooltipStyle} formatter={(v: number) => [v, 'Papers']} />
                <Bar dataKey="papers" fill="#111827" radius={[3, 3, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <p className="text-xs text-gray-500">No dated papers.</p>
        )}
      </ChartBlock>

      <ChartBlock title="Citations by publication year" note="Citations received so far by the papers of each year.">
        {agg.perYear.length ? (
          <div role="img" aria-label="Citations by publication year">
            <ResponsiveContainer width="100%" height={150}>
              <BarChart data={agg.perYear} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
                <XAxis dataKey="year" tick={{ fontSize: 11 }} tickLine={false} interval="preserveStartEnd" minTickGap={16} />
                <YAxis allowDecimals={false} width={40} tick={{ fontSize: 11 }} tickLine={false} axisLine={false} />
                <Tooltip cursor={{ fill: '#f3f4f6' }} contentStyle={tooltipStyle} formatter={(v: number) => [nf.format(v), 'Citations']} />
                <Bar dataKey="citations" fill="#2563eb" radius={[3, 3, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <p className="text-xs text-gray-500">No dated papers.</p>
        )}
      </ChartBlock>

      <ChartBlock title="Type of paper">
        {agg.types.length ? (
          <div className="flex items-center gap-4">
            <div className="h-[130px] w-[130px] shrink-0" role="img" aria-label="Share of papers by type">
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Pie data={agg.types} dataKey="value" nameKey="name" innerRadius={36} outerRadius={60} paddingAngle={2} stroke="none">
                    {agg.types.map((slice, index) => (
                      <Cell key={slice.name} fill={SLICE_COLORS[index % SLICE_COLORS.length]} />
                    ))}
                  </Pie>
                  <Tooltip contentStyle={tooltipStyle} formatter={(v: number, name: string) => [v, name]} />
                </PieChart>
              </ResponsiveContainer>
            </div>
            <ul className="min-w-0 flex-1 space-y-1 text-xs">
              {agg.types.map((slice, index) => (
                <li key={slice.name} className="flex items-center gap-2">
                  <span
                    className="h-2.5 w-2.5 shrink-0 rounded-sm"
                    style={{ background: SLICE_COLORS[index % SLICE_COLORS.length] }}
                    aria-hidden="true"
                  />
                  <span className="truncate text-gray-700">{slice.name}</span>
                  <span className="ml-auto tabular-nums text-gray-500">
                    {slice.value} · {Math.round((slice.value / typeTotal) * 100)}%
                  </span>
                </li>
              ))}
            </ul>
          </div>
        ) : (
          <p className="text-xs text-gray-500">No papers to chart.</p>
        )}
      </ChartBlock>

      <ChartBlock
        title="Top journals"
        note="Impact is OpenAlex’s 2-year mean citedness, close to the Impact Factor but not the JCR number."
      >
        {agg.journals.length ? (
          <ul className="space-y-2">
            {agg.journals.map((journal) => (
              <li key={journal.id}>
                <div className="flex items-baseline justify-between gap-2 text-xs">
                  <span className="truncate text-gray-800" title={journal.name}>
                    {shortVenueName(journal.name, 40)}
                  </span>
                  <span className="shrink-0 tabular-nums text-gray-500">
                    {journal.works} {journal.works === 1 ? 'paper' : 'papers'}
                    <span
                      className="ml-2 rounded-full bg-gray-100 px-1.5 py-0.5 text-gray-700"
                      title="OpenAlex 2-year mean citedness"
                    >
                      {journal.impact === null ? 'impact n/a' : `impact ${journal.impact.toFixed(2)}`}
                    </span>
                  </span>
                </div>
                <div className="mt-1 h-1.5 rounded-full bg-gray-100">
                  <div
                    className="h-1.5 rounded-full bg-gray-900"
                    style={{ width: `${Math.max(4, (journal.works / topJournalWorks) * 100)}%` }}
                  />
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-xs text-gray-500">No journal articles found.</p>
        )}
        {agg.otherVenues.length ? (
          <p className="mt-3 text-xs text-gray-500">
            Also on: {agg.otherVenues.map((v) => `${shortVenueName(v.name, 24)} (${v.works})`).join(', ')}
          </p>
        ) : null}
      </ChartBlock>
    </>
  )
}

function Footnote({ profiles }: { profiles: AuthorProfile[] }): ReactElement {
  const truncated = profiles.some((p) => p.works_truncated)
  const topics = profiles.length === 1 ? profiles[0].topics.slice(0, 5) : []
  return (
    <div className="mt-5 border-t border-gray-100 pt-3">
      {topics.length ? (
        <div className="mb-3 flex flex-wrap gap-1.5">
          {topics.map((topic) => (
            <span key={topic.name} className="rounded-full bg-gray-100 px-2 py-0.5 text-[11px] text-gray-700">
              {topic.name}
            </span>
          ))}
        </div>
      ) : null}
      <p className="text-[11px] leading-4 text-gray-500">
        Sources: ORCID and OpenAlex (public records). Totals come from the author record; charts use the papers OpenAlex
        lists for them, so numbers can differ slightly from Google Scholar.
        {truncated ? ' Very long publication lists are charted from the first 1,000 papers.' : ''}
      </p>
    </div>
  )
}
