import type { ReactElement, ReactNode } from 'react'
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { deleteDocument, downloadDocument, submitPaper, undoDocument } from '../api'
import Frowns from '../components/Frowns'
import { useRejectaStore } from '../store'
import type {
  CitationResult,
  CitationStatus,
  ConsistencyReport,
  Disclosures,
  DocumentInfo,
  Mood,
  PublicationStatus,
  RejectionReason,
  RevisionComparison,
  ScoreChange,
  FixStep,
  SectionCheck,
  SectionScore,
} from '../types'

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

const ARTICLE_TYPE_LABEL: Record<string, string> = {
  research_article: 'Full paper',
  communication: 'Communication',
  perspective: 'Perspective',
  review: 'Review article',
  mini_review: 'Mini-review',
  editorial: 'Editorial',
}

const IMPACT_AREA_LABEL: Record<string, string> = {
  novelty: 'Novelty',
  scientific_validity: 'Scientific validity',
  publication_potential: 'Publication potential',
}

const CRITERION_LABEL: Record<string, string> = {
  novelty: 'Novelty',
  rigor: 'Rigor',
  evidence_support: 'Evidence',
  clarity: 'Clarity',
  journal_fit: 'Journal fit',
  coverage_balance: 'Coverage & balance',
  critical_synthesis: 'Critical synthesis',
  outlook: 'Outlook',
  purpose: 'Purpose',
  context_accuracy: 'Context & accuracy',
  citation_integrity: 'Citation integrity',
}

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
  const lastSubmission = useRejectaStore((state) => state.lastSubmission)
  const [rerunning, setRerunning] = useState(false)
  const [rerunError, setRerunError] = useState<string | null>(null)

  // "Run again": the same file and settings, analysed afresh instead of returning the earlier report.
  const runAgain = (): void => {
    if (!lastSubmission || rerunning) return
    const { file, journal, mode, email, articleType, keepCopy } = lastSubmission
    setRerunning(true)
    setRerunError(null)
    submitPaper(file, journal, mode, email, articleType, keepCopy, true)
      .then((data) => {
        useRejectaStore.getState().setResult(data)
        window.scrollTo({ top: 0 })
      })
      .catch((err: unknown) => setRerunError(err instanceof Error ? err.message : 'Analysis failed — please try again'))
      .finally(() => setRerunning(false))
  }

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
  const typeLabel = analysis.paper_format ? ARTICLE_TYPE_LABEL[analysis.paper_format] : undefined
  const criteriaEntries = Object.entries(analysis.criteria ?? {})
  const notAssessed = analysis.not_assessed ?? []
  const groundingNotes = analysis.grounding_notes ?? []
  const derivedSections = result.meta.structure_mode === 'derived'
  const integrity = result.integrity ?? null
  const retracted = integrity?.status === 'retracted'
  // A published paper passed peer review: what the report raises are reviewer concerns, not rejection reasons.
  const published = integrity?.status === 'published' || integrity?.status === 'corrected'
  const consistencyReport = result.consistency ?? null
  const disclosures = result.disclosures ?? null
  const unverifiedConcerns = analysis.unverified_concerns ?? []
  const paperCopy = result.document ?? null
  // section name in the report -> section of the working copy, when it was found there
  const copySections = new Map((paperCopy?.sections ?? []).filter((s) => s.mapped).map((s) => [s.name, s.id]))

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
              <span className="text-sm text-gray-700">
                {retracted ? 'Retracted — no recommendation' : mood.description}
              </span>
              {typeLabel ? <Chip className="bg-stone-100 text-stone-700">{typeLabel}</Chip> : null}
            </div>
            <p className="mt-2 text-sm italic text-gray-500">{analysis.verdict}</p>
          </div>
          <ScoreRing score={analysis.mood_score} />
        </div>
        {result.meta.cached || lastSubmission ? (
          <div className="mt-4 flex flex-wrap items-center justify-between gap-2 border-t border-gray-100 pt-3">
            <p className="text-xs text-gray-500">
              {result.meta.cached
                ? `You analysed this exact file with the same settings${formatWhen(result.meta.analysed_at)}. This is that report, so the same file always gets the same result.`
                : result.meta.analysed_at
                  ? `Analysed${formatWhen(result.meta.analysed_at)}.`
                  : null}
            </p>
            {lastSubmission ? (
              <button
                type="button"
                onClick={runAgain}
                disabled={rerunning}
                className="shrink-0 rounded-lg border border-stone-300 bg-white px-3 py-1.5 text-xs font-medium text-stone-800 hover:bg-stone-100 transition disabled:opacity-50"
              >
                {rerunning ? 'Analysing again…' : '↻ Run again'}
              </button>
            ) : null}
          </div>
        ) : null}
        {rerunError ? <p className="mt-2 text-xs text-red-700">{rerunError}</p> : null}
      </section>

      {integrity ? <IntegrityBanner integrity={integrity} /> : null}

      {result.revision ? <RevisionPanel revision={result.revision} /> : null}

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
        {criteriaEntries.length > 0 || notAssessed.length > 0 || groundingNotes.length > 0 ? (
          <Panel title="Score breakdown" badge="pro" icon="≡">
            {criteriaEntries.length > 0 ? (
              <div className="space-y-3">
                {criteriaEntries.map(([key, item]) => (
                  <div key={key}>
                    <div className="flex items-center gap-3">
                      <p className="w-36 shrink-0 text-sm font-medium">{CRITERION_LABEL[key] ?? key.replaceAll('_', ' ')}</p>
                      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-gray-100">
                        <div
                          className={`h-full ${barClass(item.score)}`}
                          style={{ width: `${Math.min(100, Math.max(0, item.score))}%` }}
                        />
                      </div>
                      <span className="w-8 text-right text-sm text-gray-700">{item.score}</span>
                    </div>
                    {(item.adjustments ?? []).map((note) => (
                      <p key={note} className="mt-1 text-xs text-amber-800 sm:pl-[9.75rem]">
                        {note}
                        {typeof item.raw_score === 'number' ? ` (the model gave ${item.raw_score})` : ''}
                      </p>
                    ))}
                    {typeof item.coverage === 'number' ? (
                      <p className="mt-1 text-xs text-gray-500 sm:pl-[9.75rem]">
                        Coverage {Math.round(item.coverage * 100)}% of references settled
                      </p>
                    ) : null}
                    {item.reason ? (
                      <p className="mt-1 text-xs text-gray-500 sm:pl-[9.75rem]">
                        {item.computed ? 'Computed from the reference check. ' : ''}
                        {item.reason}
                      </p>
                    ) : null}
                  </div>
                ))}
                <p className="text-xs text-gray-400">
                  The overall score is the weighted mean of these criteria, so it stays comparable between papers.
                  Limits applied by code (publication record, inconsistencies in the paper, verified concerns) are
                  shown under each criterion.
                </p>
              </div>
            ) : null}
            {notAssessed.length > 0 ? (
              <div className="mt-4 rounded-lg border border-amber-200 bg-amber-50 p-3">
                <p className="text-xs font-semibold uppercase tracking-wide text-amber-800">Not assessed</p>
                <ul className="mt-1 space-y-1">
                  {notAssessed.map((note) => (
                    <li key={note} className="text-xs text-amber-900">{note}</li>
                  ))}
                </ul>
              </div>
            ) : null}
            {groundingNotes.length > 0 ? (
              <div className="mt-4 rounded-lg border border-gray-200 bg-gray-50 p-3">
                <p className="text-xs font-semibold uppercase tracking-wide text-gray-600">Checked against your manuscript</p>
                <ul className="mt-1 space-y-1">
                  {groundingNotes.map((note) => (
                    <li key={note} className="text-xs text-gray-600">{note}</li>
                  ))}
                </ul>
              </div>
            ) : null}
          </Panel>
        ) : null}

        {paperCopy || result.document_note ? (
          <Panel title="Your editable paper" badge="pro" icon="✎">
            {paperCopy ? <PaperCopyPanel copy={paperCopy} /> : <p className="text-sm text-amber-800">{result.document_note}</p>}
          </Panel>
        ) : null}

        <Panel title="Citation autopsy" badge="free" icon="⌕">
          <div className="flex flex-wrap gap-2">
            <Chip className="bg-green-50 text-green-700">{result.citations.verified} verified</Chip>
            <Chip className="bg-red-50 text-red-700">{result.citations.ghost} ghost</Chip>
            <Chip className="bg-gray-100 text-gray-600">
              {result.citations.unverified - (result.citations.check_incomplete ?? 0)} unverified
            </Chip>
            {result.citations.check_incomplete ? (
              <Chip className="bg-amber-50 text-amber-700">{result.citations.check_incomplete} not checked</Chip>
            ) : null}
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
              {derivedSections ? (
                <p className="text-xs text-gray-500">
                  This file has no standard section headings, so the sections below were identified from the content.
                </p>
              ) : null}
              {sectionEntries.map(([name, section]) => (
                <SectionRow
                  key={name}
                  name={name}
                  section={section}
                  readerMode={readerMode}
                  targetJournal={result.journal.target}
                  field={analysis.field}
                  copy={paperCopy && copySections.has(name) ? { documentId: paperCopy.id, sectionId: copySections.get(name) as string } : null}
                />
              ))}
            </div>
          )}
        </Panel>

        <Panel title={published ? 'Reviewer concerns' : 'Rejection reasons'} badge="pro" icon="!">
          {published ? (
            <p className="mb-3 text-xs text-gray-500">
              This paper is already published, so these are points a reviewer could raise, not reasons it would be rejected.
            </p>
          ) : null}
          {analysis.rejection_reasons.length === 0 ? (
            <p className="text-sm text-gray-500">
              {unverifiedConcerns.length > 0
                ? 'No concern could be tied to a passage of your manuscript. The ones raised are listed below as unverified.'
                : published
                  ? 'No concrete concerns identified'
                  : 'No major rejection risks identified'}
            </p>
          ) : (
            <div className="space-y-2">
              {analysis.rejection_reasons.map((item, index) => (
                <ReasonCard
                  key={`${item.severity}-${index}`}
                  item={item}
                  targetJournal={result.journal.target}
                  field={analysis.field}
                  sectionText={item.section ? analysis.sections[item.section]?.tool_text : null}
                  copy={
                    paperCopy && item.section && copySections.has(item.section)
                      ? { documentId: paperCopy.id, sectionId: copySections.get(item.section) as string }
                      : null
                  }
                />
              ))}
            </div>
          )}
        </Panel>

        {consistencyReport && consistencyReport.status !== 'not_applicable' ? (
          <Panel title="Consistency check" badge="pro" icon="≠">
            <ConsistencyPanel report={consistencyReport} />
          </Panel>
        ) : null}

        {disclosures && (disclosures.competing_interests || disclosures.funding) ? (
          <Panel title="Disclosures" badge="pro" icon="§">
            <DisclosuresPanel disclosures={disclosures} />
          </Panel>
        ) : null}

        {unverifiedConcerns.length > 0 ? (
          <Panel title="Unverified concerns" badge="pro" icon="?">
            <p className="mb-3 text-xs text-gray-500">
              Raised by the model but not tied to any passage of your manuscript. They are not counted as rejection
              reasons and do not affect the score. Check each one against your paper before acting on it.
            </p>
            <ul className="space-y-2">
              {unverifiedConcerns.map((item, index) => (
                <li key={`unverified-${index}`} className="rounded-lg border border-dashed border-gray-300 bg-white p-3">
                  <p className="text-sm text-gray-800">{item.problem || item.reason}</p>
                  {item.unverified_reason ? (
                    <p className="mt-1 text-xs text-amber-800">{item.unverified_reason}</p>
                  ) : null}
                  {item.solution || item.fix ? (
                    <p className="mt-1 text-xs text-gray-500">Suggested: {item.solution || item.fix}</p>
                  ) : null}
                </li>
              ))}
            </ul>
          </Panel>
        ) : null}

        <Panel title="Novelty assessment" badge="pro" icon="✦">
          <p className="text-sm leading-6 text-gray-800">{analysis.novelty_assessment}</p>
        </Panel>

        <Panel title="Journal fit" badge="pro" icon="▣">
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-sm font-medium text-gray-900">
              {result.journal.openalex_name ?? result.journal.target}
            </p>
            <Chip
              className={
                !result.journal.found
                  ? 'bg-gray-100 text-gray-600'
                  : result.journal.match_quality === 'approximate'
                    ? 'bg-amber-50 text-amber-800'
                    : 'bg-green-50 text-green-700'
              }
            >
              {!result.journal.found ? 'not found' : result.journal.match_quality === 'approximate' ? 'closest match' : 'found'}
            </Chip>
          </div>
          {result.journal.note ? <p className="mt-2 text-sm text-amber-800">{result.journal.note}</p> : null}
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
        {result.meta.sections_found.length} sections {derivedSections ? 'identified' : 'detected'} ·{' '}
        {(result.meta.processing_ms / 1000).toFixed(1)}s ·{' '}
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

function PaperCopyPanel({ copy }: { copy: DocumentInfo }): ReactElement {
  const setDocument = useRejectaStore((state) => state.setDocument)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)

  const run = async (action: () => Promise<void>): Promise<void> => {
    setBusy(true)
    setMessage(null)
    try {
      await action()
    } catch (err: unknown) {
      setMessage(err instanceof Error ? err.message : 'Something went wrong')
    } finally {
      setBusy(false)
    }
  }

  const base = copy.filename.replace(/\.[^.]+$/, '') || 'manuscript'
  return (
    <div className="text-sm text-gray-700">
      <p>
        Version {copy.version} of <span className="font-medium">{copy.filename}</span> · kept until{' '}
        {new Date(copy.expires_at).toLocaleString()}
      </p>
      <p className="mt-1 text-xs text-gray-500">
        Use Calibrate on a section below, then “Update in paper” to write the result into this copy.
      </p>
      {copy.notes.map((note) => (
        <p key={note} className="mt-1 text-xs text-gray-500">{note}</p>
      ))}
      <div className="mt-3 flex flex-wrap gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={() => run(() => downloadDocument(copy.id, `${base}-rejecta-v${copy.version}.docx`))}
          className="rounded bg-stone-900 px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
        >
          Download paper (.docx)
        </button>
        <button
          type="button"
          disabled={busy || !copy.can_undo}
          onClick={() => run(async () => setDocument(await undoDocument(copy.id)))}
          className="rounded border border-stone-300 px-3 py-1.5 text-xs font-medium text-stone-800 disabled:opacity-40"
        >
          Undo last change
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() =>
            run(async () => {
              await deleteDocument(copy.id)
              setDocument(null)
            })
          }
          className="rounded border border-red-200 px-3 py-1.5 text-xs font-medium text-red-700 disabled:opacity-50"
        >
          Delete copy now
        </button>
      </div>
      {message ? <p className="mt-2 text-xs text-red-700">{message}</p> : null}
    </div>
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
            <span
              className={`mt-0.5 shrink-0 rounded-full px-2 py-0.5 text-[10px] font-medium uppercase ${
                item.check_incomplete ? 'bg-amber-50 text-amber-700' : STATUS_BADGE[item.status]
              }`}
            >
              {item.check_incomplete ? 'not checked' : item.status}
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

/** A section's fix plan: numbered steps, most important first, each with the words it is about and what to do. */
function FixPlan({ plan }: { plan: FixStep[] }): ReactElement {
  return (
    <ol className="mt-2 w-full space-y-2">
      {plan.map((item) => (
        <li
          key={item.step}
          className={`rounded-md border-l-2 px-3 py-2 text-sm ${item.severity === 'major' ? 'border-red-400 bg-red-50/50' : 'border-amber-300 bg-amber-50/40'}`}
        >
          <p className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">
            Step {item.step} · {item.severity}
            {item.source === 'checklist' ? ' · found by code' : ''}
          </p>
          <p className="text-gray-800">{item.problem}</p>
          {item.quote ? (
            <blockquote className="mt-1 border-l-2 border-gray-200 pl-2 text-xs italic text-gray-500">“{item.quote}”</blockquote>
          ) : null}
          <p className="mt-1 text-sky-700">→ {item.action}</p>
        </li>
      ))}
    </ol>
  )
}

/** The section review: what the section does, what is good, and the problems, each with the words it is about. */
function SectionReviewBlock({ section }: { section: SectionScore }): ReactElement {
  const strengths = section.strengths ?? []
  const findings = section.findings ?? []
  const basis = section.score_basis ?? []
  const [showBasis, setShowBasis] = useState(false)
  return (
    <div className="mt-2 space-y-2 sm:pl-[7.5rem]">
      {section.summary ? <p className="text-sm text-gray-700">{section.summary}</p> : null}
      {basis.length > 0 ? (
        <div className="text-xs">
          <button
            type="button"
            onClick={() => setShowBasis((value) => !value)}
            aria-expanded={showBasis}
            className="text-stone-500 underline hover:text-stone-800"
          >
            {showBasis ? 'Hide how this score was set ▴' : 'How this score was set ▾'}
          </button>
          {showBasis ? (
            <ul className="mt-1 space-y-0.5 text-gray-500">
              {basis.map((step, index) => (
                <li key={index}>{step}</li>
              ))}
              <li className="pt-0.5 text-gray-400">
                Computed from the findings and checklist below, so the same findings always give the same score.
              </li>
            </ul>
          ) : null}
        </div>
      ) : null}
      {strengths.map((item, index) => (
        <div key={`s-${index}`} className="text-sm">
          <p className="text-green-700">✓ {item.point}</p>
          {item.quote ? (
            <blockquote className="ml-4 mt-0.5 border-l-2 border-green-100 pl-2 text-xs italic text-gray-500">“{item.quote}”</blockquote>
          ) : null}
        </div>
      ))}
      {findings.map((item, index) => (
        <div
          key={`f-${index}`}
          className={`rounded-md border-l-2 px-3 py-2 text-sm ${item.severity === 'major' ? 'border-red-400 bg-red-50/50' : 'border-amber-300 bg-amber-50/40'}`}
        >
          <p className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">{item.severity}</p>
          <p className="text-gray-800">{item.problem}</p>
          <blockquote className="mt-1 border-l-2 border-gray-200 pl-2 text-xs italic text-gray-500">“{item.quote}”</blockquote>
          <p className="mt-1 text-sky-700">→ {item.fix}</p>
        </div>
      ))}
    </div>
  )
}

const CHECK_MARK: Record<SectionCheck['status'], { mark: string; tone: string }> = {
  met: { mark: '✓', tone: 'text-green-700' },
  missing: { mark: '✗', tone: 'text-red-700' },
  issue: { mark: '!', tone: 'text-amber-700' },
  info: { mark: 'i', tone: 'text-gray-500' },
}

/** The section's checklist, decided by code from the text: problems first, then what the paper does, then facts. */
function SectionChecklist({ checks }: { checks: SectionCheck[] }): ReactElement {
  const rank = (c: SectionCheck): number =>
    c.status === 'met' ? 3 : c.status === 'info' ? 4 : c.severity === 'major' ? 0 : c.status === 'missing' ? 1 : 2
  const ordered = [...checks].sort((a, b) => rank(a) - rank(b))
  const problems = checks.filter((c) => c.status === 'missing' || c.status === 'issue').length
  const [open, setOpen] = useState<number | null>(null)
  return (
    <div className="mt-2 sm:pl-[7.5rem]">
      <p className="text-[11px] font-semibold uppercase tracking-wide text-gray-400">
        Checklist · {problems === 0 ? 'no problems found' : `${problems} to check`}
        <span className="font-normal normal-case tracking-normal"> · read by code from your text</span>
      </p>
      <ul className="mt-1 space-y-1">
        {ordered.map((check, index) => {
          const { mark, tone } = CHECK_MARK[check.status]
          const expandable = Boolean(check.quote || check.detail || check.fix)
          return (
            <li key={`${check.key}-${index}`} className="text-sm">
              <button
                type="button"
                disabled={!expandable}
                onClick={() => setOpen(open === index ? null : index)}
                aria-expanded={open === index}
                className="flex w-full items-start gap-2 text-left disabled:cursor-default"
              >
                <span className={`w-3 shrink-0 text-center font-semibold ${tone}`} aria-hidden="true">{mark}</span>
                <span className="flex-1 text-gray-700">
                  {check.label}
                  {check.severity === 'major' ? (
                    <span className="ml-2 rounded bg-red-50 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-red-700">major</span>
                  ) : null}
                </span>
                {expandable ? <span className="text-xs text-gray-400">{open === index ? '▴' : '▾'}</span> : null}
              </button>
              {open === index ? (
                <div className="ml-5 mt-1 space-y-1 text-xs">
                  {check.detail ? <p className="text-gray-600">{check.detail}</p> : null}
                  {check.quote ? (
                    <blockquote className="border-l-2 border-gray-200 pl-2 italic text-gray-500">“{check.quote}”</blockquote>
                  ) : null}
                  {check.fix ? <p className="text-sky-700">→ {check.fix}</p> : null}
                </div>
              ) : null}
            </li>
          )
        })}
      </ul>
    </div>
  )
}

function SectionRow({
  name,
  section,
  readerMode,
  targetJournal,
  field,
  copy,
}: {
  name: string
  section: SectionScore
  readerMode: boolean
  targetJournal: string
  field?: string | null
  /** The matching section of the working copy, so Calibrate can write the result back into the paper. */
  copy?: { documentId: string; sectionId: string } | null
}): ReactElement {
  const navigate = useNavigate()
  const width = Math.min(100, Math.max(0, section.score))
  const [showFix, setShowFix] = useState(false)
  const [showContent, setShowContent] = useState(false)
  const plan = section.fix_plan ?? []

  // The section text for the tools is prepared by the server (captions left out, length limited) and passed on as it is.
  const handleCalibrateSection = (): void => {
    if (section.tool_text) {
      navigate('/calibrate', {
        state: {
          text: section.tool_text,
          journal: targetJournal,
          ...(field ? { field } : {}),
          focus: {
            section: section.title || sectionLabel(name),
            issue: section.issue ?? section.weakness ?? '',
            fix: section.fix ?? '',
            // every issue of the section with how to fix it: the Calibrator resolves each one
            ...(plan.length > 0 ? { steps: plan } : {}),
          },
          notes: section.tool_notes ?? [],
          ...(copy ? { document: copy } : {}),
        },
      })
      return
    }
    const promptSample = `[${sectionLabel(name)} Section]\nIssue to address: ${section.issue ?? 'Improve scientific register'}\nTarget remediation: ${section.fix ?? 'Align with journal standard'}`
    navigate('/calibrate', {
      state: {
        text: promptSample,
        journal: targetJournal,
      },
    })
  }

  const handleHeatmapSection = (): void => {
    if (section.tool_text) {
      navigate('/heatmap', {
        state: {
          text: section.tool_text,
          journal: targetJournal,
          ...(field ? { field } : {}),
          notes: section.tool_notes ?? [],
        },
      })
      return
    }
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
        <p className="w-28 shrink-0 break-words text-sm font-medium">{section.title || sectionLabel(name)}</p>
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
          {section.issue && !section.summary ? (
            <p className="text-sm text-gray-500">
              {section.score < SUGGESTION_SCORE ? '⚠' : '•'} {section.issue}
            </p>
          ) : null}
          {section.fix || plan.length > 0 ? (
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
              {showFix && plan.length > 0 ? <FixPlan plan={plan} /> : null}
              {showFix && plan.length === 0 && section.fix ? (
                <p className="w-full mt-1 text-sm text-sky-700">→ {section.fix}</p>
              ) : null}
            </div>
          ) : null}
        </div>
      )}
      {section.summary || section.strengths?.length || section.findings?.length ? <SectionReviewBlock section={section} /> : null}
      {section.checks && section.checks.length > 0 ? <SectionChecklist checks={section.checks} /> : null}
      {section.content ? (
        <div className="mt-1.5 sm:pl-[7.5rem]">
          <button
            type="button"
            onClick={() => setShowContent((value) => !value)}
            aria-expanded={showContent}
            className="text-xs text-stone-500 underline hover:text-stone-800"
          >
            {showContent ? 'Hide content ▴' : 'Show content ▾'}
          </button>
          {showContent ? (
            <div className="mt-1.5 rounded-lg border border-gray-200 bg-gray-50 p-3">
              <p className="text-xs text-gray-500">
                {section.word_count != null ? `${section.word_count.toLocaleString()} words` : null}
                {section.start_pct != null && section.end_pct != null
                  ? ` · covers ${section.start_pct}%–${section.end_pct}% of the paper`
                  : null}
                {section.source === 'derived' ? ' · runs until the next section starts, so the end is approximate' : null}
              </p>
              <div className="mt-2 max-h-72 space-y-2 overflow-auto text-xs leading-relaxed text-gray-700">
                {section.content.split(/\n{2,}/).map((paragraph, index) => (
                  <p key={index}>{paragraph}</p>
                ))}
              </div>
              {section.content_truncated ? (
                <p className="mt-2 text-xs text-gray-400">Shortened: only the first part of this section is shown.</p>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}

function formatWhen(iso?: string | null): string {
  if (!iso) return ''
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? '' : ` on ${date.toLocaleString()}`
}

const ISSUE_STATUS: Record<string, { label: string; className: string }> = {
  resolved: { label: 'Resolved', className: 'bg-green-50 text-green-700' },
  partly_resolved: { label: 'Partly resolved', className: 'bg-amber-50 text-amber-800' },
  open: { label: 'Still open', className: 'bg-red-50 text-red-700' },
  not_assessed: { label: 'Not assessed', className: 'bg-gray-100 text-gray-600' },
}

function Delta({ before, after }: { before: number | null; after: number | null }): ReactElement {
  if (before == null || after == null) {
    return <span className="text-xs text-gray-400">{before == null ? 'new' : 'not in this version'}</span>
  }
  const change = after - before
  const tone = change > 0 ? 'text-green-700' : change < 0 ? 'text-red-700' : 'text-gray-500'
  return (
    <span className="text-xs tabular-nums text-gray-700">
      {before} → {after}{' '}
      <span className={tone}>({change > 0 ? '+' : ''}{change})</span>
    </span>
  )
}

function ChangeList({ title, items, labels }: { title: string; items: ScoreChange[]; labels?: Record<string, string> }): ReactElement | null {
  if (items.length === 0) return null
  return (
    <div className="mt-3">
      <p className="text-[10px] font-bold uppercase tracking-wide text-gray-500">{title}</p>
      <ul className="mt-1 space-y-1">
        {items.map((item) => (
          <li key={item.name} className="flex items-center justify-between gap-3 text-sm">
            <span className="text-gray-800">{labels?.[item.name] ?? sectionLabel(item.name)}</span>
            <Delta before={item.before} after={item.after} />
          </li>
        ))}
      </ul>
    </div>
  )
}

function RevisionPanel({ revision }: { revision: RevisionComparison }): ReactElement {
  const resolved = revision.issues.filter((issue) => issue.status === 'resolved').length
  return (
    <section className="mt-4 rounded-xl border border-sky-200 bg-sky-50/40 p-5">
      <p className="text-xs font-semibold uppercase tracking-wider text-sky-900">
        {revision.same_file ? 'Same file as before' : 'Revised version recognised'}
      </p>
      <p className="mt-1 text-sm text-gray-700">
        {revision.same_file
          ? `You analysed this same file${formatWhen(revision.previous_analysed_at)}. Differences below come from the analysis itself, not from your paper.`
          : `This looks like a new version of “${revision.previous_filename}”, analysed${formatWhen(revision.previous_analysed_at)} (about ${Math.round(revision.similarity * 100)}% of the text is unchanged).`}
      </p>
      {!revision.same_journal ? (
        <p className="mt-1 text-xs text-amber-800">
          The earlier analysis was for {revision.previous_journal}, so the scores were judged against a different journal.
        </p>
      ) : null}
      <div className="mt-3 flex items-center justify-between rounded-lg bg-white px-3 py-2">
        <span className="text-sm font-medium text-gray-900">Overall score</span>
        <Delta before={revision.score_before} after={revision.score_after} />
      </div>
      <ChangeList title="Criteria" items={revision.criteria} labels={CRITERION_LABEL} />
      <ChangeList title="Sections" items={revision.sections} />
      {revision.issues.length > 0 ? (
        <div className="mt-3">
          <p className="text-[10px] font-bold uppercase tracking-wide text-gray-500">
            Issues raised last time · {resolved} of {revision.issues.length} resolved
          </p>
          <ul className="mt-1 space-y-2">
            {revision.issues.map((issue, index) => {
              const status = ISSUE_STATUS[issue.status] ?? ISSUE_STATUS.not_assessed
              return (
                <li key={index} className="rounded-lg border border-gray-200 bg-white p-2.5">
                  <div className="flex items-start justify-between gap-2">
                    <p className="text-sm text-gray-800">{issue.problem}</p>
                    <Chip className={status.className}>{status.label}</Chip>
                  </div>
                  {issue.note ? <p className="mt-1 text-xs text-gray-500">{issue.note}</p> : null}
                </li>
              )
            })}
          </ul>
        </div>
      ) : null}
    </section>
  )
}

const SECTION_NAME: Record<string, string> = {
  abstract: 'Abstract',
  introduction: 'Introduction',
  methods: 'Methods',
  results: 'Results',
  discussion: 'Discussion',
}

const CELL_LINE_STATUS: Record<string, { label: string; className: string }> = {
  problematic: { label: 'problematic', className: 'bg-red-50 text-red-700' },
  no_known_problem: { label: 'no known problem', className: 'bg-green-50 text-green-700' },
  not_found: { label: 'no exact record', className: 'bg-gray-100 text-gray-600' },
  not_checked: { label: 'not checked', className: 'bg-gray-100 text-gray-600' },
}

/** What code found by comparing the paper's own values, and the cell lines looked up in Cellosaurus. */
function ConsistencyPanel({ report }: { report: ConsistencyReport }): ReactElement {
  if (report.status === 'not_checked') {
    return <p className="text-sm text-gray-500">{report.note || 'The consistency check could not be completed this time.'}</p>
  }
  return (
    <div className="space-y-3">
      <p className="text-xs text-gray-500">
        {report.facts_checked} values were read from your manuscript and compared across its sections by code. Every
        item below quotes the passages it rests on.
      </p>
      {report.findings.length === 0 ? (
        <p className="text-sm text-gray-700">No inconsistencies found between the values the paper gives.</p>
      ) : (
        <ul className="space-y-2">
          {report.findings.map((finding) => (
            <li
              key={finding.message}
              className={`rounded-lg border p-3 ${finding.severity === 'major' ? 'border-amber-200 bg-amber-50' : 'border-gray-200 bg-gray-50'}`}
            >
              <p className="text-sm text-gray-900">{finding.message}</p>
              <ul className="mt-2 space-y-1">
                {finding.quotes.map((quote) => (
                  <li key={`${quote.section}-${quote.quote}`} className="text-xs text-gray-600">
                    <span className="font-medium">{SECTION_NAME[quote.section] ?? (quote.section || 'Text')}:</span>{' '}
                    “{quote.quote}”
                  </li>
                ))}
              </ul>
            </li>
          ))}
        </ul>
      )}
      {report.cell_lines.length > 0 ? (
        <div>
          <p className="text-xs font-semibold uppercase tracking-wide text-gray-600">Cell lines (Cellosaurus)</p>
          <ul className="mt-1 space-y-1">
            {report.cell_lines.map((line) => {
              const status = CELL_LINE_STATUS[line.status] ?? CELL_LINE_STATUS.not_checked
              return (
                <li key={line.name} className="text-xs text-gray-700">
                  <span className="font-medium">{line.name}</span>{' '}
                  <Chip className={status.className}>{status.label}</Chip>
                  {line.accession ? (
                    <a
                      className="ml-1 underline"
                      href={`https://www.cellosaurus.org/${line.accession}`}
                      target="_blank"
                      rel="noreferrer"
                    >
                      {line.accession}
                    </a>
                  ) : null}
                  {line.note ? <span className="block text-red-700">{line.note}</span> : null}
                </li>
              )
            })}
          </ul>
        </div>
      ) : null}
    </div>
  )
}

/** The paper's own competing-interest and funding statements: context for the reader, never a fault. */
function DisclosuresPanel({ disclosures }: { disclosures: Disclosures }): ReactElement {
  return (
    <div className="space-y-2 text-sm text-gray-800">
      {disclosures.interests.length > 0 ? (
        <div className="flex flex-wrap gap-1">
          {disclosures.interests.map((kind) => (
            <Chip key={kind} className="bg-sky-50 text-sky-800">{kind}</Chip>
          ))}
        </div>
      ) : disclosures.declares_none ? (
        <p className="text-xs text-gray-500">The authors declare no competing interests.</p>
      ) : null}
      {disclosures.competing_interests ? (
        <p><span className="font-medium">Competing interests:</span> {disclosures.competing_interests}</p>
      ) : null}
      {disclosures.funding ? (
        <p><span className="font-medium">Funding:</span> {disclosures.funding}</p>
      ) : null}
      <p className="text-xs text-gray-500">
        Quoted from the paper. A disclosed interest is context for weighing the claims, not a sign of misconduct, and
        it does not change the score.
      </p>
    </div>
  )
}

const NOTICE_SOURCE: Record<string, string> = {
  publisher: 'publisher',
  'retraction-watch': 'Retraction Watch',
}

/** The publication record, shown above everything else: it decides what the score cannot. */
function IntegrityBanner({ integrity }: { integrity: PublicationStatus }): ReactElement | null {
  const where = [integrity.journal, integrity.year ? `(${integrity.year})` : ''].filter(Boolean).join(' ')
  const doiLink = integrity.doi ? (
    <a className="underline" href={`https://doi.org/${integrity.doi}`} target="_blank" rel="noreferrer">
      {integrity.doi}
    </a>
  ) : null
  const notices = (integrity.notices ?? []).filter((n) => n.type !== 'correction' || integrity.status === 'corrected')
  const noticeList =
    notices.length > 0 ? (
      <ul className="mt-2 space-y-1">
        {notices.map((notice) => (
          <li key={`${notice.type}-${notice.doi ?? ''}`} className="text-xs">
            {notice.label || notice.type}
            {notice.date ? ` · ${notice.date}` : ''}
            {notice.source
              ? ` · ${notice.source.split(', ').map((s) => NOTICE_SOURCE[s] ?? s).join(' and ')}`
              : ''}
            {notice.doi ? (
              <>
                {' · '}
                <a className="underline" href={`https://doi.org/${notice.doi}`} target="_blank" rel="noreferrer">
                  read the notice
                </a>
              </>
            ) : null}
          </li>
        ))}
      </ul>
    ) : null
  const marks = (integrity.file_marks ?? []).map((m) => `"${m.text}"`).join(', ')
  const notes = (integrity.notes ?? []).map((note) => (
    <p key={note} className="mt-1 text-xs opacity-80">{note}</p>
  ))

  if (integrity.status === 'retracted') {
    return (
      <section className="mt-4 rounded-xl border-2 border-red-300 bg-red-50 p-4 text-red-900">
        <p className="text-sm font-semibold">
          {integrity.is_notice ? 'This file is a retraction notice' : 'This article has been retracted'}
        </p>
        <p className="mt-1 text-sm">
          {where ? `Published in ${where}` : 'The publication record'}
          {doiLink ? <> · {doiLink}</> : null}
          {marks ? ` · the file is marked ${marks}` : ''}. Its findings should not be relied on or cited as valid,
          and no recommendation is made. The analysis below describes the text only.
        </p>
        {noticeList}
        {notes}
      </section>
    )
  }
  if (integrity.status === 'expression_of_concern') {
    return (
      <section className="mt-4 rounded-xl border-2 border-amber-300 bg-amber-50 p-4 text-amber-900">
        <p className="text-sm font-semibold">The publisher has issued an expression of concern</p>
        <p className="mt-1 text-sm">
          {where ? `Published in ${where}` : 'The publication record'}
          {doiLink ? <> · {doiLink}</> : null}. The score is capped until the concern is resolved.
        </p>
        {noticeList}
        {notes}
      </section>
    )
  }
  if (integrity.status === 'published' || integrity.status === 'corrected') {
    return (
      <section className="mt-4 rounded-xl border border-sky-200 bg-sky-50 p-4 text-sky-900">
        <p className="text-sm font-semibold">
          Already published{where ? ` in ${where}` : ''}
          {integrity.status === 'corrected' ? ', with a correction' : ''}
        </p>
        <p className="mt-1 text-sm">
          {doiLink ? <>DOI {doiLink}. </> : null}
          {integrity.at_target_journal
            ? 'It passed peer review in this journal, so nothing in it is treated as a desk rejection here.'
            : 'It passed peer review there. Your target journal is a different one, so scope and level are judged for it.'}
        </p>
        {integrity.status === 'corrected' ? noticeList : null}
        {notes}
      </section>
    )
  }
  if (integrity.status === 'not_checked' && (integrity.notes ?? []).length > 0) {
    return (
      <section className="mt-4 rounded-xl border border-gray-200 bg-gray-50 p-3 text-gray-700">
        <p className="text-xs">Publication status not checked: {(integrity.notes ?? []).join(' ')}</p>
      </section>
    )
  }
  return null
}

function ReasonCard({
  item,
  targetJournal,
  field,
  sectionText,
  copy,
}: {
  item: RejectionReason
  targetJournal: string
  field?: string | null
  /** The whole section's tool text, used when the server found no paragraphs around the evidence. */
  sectionText?: string | null
  /** The section of the working copy the evidence sits in, so the fix can be written back into the paper. */
  copy?: { documentId: string; sectionId: string } | null
}): ReactElement {
  const navigate = useNavigate()
  const style = SEVERITY_STYLE[item.severity]

  // The text of the manuscript the reason is about goes to the Calibrator, with the reason as its focus.
  const handleFixReason = (): void => {
    const text = item.tool_text || sectionText
    const focus = {
      section: item.section ? sectionLabel(item.section) : 'Passage under review',
      issue: item.problem || item.reason,
      fix: item.solution || item.fix,
    }
    if (text || copy) {
      navigate('/calibrate', {
        state: {
          text: text ?? '',
          journal: targetJournal,
          ...(field ? { field } : {}),
          focus,
          notes: item.tool_text ? item.tool_notes ?? [] : [],
          ...(copy ? { document: copy, ...(item.evidence ? { evidence: item.evidence } : {}) } : {}),
        },
      })
      return
    }
    // Neither the paragraphs nor the section could be found: start from the quoted passage itself.
    navigate('/calibrate', {
      state: {
        text: item.evidence ?? '',
        journal: targetJournal,
        ...(field ? { field } : {}),
        focus,
      },
    })
  }

  return (
    <article className={`rounded-lg border border-gray-200 border-l-4 p-3.5 ${style.border}`}>
      <div className="flex items-center justify-between">
        <p className={`text-[10px] font-bold uppercase tracking-wide ${style.label}`}>
          {style.name}
          {item.origin && item.origin !== 'model' ? (
            <span className="ml-2 rounded bg-white/70 px-1.5 py-0.5 text-[9px] font-semibold normal-case tracking-normal text-gray-600">
              {item.origin === 'publication_record' ? 'from the publication record' : 'found by comparing the paper’s own values'}
            </span>
          ) : null}
        </p>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() =>
              navigate('/rebuttal', {
                state: {
                  rejection_reason: `${item.problem || item.reason}${item.impact ? `\nImpact: ${item.impact}` : ''}\nSuggested remediation: ${item.solution || item.fix}`,
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
      <p className="mt-1.5 text-[10px] font-bold uppercase tracking-wide text-gray-500">Problem</p>
      <p className="text-sm font-medium text-gray-800">{item.problem || item.reason}</p>
      {item.evidence ? (
        <>
          <p className="mt-2 text-[10px] font-bold uppercase tracking-wide text-gray-500">
            Evidence{item.location ? ` · ${item.location}` : ''}
          </p>
          <blockquote className="border-l-2 border-gray-300 pl-2.5 text-xs italic text-gray-600">
            “{item.evidence}”
          </blockquote>
        </>
      ) : null}
      {item.impact ? (
        <>
          <p className="mt-2 text-[10px] font-bold uppercase tracking-wide text-gray-500">
            Impact{item.impact_area ? ` · ${IMPACT_AREA_LABEL[item.impact_area] ?? item.impact_area}` : ''}
          </p>
          <p className="text-sm text-gray-700">{item.impact}</p>
        </>
      ) : null}
      <p className="mt-2 text-[10px] font-bold uppercase tracking-wide text-gray-500">Solution</p>
      <p className="text-sm text-sky-700">→ {item.solution || item.fix}</p>
    </article>
  )
}
