import { useState, useEffect } from 'react'
import type { ReactElement } from 'react'
import { useLocation } from 'react-router-dom'
import ReactMarkdown from 'react-markdown'
import { calibrateVoice } from '../api'
import type { VoiceCalibrateResponse } from '../types'
import FormattedMathText from '../components/FormattedMathText'

export default function Calibrate(): ReactElement {
  const location = useLocation()
  const state = location.state as { text?: string; journal?: string; field?: string } | null

  const [text, setText] = useState<string>(state?.text ?? '')
  const [journal, setJournal] = useState<string>(state?.journal ?? 'Nature Machine Intelligence')
  const [field, setField] = useState<string>(state?.field ?? 'Computer Science & Machine Learning')
  const [intensity, setIntensity] = useState<'polish' | 'rewrite' | 'restructure'>('rewrite')
  const [loading, setLoading] = useState<boolean>(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<VoiceCalibrateResponse | null>(null)
  const [copied, setCopied] = useState<boolean>(false)

  useEffect(() => {
    if (state?.text) {
      setText(state.text)
    }
    if (state?.journal) {
      setJournal(state.journal)
    }
    if (state?.field) {
      setField(state.field)
    }
  }, [state])

  const handleCalibrate = async (): Promise<void> => {
    if (!text.trim() || text.trim().length < 20) {
      setError('Please provide an excerpt of at least 20 characters.')
      return
    }
    setError(null)
    setLoading(true)
    try {
      const res = await calibrateVoice({
        text,
        journal,
        field,
        intensity,
      })
      setResult(res)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Calibration failed')
    } finally {
      setLoading(false)
    }
  }

  const handleCopy = async (): Promise<void> => {
    if (!result?.calibrated_text) return
    await navigator.clipboard.writeText(result.calibrated_text)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  return (
    <div className="max-w-6xl mx-auto px-4 py-8">
      {/* Header */}
      <div className="border-b border-stone-200 pb-6 mb-8">
        <div className="flex items-center gap-2 mb-2">
          <span className="text-xs uppercase tracking-widest font-semibold px-2 py-0.5 rounded bg-stone-900 text-stone-100">
            Advanced Writing Companion
          </span>
          <span className="text-xs text-stone-500">v1.0 Zero-Drift Engine</span>
        </div>
        <h1 className="text-3xl font-serif font-bold text-stone-900 tracking-tight">
          Voice Calibrator
        </h1>
        <p className="text-sm text-stone-600 mt-2 max-w-2xl">
          Transform synthetic or uncalibrated drafts into the authentic scholarly register of your target journal.
          All formulas, citations, and statistical metrics are protected in the scientific invariant vault.
        </p>
      </div>

      {/* Control Strip */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6 p-4 rounded-lg bg-stone-50 border border-stone-200">
        <div>
          <label className="block text-xs font-semibold uppercase tracking-wider text-stone-600 mb-1">
            Target Journal
          </label>
          <input
            type="text"
            value={journal}
            onChange={(e) => setJournal(e.target.value)}
            className="w-full text-sm px-3 py-2 border border-stone-300 rounded bg-white focus:outline-none focus:ring-1 focus:ring-stone-900"
            placeholder="e.g. Nature Machine Intelligence"
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
            placeholder="e.g. Computational Biology"
          />
        </div>

        <div>
          <label className="block text-xs font-semibold uppercase tracking-wider text-stone-600 mb-1">
            Calibration Intensity
          </label>
          <div className="flex rounded border border-stone-300 overflow-hidden bg-white">
            {(['polish', 'rewrite', 'restructure'] as const).map((mode) => (
              <button
                key={mode}
                type="button"
                onClick={() => setIntensity(mode)}
                className={`flex-1 text-xs py-2 capitalize font-medium transition-colors ${
                  intensity === mode
                    ? 'bg-stone-900 text-white'
                    : 'text-stone-600 hover:bg-stone-100'
                }`}
              >
                {mode}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* Error alert */}
      {error && (
        <div className="mb-6 p-3 rounded bg-red-50 border border-red-200 text-xs text-red-700">
          {error}
        </div>
      )}

      {/* Split Workbench */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Left: Input Text */}
        <div className="flex flex-col">
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs font-semibold uppercase tracking-wider text-stone-700">
              Draft Manuscript Excerpt
            </span>
            <span className="text-xs text-stone-400">
              {text.trim() ? `${text.trim().split(/\s+/).length} words` : '0 words'}
            </span>
          </div>
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Paste your abstract, introduction, or discussion draft here... E.g. We delve into modern neural networks. Our model achieved an AUROC = 0.942 with p < 0.001 across n = 1,420 patients (Vaswani et al., 2017)."
            className="w-full h-80 p-4 font-sans text-sm text-stone-800 border border-stone-300 rounded-lg bg-white resize-y focus:outline-none focus:ring-2 focus:ring-stone-900 leading-relaxed shadow-sm placeholder:text-stone-400 placeholder:font-mono placeholder:text-xs"
          />
          <div className="mt-4">
            <button
              type="button"
              disabled={loading}
              onClick={handleCalibrate}
              className="w-full py-3 px-4 rounded bg-stone-900 text-white text-xs font-semibold tracking-wider uppercase hover:bg-stone-800 disabled:opacity-50 transition"
            >
              {loading ? 'Calibrating with Scientific Vault...' : `Calibrate to ${journal}`}
            </button>
          </div>
        </div>

        {/* Right: Calibrated Output */}
        <div className="flex flex-col">
          <div className="flex items-center justify-between mb-2">
            <div className="flex items-center gap-2">
              <span className="text-xs font-semibold uppercase tracking-wider text-stone-700">
                Calibrated Scholarly Output
              </span>
              {result && (
                <span
                  className={`text-[10px] uppercase font-bold px-2 py-0.5 rounded border ${
                    result.status === 'verified'
                      ? 'bg-emerald-50 text-emerald-800 border-emerald-300'
                      : result.status === 'blocked'
                      ? 'bg-rose-50 text-rose-800 border-rose-300'
                      : result.status === 'guidance'
                      ? 'bg-sky-50 text-sky-800 border-sky-300'
                      : 'bg-stone-100 text-stone-700 border-stone-300'
                  }`}
                >
                  {result.status === 'verified' && `✓ ${result.total_restored_invariants}/${result.total_detected_invariants} Invariants Verified`}
                  {result.status === 'blocked' && '⛔ Output Blocked (Validation Failed)'}
                  {result.status === 'guidance' && '📋 Revision Guidance Mode'}
                  {result.status === 'no_invariants' && '• No Protected Invariants Detected'}
                </span>
              )}
            </div>
            {result && result.status !== 'blocked' && (
              <span className="text-xs text-stone-400">
                {result.word_count_calibrated} words
              </span>
            )}
          </div>

          <div className="relative w-full h-80 p-4 font-sans text-sm border border-stone-300 rounded-lg bg-white overflow-y-auto leading-relaxed text-stone-800 shadow-sm">
            {result ? (
              result.status === 'blocked' ? (
                <div className="h-full flex flex-col justify-center items-center text-center p-4 bg-rose-50/50 rounded border border-rose-200">
                  <span className="text-2xl mb-2">⛔</span>
                  <h4 className="text-sm font-semibold text-rose-900 mb-1">
                    Output Quarantined & Blocked
                  </h4>
                  <p className="text-xs text-rose-700 max-w-md mb-3">
                    {result.block_reasons?.length
                      ? result.block_reasons[0]
                      : 'Protected scientific invariants failed cryptographic verification. Raw output suppressed.'}
                  </p>
                  <div className="text-[11px] font-mono bg-white px-2.5 py-1.5 rounded border border-rose-200 text-rose-800">
                    Unverified: {result.total_detected_invariants - result.total_restored_invariants} tokens missing or corrupted
                  </div>
                </div>
              ) : result.status === 'guidance' ? (
                <div className="prose prose-sm max-w-none text-stone-900 leading-relaxed text-[14px]">
                  <ReactMarkdown>{result.calibrated_text ?? ''}</ReactMarkdown>
                </div>
              ) : (
                <div className="leading-relaxed text-[14.5px] text-stone-900 tracking-[0.01em]">
                  <FormattedMathText text={result.calibrated_text ?? ''} />
                </div>
              )
            ) : (
              <div className="h-full flex items-center justify-center text-stone-400 text-xs italic">
                Calibrated output will appear here with preserved mathematical bounds and journal register.
              </div>
            )}
          </div>

          <div className="mt-4 flex gap-3">
            <button
              type="button"
              disabled={!result || result.status === 'blocked' || !result.calibrated_text}
              onClick={handleCopy}
              className={`flex-1 py-3 px-4 rounded border text-xs font-semibold tracking-wider uppercase transition ${
                !result || result.status === 'blocked' || !result.calibrated_text
                  ? 'bg-stone-100 border-stone-200 text-stone-400 cursor-not-allowed opacity-60'
                  : 'bg-white border-stone-300 text-stone-800 hover:bg-stone-50 cursor-pointer'
              }`}
            >
              {result?.status === 'blocked'
                ? '🔒 Copy Blocked (Validation Required)'
                : copied
                ? '✓ Copied to Clipboard'
                : result?.status === 'guidance'
                ? 'Copy Revision Guidance'
                : 'Copy Calibrated Text'}
            </button>
          </div>
        </div>
      </div>

      {/* Audit Telemetry Strip */}
      {result && (
        <div className="mt-8 p-5 rounded-lg border border-stone-200 bg-white">
          <h3 className="text-xs font-semibold uppercase tracking-wider text-stone-900 mb-3">
            Editorial Verification & Invariant Audit Report
          </h3>
          {result.status === 'guidance' ? (
            /* Dedicated Reviewer Guidance Telemetry Grid */
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4 text-center">
              <div className="p-3 bg-stone-50 rounded border border-stone-100">
                <div className="text-sm font-bold text-sky-800 uppercase tracking-wider">
                  Reviewer Feedback
                </div>
                <div className="text-[11px] text-stone-500 uppercase tracking-wider mt-1">
                  Detected Input Type
                </div>
              </div>
              <div className="p-3 bg-stone-50 rounded border border-stone-100">
                <div className="text-sm font-bold text-sky-800 uppercase tracking-wider">
                  Revision Guidance
                </div>
                <div className="text-[11px] text-stone-500 uppercase tracking-wider mt-1">
                  Operating Mode
                </div>
              </div>
              <div className="p-3 bg-stone-50 rounded border border-stone-100">
                <div className="text-sm font-bold text-emerald-800 uppercase tracking-wider">
                  None (Zero Fabrication)
                </div>
                <div className="text-[11px] text-stone-500 uppercase tracking-wider mt-1">
                  Empirical Results Added
                </div>
              </div>
              <div className="p-3 bg-stone-50 rounded border border-stone-100">
                <div className="text-sm font-bold text-stone-900 uppercase tracking-wider">
                  {result.guidance_items?.length ?? 4} Directives
                </div>
                <div className="text-[11px] text-stone-500 uppercase tracking-wider mt-1">
                  Evidence Action Items
                </div>
              </div>
            </div>
          ) : (
            /* Standard Manuscript Calibration Telemetry Grid */
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4 text-center">
              <div className="p-3 bg-stone-50 rounded border border-stone-100">
                <div className="text-xl font-bold text-stone-900">
                  {result.synthetic_markers_removed > 0
                    ? result.synthetic_markers_removed
                    : 'Clean Draft'}
                </div>
                <div className="text-[11px] text-stone-500 uppercase tracking-wider">
                  {result.synthetic_markers_removed > 0
                    ? 'Clichés Eradicated'
                    : 'No Clichés Detected'}
                </div>
              </div>
              <div className="p-3 bg-stone-50 rounded border border-stone-100">
                <div
                  className={`text-xl font-bold ${
                    result.status === 'blocked'
                      ? 'text-rose-700'
                      : result.status === 'no_invariants'
                      ? 'text-stone-600'
                      : 'text-emerald-700'
                  }`}
                >
                  {result.status === 'blocked'
                    ? `Failed: ${result.total_detected_invariants - result.total_restored_invariants}/${result.total_detected_invariants}`
                    : result.status === 'no_invariants'
                    ? '0 Detected'
                    : `${result.total_restored_invariants} / ${result.total_detected_invariants}`}
                </div>
                <div className="text-[11px] text-stone-500 uppercase tracking-wider">
                  {result.status === 'no_invariants'
                    ? 'Protected Invariants'
                    : 'Invariants Verified'}
                </div>
              </div>
              <div className="p-3 bg-stone-50 rounded border border-stone-100">
                <div className="text-xl font-bold text-stone-900">
                  {result.word_count_original} → {result.word_count_calibrated}
                </div>
                <div className="text-[11px] text-stone-500 uppercase tracking-wider">
                  Word Count Delta
                </div>
              </div>
              <div className="p-3 bg-stone-50 rounded border border-stone-100">
                <div className="text-xl font-bold text-stone-900 capitalize">
                  {result.intensity_applied}
                </div>
                <div className="text-[11px] text-stone-500 uppercase tracking-wider">
                  Calibration Mode
                </div>
              </div>
            </div>
          )}

          {result.audit_notes.length > 0 && (
            <div className="text-xs space-y-2 mt-4 pt-3 border-t border-stone-200">
              <div className="font-semibold text-stone-900 uppercase tracking-wider text-[11px]">
                Scientific Consistency & Editorial Advisory:
              </div>
              <div className="space-y-1.5">
                {result.audit_notes.map((note, idx) => {
                  const isWarning = note.startsWith('Statistical') || note.startsWith('Bibliographic') || note.startsWith('Semantic') || note.startsWith('Security')
                  const isImprovement = note.startsWith('Editorial')
                  return (
                    <div
                      key={idx}
                      className={`p-2.5 rounded text-xs flex items-start gap-2 ${
                        isWarning
                          ? 'bg-amber-50 border border-amber-200 text-amber-900'
                          : isImprovement
                          ? 'bg-emerald-50 border border-emerald-200 text-emerald-900'
                          : 'bg-stone-50 border border-stone-200 text-stone-700'
                      }`}
                    >
                      <span className="shrink-0">{isWarning ? '⚠' : isImprovement ? '✓' : '•'}</span>
                      <span>{note}</span>
                    </div>
                  )
                })}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
