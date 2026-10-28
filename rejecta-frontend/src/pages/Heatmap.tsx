import { useState, useEffect } from 'react'
import type { ReactElement } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { analyzeHeatmap } from '../api'
import type { HeatmapAnalyzeResponse, ParagraphHeatDetail, SentenceHeatDetail } from '../types'

export default function Heatmap(): ReactElement {
  const location = useLocation()
  const navigate = useNavigate()
  const state = location.state as { text?: string; journal?: string; field?: string } | null

  const [text, setText] = useState<string>(
    state?.text ??
      'In this paper, we delve into deep neural architectures for clinical prognosis. It is paramount and a testament to recent progress that our proposed transformer demonstrates predictive utility across acute patients.\n\nFurthermore, our attention regularization minimizes gradient variance such that results improve significantly. Consequently, the findings were shown to be robust, and it is important to remember that baselines were surpassed without degradation.'
  )
  const [journal, setJournal] = useState<string>(state?.journal ?? 'Nature Machine Intelligence')
  const [field, setField] = useState<string>(state?.field ?? 'Computer Science & Machine Learning')
  const [loading, setLoading] = useState<boolean>(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<HeatmapAnalyzeResponse | null>(null)
  const [selectedSentence, setSelectedSentence] = useState<SentenceHeatDetail | null>(null)
  const [selectedParagraph, setSelectedParagraph] = useState<ParagraphHeatDetail | null>(null)

  const handleAnalyze = async (): Promise<void> => {
    if (!text.trim() || text.trim().length < 20) {
      setError('Please provide at least 20 characters for stylometric analysis.')
      return
    }
    setError(null)
    setLoading(true)
    try {
      const res = await analyzeHeatmap({ text, journal, field })
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

  useEffect(() => {
    // Run initial analysis automatically on mount
    handleAnalyze()
  }, [])

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
          <span className="text-xs text-stone-500">v1.0 Explainable Heatmap</span>
        </div>
        <h1 className="text-3xl font-serif font-bold text-stone-900 tracking-tight">
          AI Signal Heatmap
        </h1>
        <p className="text-sm text-stone-600 mt-2 max-w-2xl leading-relaxed">
          Analyzes paragraph rhythm, transition repetition, sentence-length uniformity, and passive construction density.
          Highlights stylistic patterns to help authors diversify cadence—this is an editorial diagnostic tool, not an accusation of misconduct.
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
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-8">
          <div className="p-4 bg-white rounded-lg border border-stone-200 text-center">
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
          </div>

          <div className="p-4 bg-white rounded-lg border border-stone-200 text-center">
            <div className="text-2xl font-bold text-stone-900">
              {result.flagged_sentences_count} / {result.total_sentences}
            </div>
            <div className="text-[11px] text-stone-500 uppercase tracking-wider mt-1">
              Sentences with Elevated Signals
            </div>
          </div>

          <div className="p-4 bg-white rounded-lg border border-stone-200 text-center">
            <div className="text-2xl font-bold text-stone-900">
              {result.readability_metrics.average_sentence_length} words
            </div>
            <div className="text-[11px] text-stone-500 uppercase tracking-wider mt-1">
              Avg Sentence Length
            </div>
          </div>

          <div className="p-4 bg-white rounded-lg border border-stone-200 text-center relative group">
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
            <div className="hidden group-hover:block absolute bottom-full left-1/2 -translate-x-1/2 mb-2 w-64 p-2 bg-stone-900 text-stone-100 text-[11px] rounded shadow-lg z-10 text-left">
              Measures sentence length variation (CV). Higher values (&gt;0.35) indicate natural alternation between complex explanations and punchy empirical claims.
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
                      {para.primary_issue && (
                        <span className="text-[10px] text-amber-800 font-medium">
                          • {para.primary_issue}
                        </span>
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
                        ? 'Elevated Repetition'
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
                        <span>Lexical Clichés & Repetition:</span>
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
