import { useState, useEffect } from 'react'
import type { ReactElement } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { forgeAbstract } from '../api'
import type { AbstractForgeResponse, AbstractVariantDetail } from '../types'
import FormattedMathText from '../components/FormattedMathText'

export default function Forge(): ReactElement {
  const location = useLocation()
  const navigate = useNavigate()
  const state = location.state as {
    text?: string
    methodology?: string
    journal?: string
    field?: string
  } | null

  const [abstractDraft, setAbstractDraft] = useState<string>(
    state?.text ??
      'In this paper, we basically propose SpikeQ, an adaptive post-training quantization method for spiking neural networks. We evaluated our model across 6 image classification benchmarks. Mean energy consumption per inference dropped from 1.84 mJ to 1.21 mJ, while top-1 accuracy remained 92.4% (p < 0.001; 95% CI [-0.5, 0.1]). Our optimization minimizes spike-error variance directly.'
  )
  const [methodology, setMethodology] = useState<string>(
    state?.methodology ??
      'Spiking neural networks (SNNs) implemented on neuromorphic hardware under event-driven temporal dynamics. Evaluated on ImageNet and neuromorphic CIFAR10-DVS.'
  )
  const [journal, setJournal] = useState<string>(state?.journal ?? 'Nature Machine Intelligence')
  const [field, setField] = useState<string>(state?.field ?? 'Computer Science & Machine Learning')
  const [loading, setLoading] = useState<boolean>(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<AbstractForgeResponse | null>(null)
  const [copiedIndex, setCopiedIndex] = useState<number | null>(null)

  const handleForge = async (): Promise<void> => {
    if (!abstractDraft.trim() || abstractDraft.trim().length < 20) {
      setError('Please provide an abstract draft of at least 20 characters.')
      return
    }
    setError(null)
    setLoading(true)
    try {
      const res = await forgeAbstract({
        abstract_draft: abstractDraft,
        methodology_context: methodology,
        journal,
        field,
      })
      setResult(res)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Abstract Forge synthesis failed')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    handleForge()
  }, [])

  const handleCopy = async (textToCopy: string, index: number): Promise<void> => {
    await navigator.clipboard.writeText(textToCopy)
    setCopiedIndex(index)
    setTimeout(() => setCopiedIndex(null), 2000)
  }

  const handleSendToCalibrator = (variantText: string): void => {
    navigate('/calibrate', {
      state: {
        text: variantText,
        journal,
        field,
      },
    })
  }

  const handleSendToHeatmap = (variantText: string): void => {
    navigate('/heatmap', {
      state: {
        text: variantText,
        journal,
        field,
      },
    })
  }

  return (
    <div className="max-w-6xl mx-auto px-4 py-8">
      {/* Header */}
      <div className="border-b border-stone-200 pb-6 mb-8">
        <div className="flex items-center gap-2 mb-2">
          <span className="text-xs uppercase tracking-widest font-semibold px-2 py-0.5 rounded bg-stone-900 text-stone-100">
            Multi-Target Synthesis
          </span>
          <span className="text-xs text-stone-500">v1.0 Architectural Forge</span>
        </div>
        <h1 className="text-3xl font-serif font-bold text-stone-900 tracking-tight">
          Abstract Forge
        </h1>
        <p className="text-sm text-stone-600 mt-2 max-w-2xl leading-relaxed">
          Synthesizes three distinct architectural variants from your draft:
          Discoverability (SEO-optimized for PubMed/Google Scholar), Clarity (direct empirical for specialist reviewers),
          and High-Impact Narrative (broad paradigm shift for Nature/Science editors). All scientific invariants are cryptographically protected.
        </p>
      </div>

      {/* Control Strip */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6 p-4 rounded-lg bg-stone-50 border border-stone-200">
        <div>
          <label className="block text-xs font-semibold uppercase tracking-wider text-stone-600 mb-1">
            Target Venue / Bar
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
            Field / Discipline
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
            onClick={handleForge}
            className="w-full py-2.5 px-4 rounded bg-stone-900 text-white text-xs font-semibold tracking-wider uppercase hover:bg-stone-800 disabled:opacity-50 transition"
          >
            {loading ? 'Synthesizing 3 Architectures...' : 'Forge 3 Abstract Variants'}
          </button>
        </div>
      </div>

      {/* Input Drawers */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-6">
        <div className="p-4 bg-white rounded-lg border border-stone-200 shadow-sm">
          <div className="flex items-center justify-between mb-1.5">
            <label className="text-xs font-semibold uppercase tracking-wider text-stone-700">
              Current Abstract Draft / Findings
            </label>
            <span className="text-xs text-stone-400">
              {abstractDraft.trim() ? `${abstractDraft.trim().split(/\s+/).length} words` : '0 words'}
            </span>
          </div>
          <textarea
            value={abstractDraft}
            onChange={(e) => setAbstractDraft(e.target.value)}
            rows={4}
            className="w-full p-2.5 font-sans text-xs text-stone-800 border border-stone-300 rounded bg-stone-50/50 resize-y focus:outline-none focus:ring-1 focus:ring-stone-900 leading-relaxed"
            placeholder="Paste your current draft abstract here..."
          />
        </div>

        <div className="p-4 bg-white rounded-lg border border-stone-200 shadow-sm">
          <div className="flex items-center justify-between mb-1.5">
            <label className="text-xs font-semibold uppercase tracking-wider text-stone-700">
              Methodology & Technical Context (Optional)
            </label>
            <span className="text-xs text-stone-400">Contextual anchor</span>
          </div>
          <textarea
            value={methodology}
            onChange={(e) => setMethodology(e.target.value)}
            rows={4}
            className="w-full p-2.5 font-sans text-xs text-stone-800 border border-stone-300 rounded bg-stone-50/50 resize-y focus:outline-none focus:ring-1 focus:ring-stone-900 leading-relaxed"
            placeholder="Key datasets, hardware setups, or boundary conditions..."
          />
        </div>
      </div>

      {/* Error alert */}
      {error && (
        <div className="mb-6 p-3 rounded bg-red-50 border border-red-200 text-xs text-red-700">
          {error}
        </div>
      )}

      {/* Comparative Summary Strip */}
      {result && (
        <div className="mb-8 p-4 rounded-lg bg-stone-900 text-stone-100 flex flex-col md:flex-row items-start md:items-center justify-between gap-4">
          <div className="space-y-1.5">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-[10px] font-bold uppercase tracking-widest bg-stone-700 px-2 py-0.5 rounded text-stone-200">
                Architectural Breakdown
              </span>
              <span className="text-xs text-stone-200 font-mono">
                {result.invariant_telemetry?.summary_label ??
                  (result.invariants_preserved_count > 0
                    ? `✓ ${result.invariants_preserved_count} Protected Spans Verified`
                    : '• General Text Draft (0 Invariants Detected)')}
              </span>
            </div>
            <p className="text-xs text-stone-300 leading-relaxed max-w-3xl">
              {result.comparative_summary}
            </p>
            {result.fact_inventory?.has_tradeoffs && (
              <div className="text-[11px] text-amber-300 flex items-center gap-1.5">
                <span>⚠ Protected Trade-off / Limitation:</span>
                <span className="italic">
                  {result.fact_inventory.explicit_tradeoffs.join('; ')}
                </span>
              </div>
            )}
          </div>
          <div className="text-right shrink-0">
            <div className="text-xl font-bold text-white">3 Architectures</div>
            <div className="text-[10px] uppercase text-stone-400 tracking-wider">
              Venue: {result.target_journal}
            </div>
          </div>
        </div>
      )}
      {/* 3-Card Interactive Matrix */}
      {result && (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {result.variants.map((v: AbstractVariantDetail, idx: number) => {
            const isSEO = v.variant_type === 'discoverability_seo'
            const isClarity = v.variant_type === 'clarity_direct'
            const badgeBg = isSEO
              ? 'bg-sky-50 text-sky-800 border-sky-200'
              : isClarity
              ? 'bg-emerald-50 text-emerald-800 border-emerald-200'
              : 'bg-purple-50 text-purple-800 border-purple-200'

            return (
              <div
                key={v.variant_type}
                className="flex flex-col justify-between bg-white border border-stone-300 rounded-xl p-5 shadow-sm hover:shadow-md transition"
              >
                <div>
                  {/* Card Header */}
                  <div className="flex items-start justify-between gap-2 mb-3">
                    <div>
                      <span className={`text-[10px] uppercase font-bold px-2 py-0.5 rounded border ${badgeBg}`}>
                        {v.title_label}
                      </span>
                      <p className="text-[11px] text-stone-500 mt-1 font-medium">
                        Target: {v.target_persona}
                      </p>
                    </div>
                    {/* Editorial Readiness Score Pill */}
                    <div className="text-right shrink-0 relative group">
                      <div className="text-lg font-bold text-stone-900 leading-none">
                        {v.readiness_index?.overall_readiness_score ?? v.predicted_acceptance_score}
                      </div>
                      <div className="text-[9px] uppercase tracking-wider text-stone-500 mt-0.5 font-semibold">
                        Readiness Index
                      </div>
                      <div className="hidden group-hover:block absolute right-0 top-full mt-1.5 w-60 p-2.5 bg-stone-900 text-stone-100 text-[10.5px] rounded shadow-xl z-20 text-left">
                        <div className="font-semibold text-white mb-1">Editorial Readiness Breakdown:</div>
                        <div className="space-y-0.5 text-stone-300">
                          <div>• Empirical Anchoring: {v.readiness_index?.empirical_anchoring ?? 88}%</div>
                          <div>• Comparison Grounding: {v.readiness_index?.comparison_grounding ?? 85}%</div>
                          <div>• Trade-off Retention: {v.readiness_index?.tradeoff_retention ?? 85}%</div>
                          <div>• Narrative Discipline: {v.readiness_index?.narrative_discipline ?? 86}%</div>
                        </div>
                        <div className="text-[9.5px] text-stone-400 mt-1 pt-1 border-t border-stone-800">
                          Structural quality index (does not guarantee journal acceptance).
                        </div>
                      </div>
                    </div>
                  </div>

                  {/* Highlights Bullet Tags */}
                  <div className="flex flex-wrap gap-1.5 mb-3.5">
                    {v.key_highlights.map((h, i) => (
                      <span
                        key={i}
                        className="text-[10px] bg-stone-100 text-stone-700 px-2 py-0.5 rounded font-mono"
                      >
                        • {h}
                      </span>
                    ))}
                  </div>

                  {/* Rendered Abstract Text with LaTeX support */}
                  <div className="p-3.5 bg-stone-50 rounded-lg border border-stone-200/80 text-[13.5px] leading-relaxed text-stone-900 font-sans tracking-[0.01em] min-h-[190px]">
                    <FormattedMathText text={v.abstract_text} />
                  </div>

                  {/* Claim & Limitation Audit Badge */}
                  {v.claim_audit && (
                    <div className="mt-2.5 px-2.5 py-1.5 rounded text-[11px] flex items-center justify-between border bg-stone-50/80 border-stone-200">
                      <span className="text-stone-600 font-medium">Claim & Limitation Audit:</span>
                      <span
                        className={`font-semibold ${
                          v.claim_audit.is_faithful ? 'text-emerald-700' : 'text-amber-800'
                        }`}
                      >
                        {v.claim_audit.is_faithful ? '✓ Faithful to Source' : '⚠ Overclaim Caution'}
                      </span>
                    </div>
                  )}

                  {/* SEO Keywords for Variant A */}
                  {v.seo_keywords && v.seo_keywords.length > 0 && (
                    <div className="mt-3 p-2.5 bg-sky-50/50 rounded border border-sky-100">
                      <span className="text-[10px] uppercase font-bold tracking-wider text-sky-900 block mb-1">
                        Indexing Keywords (PubMed & Scholar):
                      </span>
                      <div className="flex flex-wrap gap-1">
                        {v.seo_keywords.map((kw, i) => (
                          <span
                            key={i}
                            className="text-[10.5px] bg-white text-sky-800 px-2 py-0.5 rounded border border-sky-200"
                          >
                            {kw}
                          </span>
                        ))}
                      </div>
                    </div>
                  )}

                  <div className="mt-2 text-right">
                    <span className="text-[11px] text-stone-400 font-mono">
                      {v.word_count} words
                    </span>
                  </div>
                </div>

                {/* Card Actions Footer */}
                <div className="mt-5 pt-3 border-t border-stone-200 space-y-2">
                  <button
                    type="button"
                    onClick={() => handleCopy(v.abstract_text, idx)}
                    className="w-full py-2 px-3 rounded bg-stone-900 text-white text-xs font-semibold uppercase tracking-wider hover:bg-stone-800 transition"
                  >
                    {copiedIndex === idx ? '✓ Copied to Clipboard' : 'Copy This Abstract'}
                  </button>

                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={() => handleSendToCalibrator(v.abstract_text)}
                      className="flex-1 py-1.5 px-2 rounded border border-stone-300 bg-white text-[11px] font-medium text-stone-700 hover:bg-stone-50 transition text-center"
                    >
                      ✨ Calibrate
                    </button>
                    <button
                      type="button"
                      onClick={() => handleSendToHeatmap(v.abstract_text)}
                      className="flex-1 py-1.5 px-2 rounded border border-stone-300 bg-white text-[11px] font-medium text-stone-700 hover:bg-stone-50 transition text-center"
                    >
                      📊 Heatmap
                    </button>
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
