import type { ReactElement, ReactNode } from 'react'
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import Frowns from '../components/Frowns'
import { useRejectaStore } from '../store'
import type { CitationResult, CitationStatus, Mood, RejectionReason, SectionScore } from '../types'

const VISIBLE_CITATIONS = 10

const MOOD_COPY: Record<Mood, { label: string; description: string; pill: string }> = {
  furious: {
    label: 'Furious',
    description: 'Desk rejection certain',
    pill: 'border-red-200 bg-red-50 text-red-700',
  },
  skeptical: {
    label: 'Skeptical',
    description: 'Desk rejection likely',
    pill: 'border-amber-200 bg-amber-50 text-amber-800',
  },
  unimpressed: {
    label: 'Unimpressed',
    description: 'Significant problems',
    pill: 'border-gray-200 bg-gray-100 text-gray-700',
  },
  neutral: {
    label: 'Neutral',
    description: 'Uncertain outcome',
    pill: 'border-gray-200 bg-gray-100 text-gray-700',
  },
  interested: {
    label: 'Interested',
    description: 'Recommend for review',
    pill: 'border-green-200 bg-green-50 text-green-700',
  },
  delighted: {
    label: 'Delighted',
    description: 'Strong — accept likely',
    pill: 'border-green-200 bg-green-50 text-green-700',
  },
}

const SEVERITY_STYLE: Record<RejectionReason['severity'], { border: string; label: string; name: string }> = {
  desk_reject: {
    border: 'border-l-red-500 bg-red-50',
    label: 'text-red-700',
    name: 'Desk reject',
  },
  major: {
    border: 'border-l-amber-500 bg-amber-50',
    label: 'text-amber-800',
    name: 'Major',
  },
  minor: {
    border: 'border-l-gray-400 bg-gray-50',
    label: 'text-gray-600',
    name: 'Minor',
  },
}

const STATUS_BADGE: Record<CitationStatus, string> = {
  verified: 'bg-green-50 text-green-700',
  ghost: 'bg-red-50 text-red-700',
  unverified: 'bg-gray-100 text-gray-600',
}

// Scores at or above this read as polish suggestions, not warnings.
const SUGGESTION_SCORE = 75

function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max)}…` : text
}

function citationMeta(item: CitationResult): string {
  const authors = item.authors ?? []
  const lead = authors.length > 2 ? `${authors[0]} et al.` : authors.join(', ')
  const venue = (item.venue ?? '').replace(/\s+/g, ' ').trim()
  const parts = [lead, venue, item.year ? String(item.year) : '', item.is_preprint ? 'preprint' : '']
  return parts.filter(Boolean).join(' · ') || truncate(item.raw, 140)
}

function sectionLabel(key: string): string {
  const spaced = key.replaceAll('_', ' ')
  return spaced.charAt(0).toUpperCase() + spaced.slice(1)
}

function barClass(score: number): string {
  if (score < 50) return 'bg-red-500'
  if (score <= 70) return 'bg-amber-500'
  return 'bg-green-500'
}

function ringStroke(score: number): string {
  if (score <= 40) return '#ef4444'
  if (score <= 60) return '#f59e0b'
  if (score <= 80) return '#22c55e'
  return '#10b981'
}

export default function Results(): ReactElement {
  const navigate = useNavigate()
  const result = useRejectaStore((state) => state.result)
  const reset = useRejectaStore((state) => state.reset)

  useEffect(() => {
    if (!result) {
      navigate('/app', { replace: true })
    }
  }, [result, navigate])

  if (!result) {
    return <div className="px-4 py-16" />
  }

  const analysis = result.analysis
  const mood = MOOD_COPY[analysis.mood]
  const readerMode = result.meta.mode === 'b'
  const sectionEntries = Object.entries(analysis.sections)

  return (
    <div className="mx-auto max-w-2xl px-4 py-8">
      <section className="rounded-xl border border-gray-200 bg-white p-5">
        <div className="grid grid-cols-1 items-center gap-4 sm:grid-cols-[auto_1fr_auto]">
          <Frowns mood={analysis.mood} size={80} />
          <div>
            <p className="text-xs text-gray-500">Prof. Aldric Frowns · DPhil (Cantab)</p>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <span className={`rounded-full border px-2 py-0.5 text-xs font-medium ${mood.pill}`}>
                {mood.label}
              </span>
              <span className="text-sm text-gray-700">{mood.description}</span>
            </div>
            <p className="mt-2 text-sm italic text-gray-500">{analysis.verdict}</p>
          </div>
          <ScoreRing score={analysis.mood_score} />
        </div>
      </section>

      {/* Voice Calibrator Quick Action Callout */}
      <div className="mt-4 flex items-center justify-between rounded-xl border border-stone-200 bg-stone-50 p-4">
        <div className="flex items-center gap-3">
          <span className="flex h-8 w-8 items-center justify-center rounded-full bg-stone-900 text-xs text-white">
            ✨
          </span>
          <div>
            <p className="text-xs font-semibold uppercase tracking-wider text-stone-900">
              Voice Calibrator Available
            </p>
            <p className="text-xs text-stone-600">
              Refine your draft sections into the published register of <span className="font-medium text-stone-900">{result.journal.target}</span>.
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() =>
              navigate('/forge', {
                state: {
                  journal: result.journal.target,
                },
              })
            }
            className="shrink-0 rounded-lg border border-purple-300 bg-purple-50 px-3 py-1.5 text-xs font-semibold text-purple-900 hover:bg-purple-100 transition"
          >
            🔨 Abstract Forge
          </button>
          <button
            type="button"
            onClick={() =>
              navigate('/heatmap', {
                state: {
                  journal: result.journal.target,
                },
              })
            }
            className="shrink-0 rounded-lg border border-stone-300 bg-white px-3 py-1.5 text-xs font-medium text-stone-800 hover:bg-stone-100 transition"
          >
            Cadence Heatmap
          </button>
          <button
            type="button"
            onClick={() =>
              navigate('/calibrate', {
                state: {
                  journal: result.journal.target,
                },
              })
            }
            className="shrink-0 rounded-lg bg-stone-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-stone-800 transition"
          >
            Voice Calibrator →
          </button>
        </div>
      </div>

      <div className="mt-4">
        <Panel title="Citation autopsy" badge="free" icon="⌕">
          <div className="flex flex-wrap gap-2">
            <Chip className="bg-green-50 text-green-700">{result.citations.verified} verified</Chip>
            <Chip className="bg-red-50 text-red-700">{result.citations.ghost} ghost</Chip>
            <Chip className="bg-gray-100 text-gray-600">{result.citations.unverified} unverified</Chip>
          </div>
          {result.citations.editorial_warnings?.length ? (
            <ul className="mt-3 space-y-1">
              {result.citations.editorial_warnings.map((warning) => (
                <li key={warning} className="text-xs text-gray-500">⚠ {warning}</li>
              ))}
            </ul>
          ) : null}
          <CitationTable items={result.citations.items} total={result.citations.total} />
        </Panel>

        <Panel title="Editor's verdict" badge="pro" icon="✎">
          <div className="rounded-lg bg-gray-50 p-4">
            <p className="font-serif text-sm leading-relaxed text-gray-800">{analysis.oracle_letter}</p>
            <p className="mt-4 text-sm text-gray-500">— Prof. Aldric Frowns, Editorial Board</p>
          </div>
        </Panel>

        <Panel title="Section analysis" badge="pro" icon="▤">
          {sectionEntries.length === 0 ? (
            <p className="text-sm text-gray-500">Section data not available</p>
          ) : (
            <div className="space-y-4">
              {sectionEntries.map(([name, section]) => (
                <SectionRow
                  key={name}
                  name={name}
                  section={section}
                  readerMode={readerMode}
                  targetJournal={result.journal.target}
                />
              ))}
            </div>
          )}
        </Panel>

        <Panel title="Rejection reasons" badge="pro" icon="!">
          {analysis.rejection_reasons.length === 0 ? (
            <p className="text-sm text-gray-500">No major rejection risks identified</p>
          ) : (
            <div className="space-y-2">
              {analysis.rejection_reasons.map((item, index) => (
                <ReasonCard
                  key={`${item.severity}-${index}`}
                  item={item}
                  targetJournal={result.journal.target}
                />
              ))}
            </div>
          )}
        </Panel>

        <Panel title="Novelty assessment" badge="pro" icon="✦">
          <p className="text-sm leading-6 text-gray-800">{analysis.novelty_assessment}</p>
        </Panel>

        <Panel title="Journal fit" badge="pro" icon="▣">
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-sm font-medium text-gray-900">
              {result.journal.openalex_name ?? result.journal.target}
            </p>
            <Chip className={result.journal.found ? 'bg-green-50 text-green-700' : 'bg-gray-100 text-gray-600'}>
              {result.journal.found ? 'found' : 'not found'}
            </Chip>
          </div>
          {result.journal.found ? (
            <div className="mt-3">
              {typeof result.journal.works_count === 'number' ? (
                <p className="text-sm text-gray-600">
                  {result.journal.works_count.toLocaleString()} works indexed
                </p>
              ) : null}
              {result.journal.recent_papers.length > 0 ? (
                <ul className="mt-2 space-y-1">
                  {result.journal.recent_papers.slice(0, 3).map((paper) => (
                    <li key={`${paper.year}-${paper.title}`} className="text-sm text-gray-700">
                      {paper.title}
                      {paper.year != null ? <span className="text-gray-400"> ({paper.year})</span> : null}
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
          ) : null}
          <p className="mt-3 text-sm leading-6 text-gray-800">{analysis.journal_fit}</p>
        </Panel>
      </div>

      <p className="mt-4 text-xs text-gray-500">
        {result.meta.word_count.toLocaleString()} words · {result.meta.references_found} references ·{' '}
        {result.meta.sections_found.length} sections detected · {(result.meta.processing_ms / 1000).toFixed(1)}s ·{' '}
        {analysis.provider_used}
      </p>

      <div className="mt-4 flex flex-wrap gap-2 print:hidden">
        <button
          type="button"
          onClick={() => {
            reset()
            navigate('/app')
          }}
          className="rounded-lg bg-black px-4 py-2 text-sm font-medium text-white"
        >
          Analyse another paper
        </button>
        <button
          type="button"
          onClick={() => window.print()}
          className="rounded-lg border border-gray-300 bg-white px-4 py-2 text-sm text-gray-800"
        >
          Print or save PDF
        </button>
        <button
          type="button"
          onClick={() =>
            navigate('/calibrate', {
              state: {
                journal: result.journal.target,
              },
            })
          }
          className="rounded-lg border border-stone-300 bg-white px-4 py-2 text-sm font-medium text-stone-800 hover:bg-stone-50 transition"
        >
          ✨ How do I fix this? (Voice Calibrator)
        </button>
        <button
          type="button"
          onClick={() =>
            navigate('/heatmap', {
              state: {
                journal: result.journal.target,
              },
            })
          }
          className="rounded-lg border border-stone-300 bg-white px-4 py-2 text-sm font-medium text-stone-800 hover:bg-stone-50 transition"
        >
          📊 Inspect Cadence (Heatmap)
        </button>
      </div>
    </div>
  )
}

function ScoreRing({ score }: { score: number }): ReactElement {
  const radius = 34
  const circumference = 2 * Math.PI * radius
  const clamped = Math.min(100, Math.max(0, score))
  const offset = circumference * (1 - clamped / 100)

  return (
    <div className="relative mx-auto h-24 w-24">
      <svg viewBox="0 0 80 80" className="h-24 w-24 -rotate-90" aria-hidden="true">
        <circle cx="40" cy="40" r={radius} fill="none" stroke="#e5e7eb" strokeWidth="6" />
        <circle
          cx="40"
          cy="40"
          r={radius}
          fill="none"
          stroke={ringStroke(clamped)}
          strokeWidth="6"
          strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={offset}
        />
      </svg>
      <div className="absolute inset-0 flex items-center justify-center text-2xl font-semibold">
        {score}
      </div>
    </div>
  )
}

function Panel({
  title,
  badge,
  icon,
  children,
}: {
  title: string
  badge: 'free' | 'pro'
  icon: string
  children: ReactNode
}): ReactElement {
  const [open, setOpen] = useState(true)

  return (
    <section className="mb-2 rounded-xl border border-gray-200 bg-white">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left"
      >
        <span className="flex items-center gap-2">
          <span className="text-sm text-gray-500" aria-hidden="true">{icon}</span>
          <span className="text-sm font-medium">{title}</span>
          {badge === 'free' ? (
            <span className="rounded-full bg-green-50 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-green-700">
              free
            </span>
          ) : (
            <span className="rounded-full bg-purple-50 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-purple-700">
              pro
            </span>
          )}
        </span>
        <span className={`text-gray-400 transition-transform ${open ? 'rotate-180' : ''}`} aria-hidden="true">
          ▾
        </span>
      </button>
      <div className={`grid transition-[grid-template-rows] duration-300 ${open ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'}`}>
        <div className="overflow-hidden">
          <div className="border-t border-gray-100 px-4 py-3">{children}</div>
        </div>
      </div>
    </section>
  )
}

function Chip({ className, children }: { className: string; children: ReactNode }): ReactElement {
  return <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${className}`}>{children}</span>
}

function CitationTable({
  items,
  total,
}: {
  items: CitationResult[]
  total: number
}): ReactElement {
  const [showAll, setShowAll] = useState(false)
  const visible = showAll ? items : items.slice(0, VISIBLE_CITATIONS)

  if (items.length === 0) {
    return <p className="mt-3 text-sm text-gray-500">No references extracted</p>
  }

  return (
    <div className="mt-3">
      <ul className="divide-y divide-gray-100">
        {visible.map((item, index) => (
          <li key={`${item.status}-${index}`} className="flex items-start gap-3 py-2">
            <span className={`mt-0.5 shrink-0 rounded-full px-2 py-0.5 text-[10px] font-medium uppercase ${STATUS_BADGE[item.status]}`}>
              {item.status}
            </span>
            <span className="text-sm text-gray-700">
              {item.title ? (
                <>
                  <span className="font-medium text-gray-900">{item.title}</span>
                  <span className="mt-0.5 block text-xs text-gray-500">{citationMeta(item)}</span>
                  {item.source_url ? (
                    <a
                      href={item.source_url}
                      target="_blank"
                      rel="noreferrer"
                      className="mt-0.5 block text-xs text-sky-700 underline"
                    >
                      View source
                    </a>
                  ) : null}
                </>
              ) : (
                <>
                  {truncate(item.raw, 140)}
                  {item.status !== 'verified' && item.diagnostic_reason ? (
                    <span className="mt-0.5 block text-xs text-gray-500">{item.diagnostic_reason}</span>
                  ) : null}
                </>
              )}
            </span>
          </li>
        ))}
      </ul>
      {items.length > VISIBLE_CITATIONS ? (
        <button
          type="button"
          onClick={() => setShowAll((value) => !value)}
          className="mt-2 text-sm text-gray-700 underline"
        >
          {showAll ? 'Show fewer' : `Show all ${total}`}
        </button>
      ) : null}
    </div>
  )
}

function SectionRow({
  name,
  section,
  readerMode,
  targetJournal,
}: {
  name: string
  section: SectionScore
  readerMode: boolean
  targetJournal: string
}): ReactElement {
  const navigate = useNavigate()
  const width = Math.min(100, Math.max(0, section.score))
  const [showFix, setShowFix] = useState(false)

  const handleCalibrateSection = (): void => {
    const promptSample = `[${sectionLabel(name)} Section]\nIssue to address: ${section.issue ?? 'Improve scientific register'}\nTarget remediation: ${section.fix ?? 'Align with journal standard'}`
    navigate('/calibrate', {
      state: {
        text: promptSample,
        journal: targetJournal,
      },
    })
  }

  const handleHeatmapSection = (): void => {
    const promptSample = `[${sectionLabel(name)} Section]\nIssue to address: ${section.issue ?? 'Analyze sentence rhythm and transition density'}\nRemediation focus: ${section.fix ?? 'Vary sentence length and clarify active agency'}`
    navigate('/heatmap', {
      state: {
        text: promptSample,
        journal: targetJournal,
      },
    })
  }
  return (
    <div>
      <div className="flex items-center gap-3">
        <p className="w-28 shrink-0 text-sm font-medium capitalize">{sectionLabel(name)}</p>
        <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-gray-100">
          <div className={`h-full ${barClass(section.score)}`} style={{ width: `${width}%` }} />
        </div>
        <span className="w-8 text-right text-sm text-gray-700">{section.score}</span>
      </div>
      {readerMode ? (
        <div className="mt-1 space-y-1 pl-0 sm:pl-[7.5rem]">
          {section.strength ? <p className="text-sm text-green-700">→ {section.strength}</p> : null}
          {section.weakness ? <p className="text-sm text-amber-700">⚠ {section.weakness}</p> : null}
        </div>
      ) : (
        <div className="mt-1 space-y-1 sm:pl-[7.5rem]">
          {section.strength ? <p className="text-sm text-green-700">✓ {section.strength}</p> : null}
          {section.issue ? (
            <p className="text-sm text-gray-500">
              {section.score < SUGGESTION_SCORE ? '⚠' : '•'} {section.issue}
            </p>
          ) : null}
          {section.fix ? (
            <div className="flex flex-wrap items-center gap-2 pt-1">
              <button
                type="button"
                onClick={() => setShowFix((value) => !value)}
                className="text-xs text-stone-500 hover:text-stone-800 underline mr-1"
              >
                {showFix ? 'Hide fix' : 'How to fix'}
              </button>
              {name.toLowerCase() === 'abstract' && (
                <button
                  onClick={() =>
                    navigate('/forge', {
                      state: {
                        text: `[Abstract Section]\nIssue to address: ${section.issue ?? 'Synthesize 3 targeted architectures'}\nRemediation focus: ${section.fix ?? 'Discoverability, Clarity, and High-Impact Narrative'}`,
                        journal: targetJournal,
                      },
                    })
                  }
                  className="inline-flex items-center gap-1 rounded border border-purple-300 bg-purple-50 px-2 py-0.5 text-[11px] font-semibold text-purple-900 hover:bg-purple-100 transition"
                >
                  <span>🔨 Forge 3 Abstracts</span>
                </button>
              )}
              <button
                type="button"
                onClick={handleHeatmapSection}
                className="inline-flex items-center gap-1 rounded border border-stone-300 bg-white px-2 py-0.5 text-[11px] font-medium text-stone-700 hover:bg-stone-50 transition"
              >
                <span>📊 Heatmap</span>
              </button>
              <button
                type="button"
                onClick={handleCalibrateSection}
                className="inline-flex items-center gap-1 rounded bg-stone-900 px-2 py-0.5 text-[11px] font-medium text-white hover:bg-stone-800 transition"
              >
                <span>✨ Calibrate {sectionLabel(name)}</span>
              </button>
              {showFix ? <p className="w-full mt-1 text-sm text-sky-700">→ {section.fix}</p> : null}
            </div>
          ) : null}
        </div>
      )}
    </div>
  )
}

function ReasonCard({
  item,
  targetJournal,
}: {
  item: RejectionReason
  targetJournal: string
}): ReactElement {
  const navigate = useNavigate()
  const style = SEVERITY_STYLE[item.severity]

  const handleFixReason = (): void => {
    const promptText = `[Addressing Rejection Risk: ${item.reason}]\nRequired Remediation: ${item.fix}\nTask: Rephrase and strengthen manuscript evidence according to this directive.`
    navigate('/calibrate', {
      state: {
        text: promptText,
        journal: targetJournal,
      },
    })
  }

  return (
    <article className={`rounded-lg border border-gray-200 border-l-4 p-3.5 ${style.border}`}>
      <div className="flex items-center justify-between">
        <p className={`text-[10px] font-bold uppercase tracking-wide ${style.label}`}>{style.name}</p>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() =>
              navigate('/rebuttal', {
                state: {
                  rejection_reason: `${item.reason}\nSuggested remediation: ${item.fix}`,
                  journal: targetJournal,
                },
              })
            }
            className="inline-flex items-center gap-1 rounded border border-stone-300 bg-white px-2 py-0.5 text-[10.5px] font-medium text-stone-700 hover:bg-stone-50 transition"
          >
            <span>📝 Draft Rebuttal Strategy</span>
          </button>
          <button
            type="button"
            onClick={handleFixReason}
            className="inline-flex items-center gap-1 rounded bg-stone-900 px-2 py-0.5 text-[10.5px] font-semibold text-white hover:bg-stone-800 transition"
          >
            <span>⚡ Fix with Calibrator</span>
          </button>
        </div>
      </div>
      <p className="mt-1.5 text-sm text-gray-800 font-medium">{item.reason}</p>
      <p className="mt-1 text-sm text-sky-700">→ {item.fix}</p>
    </article>
  )
}
