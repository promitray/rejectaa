import { useState, useEffect } from 'react'
import type { ReactElement } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { analyzeHeatmap } from '../api'
import { ToolWait } from '../components/Loaders'
import type { HeatmapAnalyzeResponse, ParagraphHeatDetail, SentenceHeatDetail } from '../types'

type MetricKey = 'stylistic_signal' | 'flagged_sentences' | 'avg_length' | 'cadence_burstiness'

interface MetricGuide {
  key: MetricKey
  title: string
  badge: string
  whatItIs: string
  howItWorks: string
  benchmarks: { range: string; label: string; badgeClass: string }[]
}

const METRIC_GUIDES: Record<MetricKey, MetricGuide> = {
  stylistic_signal: {
    key: 'stylistic_signal',
    title: 'Stylistic Signal Index',
    badge: 'Formulaic Phrasing Density',
    whatItIs:
      'Percentage of template-like or formulaic phrasing detected in your text. Lower scores indicate natural, specific academic writing.',
    howItWorks:
      'Word-weighted average of sentence scores scanning for stock phrases, repeated openers, and template patterns.',
    benchmarks: [
      { range: '0% – 25%', label: 'Clean / Natural', badgeClass: 'bg-emerald-50 text-emerald-800 border-emerald-200' },
      { range: '26% – 50%', label: 'Moderate Signals', badgeClass: 'bg-amber-50 text-amber-800 border-amber-200' },
      { range: '51% – 100%', label: 'Elevated Signals', badgeClass: 'bg-rose-50 text-rose-800 border-rose-200' },
    ],
  },
  flagged_sentences: {
    key: 'flagged_sentences',
    title: 'Sentences Flagged as Formulaic',
    badge: 'Threshold Exceeded',
    whatItIs:
      'Count of sentences that crossed the formulaic threshold out of total sentences in the text.',
    howItWorks:
      'Sentences are flagged when their individual stylometric score exceeds 0.58.',
    benchmarks: [
      { range: '0 Flagged', label: 'Clean — no sentences flagged', badgeClass: 'bg-emerald-50 text-emerald-800 border-emerald-200' },
      { range: '1+ Flagged', label: 'Candidate sentences for rephrasing', badgeClass: 'bg-amber-50 text-amber-800 border-amber-200' },
    ],
  },
  avg_length: {
    key: 'avg_length',
    title: 'Average Sentence Length',
    badge: 'Readability',
    whatItIs:
      'Average number of words per sentence, indicating sentence pacing and complexity.',
    howItWorks:
      'Calculated as total word count divided by total sentences.',
    benchmarks: [
      { range: '18 – 28 words', label: 'Balanced academic standard', badgeClass: 'bg-emerald-50 text-emerald-800 border-emerald-200' },
      { range: '> 35 words', label: 'Dense / May cause reader fatigue', badgeClass: 'bg-amber-50 text-amber-800 border-amber-200' },
      { range: '< 14 words', label: 'Short / May feel choppy', badgeClass: 'bg-stone-100 text-stone-800 border-stone-200' },
    ],
  },
  cadence_burstiness: {
    key: 'cadence_burstiness',
    title: 'Cadence Burstiness (CV)',
    badge: 'Rhythm & Variation',
    whatItIs:
      'Variation in sentence lengths. Human writing naturally alternates between short punchy sentences and longer explanations.',
    howItWorks:
      'Coefficient of Variation (CV = standard deviation / mean sentence length).',
    benchmarks: [
      { range: '≥ 0.35', label: 'Dynamic Human Cadence', badgeClass: 'bg-emerald-50 text-emerald-800 border-emerald-200' },
      { range: '0.22 – 0.34', label: 'Moderate Variation', badgeClass: 'bg-amber-50 text-amber-800 border-amber-200' },
      { range: '< 0.22', label: 'Monotonous Rhythm', badgeClass: 'bg-rose-50 text-rose-800 border-rose-200' },
    ],
  },
}

export default function Heatmap(): ReactElement {
  const location = useLocation()
  const navigate = useNavigate()
  const state = location.state as {
    text?: string
    journal?: string
    field?: string
    /** What the server left out of or cut from the section text. */
    notes?: string[]
  } | null

  const [text, setText] = useState<string>(state?.text ?? '')
  const [journal, setJournal] = useState<string>(state?.journal ?? '')
  const [field, setField] = useState<string>(state?.field ?? '')
  const [loading, setLoading] = useState<boolean>(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<HeatmapAnalyzeResponse | null>(null)
  const [selectedSentence, setSelectedSentence] = useState<SentenceHeatDetail | null>(null)
  const [selectedParagraph, setSelectedParagraph] = useState<ParagraphHeatDetail | null>(null)
  const [activeMetricHelp, setActiveMetricHelp] = useState<MetricKey | null>(null)

  useEffect(() => {
    if (!activeMetricHelp) return
    const onKeyDown = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') setActiveMetricHelp(null)
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [activeMetricHelp])

  const handleAnalyze = async (): Promise<void> => {
    if (!text.trim() || text.trim().length < 20) {
      setError('Please provide at least 20 characters for stylometric analysis.')
      return
    }
    setError(null)
    setLoading(true)
    try {
      const res = await analyzeHeatmap({
        text,
        ...(journal.trim() ? { journal: journal.trim() } : {}),
        ...(field.trim() ? { field: field.trim() } : {}),
      })
      setResult(res)
      if (res.paragraphs.length > 0 && res.paragraphs[0].sentences.length > 0) {
        setSelectedParagraph(res.paragraphs[0])
        setSelectedSentence(res.paragraphs[0].sentences[0])
      }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Heatmap analysis failed')
    } finally {
      setLoading(false)
    }
  }


  const handleSendToCalibrator = (paragraphText: string): void => {
    navigate('/calibrate', {
      state: {
        text: paragraphText,
        journal: journal,
        field: field,
      },
    })
  }

  return (
    <div className="max-w-6xl mx-auto px-4 py-8">
      {/* Header */}
      <div className="border-b border-stone-200 pb-6 mb-8">
        <div className="flex items-center gap-2 mb-2">
          <span className="text-xs uppercase tracking-widest font-semibold px-2 py-0.5 rounded bg-stone-900 text-stone-100">
            Stylometric Cadence Diagnostic
          </span>
          <span className="text-xs text-stone-500">v2.0 Explainable Heatmap</span>
        </div>
        <h1 className="text-3xl font-serif font-bold text-stone-900 tracking-tight">
          AI Signal Heatmap
        </h1>
        <p className="text-sm text-stone-600 mt-2 max-w-2xl leading-relaxed">
          Finds formulaic phrasing (stock phrases, inflated verbs, hype words, template sentences, empty conclusions),
          repeated openings and transitions, and monotonous rhythm. It describes style only: it does not and cannot tell who or what wrote the text.
        </p>
      </div>

      {/* Control Strip */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6 p-4 rounded-lg bg-stone-50 border border-stone-200">
        <div>
          <label className="block text-xs font-semibold uppercase tracking-wider text-stone-600 mb-1">
            Target Venue / Standard
          </label>
          <input
            type="text"
            value={journal}
            onChange={(e) => setJournal(e.target.value)}
            placeholder="e.g. Polymer Chemistry"
            className="w-full text-sm px-3 py-2 border border-stone-300 rounded bg-white focus:outline-none focus:ring-1 focus:ring-stone-900"
          />
        </div>

        <div>
          <label className="block text-xs font-semibold uppercase tracking-wider text-stone-600 mb-1">
            Field / Domain
          </label>
          <input
            type="text"
            value={field}
            onChange={(e) => setField(e.target.value)}
            placeholder="e.g. Polymer chemistry"
            className="w-full text-sm px-3 py-2 border border-stone-300 rounded bg-white focus:outline-none focus:ring-1 focus:ring-stone-900"
          />
        </div>

        <div className="flex items-end">
          <button
            type="button"
            disabled={loading}
            onClick={handleAnalyze}
            className="w-full py-2.5 px-4 rounded bg-stone-900 text-white text-xs font-semibold tracking-wider uppercase hover:bg-stone-800 disabled:opacity-50 transition"
          >
            {loading ? 'Analyzing Stylometrics...' : 'Run Heatmap Analysis'}
          </button>
        </div>
      </div>

      {state?.notes?.length ? (
        <div className="mb-3 rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-900">
          {state.notes.map((note) => (
            <p key={note}>{note}</p>
          ))}
        </div>
      ) : null}

      {/* Draft Input Box */}
      <div className="mb-6 p-4 rounded-lg bg-white border border-stone-200">
        <div className="flex items-center justify-between mb-2">
          <label className="text-xs font-semibold uppercase tracking-wider text-stone-700">
            Draft Manuscript Excerpt for Heatmap Analysis
          </label>
          <span className="text-xs text-stone-400">
            {text.trim() ? `${text.trim().split(/\s+/).length} words` : '0 words'}
          </span>
        </div>
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Paste manuscript draft text here..."
          className="w-full h-32 p-3 font-sans text-sm text-stone-800 border border-stone-300 rounded bg-stone-50/50 resize-y focus:outline-none focus:ring-1 focus:ring-stone-900 leading-relaxed placeholder:text-stone-400"
        />
      </div>
      {/* Error alert */}
      {error && (
        <div className="mb-6 p-3 rounded bg-red-50 border border-red-200 text-xs text-red-700">
          {error}
        </div>
      )}

      {/* Summary Score Strip */}
      {result && (
        <div className="mb-8">
          {/* Sub-header with 'How these metrics work' guide button */}
          <div className="flex items-center justify-between mb-3">
            <span className="text-xs font-semibold uppercase tracking-wider text-stone-700">
              Diagnostic Metrics Summary
            </span>
            <button
              type="button"
              onClick={() => setActiveMetricHelp('stylistic_signal')}
              className="text-xs text-stone-600 hover:text-stone-900 font-medium flex items-center gap-1.5 transition"
            >
              <span className="inline-flex items-center justify-center w-4 h-4 rounded-full border border-stone-300 bg-stone-100 text-[10px] font-bold text-stone-700">
                i
              </span>
              <span>About these metrics & how they work</span>
            </button>
          </div>

          {/* Small Sample Size Warning Banner if < 15 sentences */}
          {result.total_sentences < 15 && (
            <div className="mb-4 flex items-start gap-2.5 rounded-lg border border-amber-200 bg-amber-50/90 p-3 text-xs text-amber-900 shadow-xs">
              <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-amber-200 text-amber-900 font-bold text-[11px]">
                !
              </span>
              <div>
                <span className="font-semibold text-amber-950">
                  Small sample notice ({result.total_sentences} {result.total_sentences === 1 ? 'sentence' : 'sentences'}):
                </span>{' '}
                Stylometric cadence and burstiness (CV) fluctuate on short passages. For reliable analysis, 15+ sentences are recommended.
              </div>
            </div>
          )}

          {/* 4 Metric Cards */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            {/* 1. Stylistic Signal Index */}
            <div
              onClick={() => setActiveMetricHelp('stylistic_signal')}
              className="relative group p-4 bg-white rounded-lg border border-stone-200 hover:border-stone-400 text-center cursor-pointer transition shadow-xs hover:shadow-sm"
            >
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation()
                  setActiveMetricHelp('stylistic_signal')
                }}
                className="absolute top-2.5 right-2.5 flex h-5 w-5 items-center justify-center rounded-full border border-stone-200 bg-stone-50 text-[10px] font-bold text-stone-500 hover:bg-stone-900 hover:text-white hover:border-stone-900 transition shadow-xs"
                title="Click to learn how Stylistic Signal Index works"
                aria-label="Stylistic Signal Index Info"
              >
                i
              </button>
              <div
                className={`text-2xl font-bold ${
                  result.overall_level === 'elevated_signals'
                    ? 'text-rose-700'
                    : result.overall_level === 'mixed_signals'
                    ? 'text-amber-700'
                    : 'text-emerald-700'
                }`}
              >
                {Math.round(result.overall_cadence_score * 100)}%
              </div>
              <div className="text-[11px] text-stone-500 uppercase tracking-wider mt-1">
                Stylistic Signal Index
              </div>
              <div className="text-[10px] text-stone-400 group-hover:text-stone-700 mt-1 flex items-center justify-center gap-0.5 transition font-medium">
                <span>How it works</span> <span>→</span>
              </div>
            </div>

            {/* 2. Sentences flagged as formulaic */}
            <div
              onClick={() => setActiveMetricHelp('flagged_sentences')}
              className="relative group p-4 bg-white rounded-lg border border-stone-200 hover:border-stone-400 text-center cursor-pointer transition shadow-xs hover:shadow-sm"
            >
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation()
                  setActiveMetricHelp('flagged_sentences')
                }}
                className="absolute top-2.5 right-2.5 flex h-5 w-5 items-center justify-center rounded-full border border-stone-200 bg-stone-50 text-[10px] font-bold text-stone-500 hover:bg-stone-900 hover:text-white hover:border-stone-900 transition shadow-xs"
                title="Click to learn how Sentences Flagged works"
                aria-label="Sentences Flagged Info"
              >
                i
              </button>
              <div className="text-2xl font-bold text-stone-900">
                {result.flagged_sentences_count} / {result.total_sentences}
              </div>
              <div className="text-[11px] text-stone-500 uppercase tracking-wider mt-1">
                Sentences flagged as formulaic
              </div>
              <div className="text-[10px] text-stone-400 group-hover:text-stone-700 mt-1 flex items-center justify-center gap-0.5 transition font-medium">
                <span>How it works</span> <span>→</span>
              </div>
            </div>

            {/* 3. Avg Sentence Length */}
            <div
              onClick={() => setActiveMetricHelp('avg_length')}
              className="relative group p-4 bg-white rounded-lg border border-stone-200 hover:border-stone-400 text-center cursor-pointer transition shadow-xs hover:shadow-sm"
            >
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation()
                  setActiveMetricHelp('avg_length')
                }}
                className="absolute top-2.5 right-2.5 flex h-5 w-5 items-center justify-center rounded-full border border-stone-200 bg-stone-50 text-[10px] font-bold text-stone-500 hover:bg-stone-900 hover:text-white hover:border-stone-900 transition shadow-xs"
                title="Click to learn how Avg Sentence Length works"
                aria-label="Avg Sentence Length Info"
              >
                i
              </button>
              <div className="text-2xl font-bold text-stone-900">
                {result.readability_metrics.average_sentence_length} words
              </div>
              <div className="text-[11px] text-stone-500 uppercase tracking-wider mt-1">
                Avg Sentence Length
              </div>
              <div className="text-[10px] text-stone-400 group-hover:text-stone-700 mt-1 flex items-center justify-center gap-0.5 transition font-medium">
                <span>How it works</span> <span>→</span>
              </div>
            </div>

            {/* 4. Cadence Burstiness (CV) */}
            <div
              onClick={() => setActiveMetricHelp('cadence_burstiness')}
              className="relative group p-4 bg-white rounded-lg border border-stone-200 hover:border-stone-400 text-center cursor-pointer transition shadow-xs hover:shadow-sm"
            >
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation()
                  setActiveMetricHelp('cadence_burstiness')
                }}
                className="absolute top-2.5 right-2.5 flex h-5 w-5 items-center justify-center rounded-full border border-stone-200 bg-stone-50 text-[10px] font-bold text-stone-500 hover:bg-stone-900 hover:text-white hover:border-stone-900 transition shadow-xs"
                title="Click to learn how Cadence Burstiness works"
                aria-label="Cadence Burstiness Info"
              >
                i
              </button>
              <div className="text-2xl font-bold text-stone-900">
                {result.readability_metrics.sentence_burstiness_index}
              </div>
              <div className="text-[11px] text-stone-500 uppercase tracking-wider mt-1">
                Cadence Burstiness (CV)
              </div>
              {result.readability_metrics.burstiness_qualitative_tag && (
                <div className="text-[10px] text-stone-600 font-medium mt-1">
                  {result.readability_metrics.burstiness_qualitative_tag}
                </div>
              )}
              <div className="text-[10px] text-stone-400 group-hover:text-stone-700 mt-1 flex items-center justify-center gap-0.5 transition font-medium">
                <span>How it works</span> <span>→</span>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Metric Explanation Modal Dialog */}
      {result && activeMetricHelp && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4 backdrop-blur-xs transition-opacity"
          onClick={() => setActiveMetricHelp(null)}
          role="dialog"
          aria-modal="true"
        >
          <div
            className="relative w-full max-w-xl max-h-[90vh] flex flex-col bg-white rounded-xl shadow-2xl border border-stone-200 overflow-hidden"
            onClick={(e) => e.stopPropagation()}
          >
            {/* Modal Header */}
            <div className="flex items-start justify-between border-b border-stone-200 px-6 py-4 bg-stone-50/70">
              <div>
                <div className="flex items-center gap-2">
                  <span className="text-[10px] font-semibold uppercase tracking-wider px-2 py-0.5 rounded bg-stone-200 text-stone-800">
                    {METRIC_GUIDES[activeMetricHelp].badge}
                  </span>
                  <span className="text-xs text-stone-400">• Metric Guide</span>
                </div>
                <h2 className="text-lg font-bold text-stone-900 mt-1 font-serif">
                  {METRIC_GUIDES[activeMetricHelp].title}
                </h2>
              </div>
              <button
                type="button"
                onClick={() => setActiveMetricHelp(null)}
                className="rounded-lg p-1.5 text-stone-400 hover:bg-stone-200/60 hover:text-stone-700 transition text-base leading-none"
                aria-label="Close dialog"
              >
                ✕
              </button>
            </div>

            {/* Quick Switcher Tabs */}
            <div className="flex border-b border-stone-200 bg-stone-100/70 p-1.5 gap-1 overflow-x-auto text-xs">
              {(
                [
                  ['stylistic_signal', 'Stylistic Index'],
                  ['flagged_sentences', 'Flagged Sentences'],
                  ['avg_length', 'Avg Length'],
                  ['cadence_burstiness', 'Burstiness (CV)'],
                ] as const
              ).map(([key, label]) => {
                const isActive = activeMetricHelp === key
                return (
                  <button
                    key={key}
                    type="button"
                    onClick={() => setActiveMetricHelp(key)}
                    className={`px-3 py-1.5 rounded text-xs font-medium whitespace-nowrap transition ${
                      isActive
                        ? 'bg-white text-stone-900 shadow-xs font-semibold'
                        : 'text-stone-600 hover:text-stone-900 hover:bg-white/50'
                    }`}
                  >
                    {label}
                  </button>
                )
              })}
            </div>

            {/* Modal Body */}
            <div className="p-6 overflow-y-auto space-y-5 text-stone-800 text-sm">
              {/* Current Document Value Strip */}
              <div className="flex items-center justify-between p-3 rounded-lg bg-stone-50 border border-stone-200">
                <span className="text-xs font-semibold uppercase tracking-wider text-stone-500">
                  Current Document Value
                </span>
                <span className="text-base font-bold text-stone-900">
                  {activeMetricHelp === 'stylistic_signal' && (
                    <span
                      className={
                        result.overall_level === 'elevated_signals'
                          ? 'text-rose-700'
                          : result.overall_level === 'mixed_signals'
                          ? 'text-amber-700'
                          : 'text-emerald-700'
                      }
                    >
                      {Math.round(result.overall_cadence_score * 100)}% (
                      {result.overall_level === 'elevated_signals'
                        ? 'Elevated Signals'
                        : result.overall_level === 'mixed_signals'
                        ? 'Mixed Signals'
                        : 'Clean / Low Signals'}
                      )
                    </span>
                  )}
                  {activeMetricHelp === 'flagged_sentences' && (
                    <span>
                      {result.flagged_sentences_count} / {result.total_sentences} sentences
                    </span>
                  )}
                  {activeMetricHelp === 'avg_length' && (
                    <span>{result.readability_metrics.average_sentence_length} words / sentence</span>
                  )}
                  {activeMetricHelp === 'cadence_burstiness' && (
                    <span>
                      {result.readability_metrics.sentence_burstiness_index}{' '}
                      <span className="text-xs font-medium text-stone-500">
                        ({result.readability_metrics.burstiness_qualitative_tag || 'Standard'})
                      </span>
                    </span>
                  )}
                </span>
              </div>

              {/* What It Measures */}
              <div>
                <h3 className="text-xs font-bold uppercase tracking-wider text-stone-600 mb-1">
                  What It Measures
                </h3>
                <p className="text-stone-700 leading-relaxed text-xs">
                  {METRIC_GUIDES[activeMetricHelp].whatItIs}
                </p>
              </div>

              {/* How It Works */}
              <div>
                <h3 className="text-xs font-bold uppercase tracking-wider text-stone-600 mb-1">
                  How It Works
                </h3>
                <p className="text-stone-700 leading-relaxed text-xs">
                  {METRIC_GUIDES[activeMetricHelp].howItWorks}
                </p>
              </div>

              {/* Benchmarks */}
              <div>
                <h3 className="text-xs font-bold uppercase tracking-wider text-stone-600 mb-2">
                  Benchmarks
                </h3>
                <div className="space-y-1.5">
                  {METRIC_GUIDES[activeMetricHelp].benchmarks.map((b, i) => (
                    <div
                      key={i}
                      className="flex items-center gap-2.5 p-2 rounded border border-stone-200/80 bg-white text-xs"
                    >
                      <span className={`px-2 py-0.5 rounded font-semibold border shrink-0 ${b.badgeClass}`}>
                        {b.range}
                      </span>
                      <span className="text-stone-600">{b.label}</span>
                    </div>
                  ))}
                </div>
              </div>

              {/* Disclaimer */}
              <div className="pt-2 border-t border-stone-100 text-[11px] text-stone-400 space-y-1">
                <p>Describes writing style and pacing only; does not determine authorship.</p>
                {result.total_sentences < 15 && (
                  <p className="text-amber-800 font-medium">
                    Note: Analysis is based on a short sample ({result.total_sentences} sentences). Scores are indicative.
                  </p>
                )}
              </div>
            </div>

            {/* Modal Footer */}
            <div className="flex justify-end border-t border-stone-200 px-6 py-3 bg-stone-50">
              <button
                type="button"
                onClick={() => setActiveMetricHelp(null)}
                className="py-1.5 px-4 rounded bg-stone-900 text-white text-xs font-semibold uppercase tracking-wider hover:bg-stone-800 transition"
              >
                Got It / Close
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Main Split Layout: Document Heatmap on Left, Inspector Drawer on Right */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Left 2 Cols: Document Heatmap Canvas */}
        <div className="lg:col-span-2 flex flex-col">
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs font-semibold uppercase tracking-wider text-stone-700">
              Interactive Stylometric Overlay
            </span>
            <div className="flex items-center gap-3 text-xs text-stone-500">
              <span className="flex items-center gap-1">
                <span className="w-2.5 h-2.5 rounded bg-emerald-100 border border-emerald-300" /> Clean
              </span>
              <span className="flex items-center gap-1">
                <span className="w-2.5 h-2.5 rounded bg-amber-100 border border-amber-300" /> Moderate
              </span>
              <span className="flex items-center gap-1">
                <span className="w-2.5 h-2.5 rounded bg-rose-100 border border-rose-300" /> Elevated
              </span>
            </div>
          </div>

          <div className="p-6 bg-white border border-stone-300 rounded-lg min-h-[420px] shadow-sm space-y-6">
            <ToolWait
              loading={loading}
              hasResult={!!result}
              title="Running heatmap"
              className="space-y-6"
              note=""
              steps={['Reading your sentences', 'Measuring rhythm and openers', 'Scoring each paragraph']}
            >
            {result ? (
              result.paragraphs.map((para) => {
                const paraBg =
                  para.level === 'elevated'
                    ? 'bg-rose-50/50 border-rose-200'
                    : para.level === 'moderate'
                    ? 'bg-amber-50/40 border-amber-200'
                    : 'bg-stone-50/30 border-stone-200/70'

                return (
                  <div
                    key={para.paragraph_index}
                    className={`relative group p-3.5 rounded-lg border transition ${paraBg}`}
                  >
                  {/* Top Bar for Paragraph */}
                  <div className="flex items-center justify-between mb-2 pb-1 border-b border-stone-200/60">
                    <div className="flex items-center gap-2">
                      <span className="text-[11px] font-semibold uppercase tracking-wider text-stone-600">
                        Paragraph {para.paragraph_index + 1}
                      </span>
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-stone-200/70 text-stone-700 font-mono">
                        CV: {para.paragraph_burstiness_cv}
                      </span>
                      {(para.paragraph_signals?.length ? para.paragraph_signals : para.primary_issue ? [para.primary_issue] : []).map(
                        (signal) => (
                          <span key={signal} className="text-[10px] text-amber-800 font-medium">
                            • {signal}
                          </span>
                        ),
                      )}
                    </div>
                    <button
                      type="button"
                      onClick={() => handleSendToCalibrator(para.text)}
                      className="text-[11px] font-semibold text-stone-900 hover:text-stone-700 underline flex items-center gap-1 transition"
                    >
                      ✨ Calibrate Paragraph in Voice Calibrator →
                    </button>
                  </div>

                  {/* Sentences with Highlights */}
                  <div className="text-[14.5px] leading-relaxed text-stone-900 font-sans tracking-[0.01em]">
                    {para.sentences.map((sent) => {
                      const isSelected = selectedSentence?.sentence_index === sent.sentence_index
                      const bgClass =
                        sent.level === 'elevated'
                          ? 'bg-rose-100/70 border-b-2 border-rose-400 text-stone-950 font-medium'
                          : sent.level === 'moderate'
                          ? 'bg-amber-100/60 border-b border-amber-300 text-stone-900'
                          : 'hover:bg-stone-100 text-stone-800'

                      return (
                        <span
                          key={sent.sentence_index}
                          onClick={() => {
                            setSelectedSentence(sent)
                            setSelectedParagraph(para)
                          }}
                          className={`cursor-pointer px-1 py-0.5 rounded transition ${bgClass} ${
                            isSelected ? 'ring-2 ring-stone-900' : ''
                          }`}
                          title={`Score: ${Math.round(sent.signal_score * 100)}% - Click to inspect`}
                        >
                          {sent.text}{' '}
                        </span>
                      )
                    })}
                  </div>
                </div>
              )
            })
            ) : (
              <div className="h-full flex items-center justify-center text-stone-400 text-xs italic">
                Enter your draft above and run Heatmap Analysis.
              </div>
            )}
            </ToolWait>
          </div>
        </div>

        {/* Right Col: Inspector Drawer */}
        <div className="flex flex-col">
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs font-semibold uppercase tracking-wider text-stone-700">
              Stylometric Inspector
            </span>
            {selectedSentence && (
              <span className="text-xs text-stone-500">
                Sentence {selectedSentence.sentence_index + 1}
              </span>
            )}
          </div>

          <div className="p-5 bg-stone-50 border border-stone-300 rounded-lg shadow-sm min-h-[420px]">
            {selectedSentence ? (
              <div className="space-y-5">
                <div>
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-xs font-semibold uppercase tracking-wider text-stone-600">
                      Signal Intensity
                    </span>
                    <span
                      className={`text-[11px] font-bold px-2 py-0.5 rounded uppercase ${
                        selectedSentence.level === 'elevated'
                          ? 'bg-rose-100 text-rose-800 border border-rose-300'
                          : selectedSentence.level === 'moderate'
                          ? 'bg-amber-100 text-amber-800 border border-amber-300'
                          : 'bg-emerald-100 text-emerald-800 border border-emerald-300'
                      }`}
                    >
                      {selectedSentence.level === 'elevated'
                        ? 'Formulaic'
                        : selectedSentence.level === 'moderate'
                        ? 'Moderate Pattern'
                        : 'Clean Cadence'}
                    </span>
                  </div>
                  <div className="text-2xl font-bold text-stone-900">
                    {Math.round(selectedSentence.signal_score * 100)}%
                  </div>
                </div>

                <div className="p-3 bg-white rounded border border-stone-200">
                  <span className="text-[11px] font-semibold uppercase tracking-wider text-stone-500 block mb-1">
                    Selected Sentence Text:
                  </span>
                  <p className="text-xs font-mono text-stone-800 leading-relaxed">
                    "{selectedSentence.text}"
                  </p>
                </div>

                {/* 4 Feature Dimensions Breakdown */}
                <div>
                  <span className="text-[11px] font-semibold uppercase tracking-wider text-stone-700 block mb-2">
                    Four-Dimension Stylometric Breakdown:
                  </span>
                  <div className="space-y-2 text-xs">
                    <div>
                      <div className="flex justify-between text-stone-600 mb-0.5">
                        <span>Sentence-Length Uniformity:</span>
                        <span className="font-semibold text-stone-900">
                          {Math.round(selectedSentence.feature_scores.sentence_length_uniformity * 100)}%
                        </span>
                      </div>
                      <div className="w-full bg-stone-200 h-1.5 rounded-full overflow-hidden">
                        <div
                          className="bg-stone-900 h-full"
                          style={{
                            width: `${selectedSentence.feature_scores.sentence_length_uniformity * 100}%`,
                          }}
                        />
                      </div>
                    </div>

                    <div>
                      <div className="flex justify-between text-stone-600 mb-0.5">
                        <span>Transition Pattern Repetition:</span>
                        <span className="font-semibold text-stone-900">
                          {Math.round(selectedSentence.feature_scores.transition_repetition * 100)}%
                        </span>
                      </div>
                      <div className="w-full bg-stone-200 h-1.5 rounded-full overflow-hidden">
                        <div
                          className="bg-stone-900 h-full"
                          style={{
                            width: `${selectedSentence.feature_scores.transition_repetition * 100}%`,
                          }}
                        />
                      </div>
                    </div>

                    <div>
                      <div className="flex justify-between text-stone-600 mb-0.5">
                        <span>Passive Construction Clustering:</span>
                        <span className="font-semibold text-stone-900">
                          {Math.round(selectedSentence.feature_scores.passive_construction_ratio * 100)}%
                        </span>
                      </div>
                      <div className="w-full bg-stone-200 h-1.5 rounded-full overflow-hidden">
                        <div
                          className="bg-stone-900 h-full"
                          style={{
                            width: `${selectedSentence.feature_scores.passive_construction_ratio * 100}%`,
                          }}
                        />
                      </div>
                    </div>

                    <div>
                      <div className="flex justify-between text-stone-600 mb-0.5">
                        <span>Formulaic Phrasing:</span>
                        <span className="font-semibold text-stone-900">
                          {Math.round(selectedSentence.feature_scores.lexical_repetition * 100)}%
                        </span>
                      </div>
                      <div className="w-full bg-stone-200 h-1.5 rounded-full overflow-hidden">
                        <div
                          className="bg-stone-900 h-full"
                          style={{
                            width: `${selectedSentence.feature_scores.lexical_repetition * 100}%`,
                          }}
                        />
                      </div>
                    </div>
                  </div>
                </div>

                {/* Diagnostics List */}
                <div className="p-3 bg-white rounded border border-stone-200 space-y-1.5">
                  <span className="text-[11px] font-semibold uppercase tracking-wider text-stone-700 block">
                    Explainable Diagnostics:
                  </span>
                  <ul className="text-xs text-stone-600 space-y-1 list-disc list-inside">
                    {selectedSentence.diagnostics.map((d, i) => (
                      <li key={i}>{d}</li>
                    ))}
                  </ul>
                  {selectedSentence.suggested_remedy && (
                    <div className="mt-2 pt-2 border-t border-stone-100 text-xs text-sky-800 font-medium">
                      💡 Remedy: {selectedSentence.suggested_remedy}
                    </div>
                  )}
                </div>

                {/* Direct Action Handoff */}
                {selectedParagraph && (
                  <button
                    type="button"
                    onClick={() => handleSendToCalibrator(selectedParagraph.text)}
                    className="w-full py-2.5 px-3 rounded bg-stone-900 text-white text-xs font-semibold tracking-wider uppercase hover:bg-stone-800 transition"
                  >
                    ✨ Calibrate Selected Paragraph →
                  </button>
                )}
              </div>
            ) : (
              <div className="h-full flex items-center justify-center text-stone-400 text-xs italic">
                Click any sentence on the left to inspect its stylometric metrics.
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
