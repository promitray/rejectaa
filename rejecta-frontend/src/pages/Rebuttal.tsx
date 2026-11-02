import { useState, useEffect } from 'react'
import type { ReactElement } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { analyzeRebuttal } from '../api'
import type {
  RebuttalAnalyzeResponse,
  RebuttalCommentItem,
  ReviewerBlock,
} from '../types'

export default function Rebuttal(): ReactElement {
  const location = useLocation()
  const navigate = useNavigate()
  const state = location.state as {
    reviewer_text?: string
    journal?: string
    rejection_reason?: string
  } | null

  const [reviewerText, setReviewerText] = useState<string>(
    state?.reviewer_text ?? (state?.rejection_reason ? `Editor / Reviewer Critique:\n${state.rejection_reason}` : '')
  )
  const [manuscriptContext, setManuscriptContext] = useState<string>('')
  const [journal, setJournal] = useState<string>(state?.journal ?? 'Nature Machine Intelligence')
  const [authorTone, setAuthorTone] = useState<'collaborative' | 'diplomatic_firm' | 'formal_rigorous'>('collaborative')
  const [loading, setLoading] = useState<boolean>(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<RebuttalAnalyzeResponse | null>(null)
  const [localResponses, setLocalResponses] = useState<Record<string, string>>({})
  const [copiedId, setCopiedId] = useState<string | null>(null)
  const [checkedItems, setCheckedItems] = useState<Record<number, boolean>>({})
  const [exportCopied, setExportCopied] = useState<boolean>(false)

  useEffect(() => {
    if (state?.reviewer_text) {
      setReviewerText(state.reviewer_text)
    } else if (state?.rejection_reason) {
      setReviewerText(`Editor / Reviewer Critique:\n${state.rejection_reason}`)
    }
    if (state?.journal) {
      setJournal(state.journal)
    }
  }, [state])

  const handleAnalyze = async (): Promise<void> => {
    if (!reviewerText.trim() || reviewerText.trim().length < 20) {
      setError('Please provide reviewer comments of at least 20 characters.')
      return
    }
    setError(null)
    setLoading(true)
    try {
      const res = await analyzeRebuttal({
        reviewer_report_text: reviewerText,
        manuscript_context: manuscriptContext.trim() || undefined,
        journal: journal.trim() || undefined,
        author_tone: authorTone,
      })
      setResult(res)
      // Initialise editable local responses
      const initialMap: Record<string, string> = {}
      res.comments.forEach((c) => {
        initialMap[c.comment_id] = c.drafted_response
      })
      setLocalResponses(initialMap)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Rebuttal analysis failed')
    } finally {
      setLoading(false)
    }
  }

  const handleCopyComment = async (commentId: string): Promise<void> => {
    const textToCopy = localResponses[commentId] ?? ''
    await navigator.clipboard.writeText(textToCopy)
    setCopiedId(commentId)
    setTimeout(() => setCopiedId(null), 2000)
  }

  const handleCopyFullExport = async (): Promise<void> => {
    if (!result?.export_document_md) return
    await navigator.clipboard.writeText(result.export_document_md)
    setExportCopied(true)
    setTimeout(() => setExportCopied(false), 2000)
  }

  const handleSendToCalibrator = (commentText: string): void => {
    navigate('/calibrate', {
      state: {
        text: commentText,
        journal: journal,
        field: 'Computer Science & Machine Learning',
      },
    })
  }

  const getCategoryBadge = (category: RebuttalCommentItem['category']) => {
    switch (category) {
      case 'agree_and_amend':
        return {
          bg: 'bg-emerald-50 text-emerald-800 border-emerald-300',
          label: 'Agree & Amend',
          icon: '🟢',
        }
      case 'clarify_misconception':
        return {
          bg: 'bg-sky-50 text-sky-800 border-sky-300',
          label: 'Clarify Misconception',
          icon: '🔵',
        }
      case 'partial_agree_bounded':
        return {
          bg: 'bg-amber-50 text-amber-800 border-amber-300',
          label: 'Bounded Counter-Proposal',
          icon: '🟠',
        }
      case 'contest_with_evidence':
        return {
          bg: 'bg-purple-50 text-purple-800 border-purple-300',
          label: 'Contest with Evidence',
          icon: '🟣',
        }
    }
  }

  return (
    <div className="max-w-7xl mx-auto px-4 py-8">
      {/* Header */}
      <div className="border-b border-stone-200 pb-6 mb-8">
        <div className="flex items-center gap-2 mb-2">
          <span className="text-xs uppercase tracking-widest font-semibold px-2 py-0.5 rounded bg-stone-900 text-stone-100">
            Peer-Review Survival Engine
          </span>
          <span className="text-xs text-stone-500">v1.0 Diplomatic Decomposer</span>
        </div>
        <h1 className="text-3xl font-serif font-bold text-stone-900 tracking-tight">
          Rebuttal Writer
        </h1>
        <p className="text-sm text-stone-600 mt-2 max-w-3xl">
          Decompose harsh or complex reviewer reports into discrete actionable critiques.
          Generate publication-ready, evidence-backed point-by-point rebuttal letters with
          calibrated diplomacy and zero emotional defensiveness.
        </p>
      </div>

      {/* Main Two-Panel Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-8 items-start">
        {/* Left Panel: Inputs (Sticky) */}
        <div className="lg:col-span-5 space-y-5 lg:sticky lg:top-8 bg-white border border-stone-200 p-6 rounded-xl shadow-sm">
          <div>
            <div className="flex items-center justify-between mb-1.5">
              <label className="text-xs font-semibold uppercase tracking-wider text-stone-700">
                Reviewer Comments / Editor Report
              </label>
              <span className="text-[11px] text-stone-400">Multi-reviewer text</span>
            </div>
            <textarea
              value={reviewerText}
              onChange={(e) => setReviewerText(e.target.value)}
              placeholder="Paste raw reviewer feedback here (e.g. 'Reviewer 1: 1. The baseline comparison is missing... 2. Clarify sample size. Reviewer 2: 1. This claim is incorrect...')"
              className="w-full min-h-[220px] p-3 text-xs font-mono text-stone-900 border border-stone-300 rounded-lg focus:outline-none focus:ring-1 focus:ring-stone-900 leading-relaxed resize-y"
            />
          </div>

          <div>
            <label className="block text-xs font-semibold uppercase tracking-wider text-stone-700 mb-1.5">
              Author's Manuscript Context & Key Findings (Optional)
            </label>
            <textarea
              value={manuscriptContext}
              onChange={(e) => setManuscriptContext(e.target.value)}
              placeholder="Add key empirical numbers, metrics, or methods the author can cite in response (e.g. 'Model achieved 92.4% accuracy with 1.21 mJ energy per inference across 6 benchmark datasets. Code is open-sourced at GitHub.')"
              className="w-full min-h-[100px] p-3 text-xs text-stone-800 border border-stone-300 rounded-lg focus:outline-none focus:ring-1 focus:ring-stone-900 leading-relaxed resize-y"
            />
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-semibold uppercase tracking-wider text-stone-700 mb-1.5">
                Target Journal
              </label>
              <input
                type="text"
                value={journal}
                onChange={(e) => setJournal(e.target.value)}
                className="w-full p-2 text-xs text-stone-900 border border-stone-300 rounded-lg focus:outline-none focus:ring-1 focus:ring-stone-900"
              />
            </div>

            <div>
              <label className="block text-xs font-semibold uppercase tracking-wider text-stone-700 mb-1.5">
                Diplomatic Stance Tone
              </label>
              <select
                value={authorTone}
                onChange={(e) =>
                  setAuthorTone(
                    e.target.value as 'collaborative' | 'diplomatic_firm' | 'formal_rigorous'
                  )
                }
                className="w-full p-2 text-xs text-stone-900 border border-stone-300 rounded-lg focus:outline-none focus:ring-1 focus:ring-stone-900 bg-white"
              >
                <option value="collaborative">Collaborative (Standard)</option>
                <option value="diplomatic_firm">Diplomatic & Firm</option>
                <option value="formal_rigorous">Formal Rigorous</option>
              </select>
            </div>
          </div>

          <button
            onClick={handleAnalyze}
            disabled={loading}
            className="w-full py-3 px-4 rounded-lg bg-stone-900 hover:bg-stone-800 text-stone-100 font-medium text-xs transition disabled:opacity-50 flex items-center justify-center gap-2"
          >
            {loading ? (
              <>
                <span className="inline-block animate-spin">⏳</span>
                <span>Decomposing & Synthesizing Rebuttal...</span>
              </>
            ) : (
              <>
                <span>📝</span>
                <span>Analyze & Synthesize Rebuttal</span>
              </>
            )}
          </button>

          {error && (
            <div className="p-3 bg-red-50 border border-red-200 text-red-700 text-xs rounded-lg">
              {error}
            </div>
          )}
        </div>

        {/* Right Panel: Interactive Workbench & Results */}
        <div className="lg:col-span-7 space-y-6">
          {!result && !loading && (
            <div className="border border-dashed border-stone-300 rounded-xl p-12 text-center text-stone-500 bg-stone-50">
              <div className="text-3xl mb-3">📑</div>
              <h3 className="text-sm font-semibold text-stone-800 mb-1">
                No Reviewer Report Loaded
              </h3>
              <p className="text-xs text-stone-500 max-w-sm mx-auto">
                Paste reviewer feedback on the left and click "Analyze & Synthesize Rebuttal" to decompose critiques into discrete cards.
              </p>
            </div>
          )}

          {result && (
            <>
              {/* Strategy Overview Strip */}
              <div className="p-5 rounded-xl bg-stone-900 text-stone-100 space-y-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <span className="text-[10px] font-bold uppercase tracking-widest bg-stone-700 px-2 py-0.5 rounded text-stone-200">
                      Negotiation Overview
                    </span>
                    <span className="text-xs text-stone-300 font-mono">
                      {result.total_comments_identified} Critiques Decomposed
                    </span>
                  </div>
                  <button
                    onClick={handleCopyFullExport}
                    className="text-xs px-2.5 py-1 rounded bg-stone-800 hover:bg-stone-700 text-stone-200 border border-stone-700 flex items-center gap-1.5 transition"
                  >
                    <span>{exportCopied ? '✓ Copied' : '📋 Copy Full Letter'}</span>
                  </button>
                </div>

                <p className="text-xs text-stone-300 leading-relaxed">
                  {result.overall_negotiation_strategy}
                </p>

                {/* Category Breakdown Chips */}
                <div className="flex flex-wrap items-center gap-2 pt-1 border-t border-stone-800">
                  <span className="text-[11px] px-2 py-0.5 rounded bg-emerald-950 text-emerald-300 border border-emerald-800">
                    🟢 {result.category_breakdown.agree_and_amend ?? 0} Agree & Amend
                  </span>
                  <span className="text-[11px] px-2 py-0.5 rounded bg-sky-950 text-sky-300 border border-sky-800">
                    🔵 {result.category_breakdown.clarify_misconception ?? 0} Clarify
                  </span>
                  <span className="text-[11px] px-2 py-0.5 rounded bg-amber-950 text-amber-300 border border-amber-800">
                    🟠 {result.category_breakdown.partial_agree_bounded ?? 0} Bounded
                  </span>
                  <span className="text-[11px] px-2 py-0.5 rounded bg-purple-950 text-purple-300 border border-purple-800">
                    🟣 {result.category_breakdown.contest_with_evidence ?? 0} Contest
                  </span>
                </div>
              </div>

              {/* Reviewer Block Summaries */}
              <div className="space-y-2">
                <h3 className="text-xs font-semibold uppercase tracking-wider text-stone-700">
                  Reviewer Groupings
                </h3>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                  {result.reviewer_blocks.map((b: ReviewerBlock) => (
                    <div
                      key={b.reviewer_id}
                      className="p-3 bg-white border border-stone-200 rounded-lg text-xs"
                    >
                      <div className="font-semibold text-stone-900 mb-1">
                        {b.reviewer_id} ({b.total_comments} points)
                      </div>
                      <p className="text-stone-500 text-[11px] leading-relaxed">
                        {b.negotiation_summary}
                      </p>
                    </div>
                  ))}
                </div>
              </div>

              {/* Individual Comment Cards */}
              <div className="space-y-4">
                <h3 className="text-xs font-semibold uppercase tracking-wider text-stone-700">
                  Itemized Point-by-Point Cards
                </h3>
                {result.comments.map((comment: RebuttalCommentItem) => {
                  const badge = getCategoryBadge(comment.category)
                  const isClean = comment.tone_audit.is_clean
                  const toneScore = comment.tone_audit.diplomatic_score

                  return (
                    <div
                      key={comment.comment_id}
                      className="bg-white border border-stone-300 rounded-xl p-5 shadow-sm space-y-4"
                    >
                      {/* Card Header */}
                      <div className="flex flex-wrap items-center justify-between gap-2 pb-3 border-b border-stone-100">
                        <div className="flex items-center gap-2">
                          <span className="text-xs font-mono font-bold px-2 py-0.5 bg-stone-100 rounded text-stone-800 border border-stone-200">
                            {comment.comment_id}
                          </span>
                          <span className={`text-[10px] font-semibold px-2 py-0.5 rounded border ${badge.bg}`}>
                            {badge.icon} {badge.label}
                          </span>
                        </div>
                        <div className="flex items-center gap-2">
                          <span
                            className={`text-[10px] font-medium px-2 py-0.5 rounded ${
                              toneScore >= 80
                                ? 'bg-emerald-50 text-emerald-700 border border-emerald-200'
                                : 'bg-amber-50 text-amber-700 border border-amber-200'
                            }`}
                          >
                            Diplomacy: {toneScore}/100 {isClean ? '✓' : '⚠'}
                          </span>
                        </div>
                      </div>

                      {/* Reviewer Quote */}
                      <div>
                        <div className="text-[10px] uppercase font-bold tracking-wider text-stone-400 mb-1">
                          Reviewer's Quote:
                        </div>
                        <blockquote className="p-3 bg-stone-50 border-l-2 border-stone-400 text-xs italic text-stone-700 rounded-r leading-relaxed">
                          "{comment.original_quote}"
                        </blockquote>
                      </div>

                      {/* Core Critique Summary */}
                      <div className="text-xs text-stone-500">
                        <span className="font-semibold text-stone-700">Critique Focus:</span>{' '}
                        {comment.core_critique_summary}
                      </div>

                      {/* Drafted Response (Editable) */}
                      <div>
                        <div className="flex items-center justify-between mb-1.5">
                          <label className="text-[11px] font-semibold text-stone-800">
                            Drafted Diplomatic Response:
                          </label>
                          <span className="text-[10px] text-stone-400">Editable</span>
                        </div>
                        <textarea
                          value={localResponses[comment.comment_id] ?? comment.drafted_response}
                          onChange={(e) =>
                            setLocalResponses({
                              ...localResponses,
                              [comment.comment_id]: e.target.value,
                            })
                          }
                          className="w-full min-h-[90px] p-2.5 text-xs text-stone-900 border border-stone-300 rounded-lg focus:outline-none focus:ring-1 focus:ring-stone-900 leading-relaxed resize-y"
                        />
                      </div>

                      {/* Action in Revised Manuscript */}
                      {comment.action_in_manuscript && (
                        <div className="p-2.5 bg-amber-50 border border-amber-200 rounded-lg text-xs">
                          <span className="font-semibold text-amber-900">
                            Action in Manuscript:{' '}
                          </span>
                          <span className="text-amber-800">
                            {comment.action_in_manuscript}
                          </span>
                        </div>
                      )}

                      {/* Citation Hooks / Pending Literature Slots */}
                      {comment.citation_hooks.length > 0 && (
                        <div className="p-2.5 bg-purple-50 border border-purple-200 rounded-lg space-y-1">
                          <div className="text-[11px] font-semibold text-purple-900">
                            Pending Citation Verification:
                          </div>
                          <div className="flex flex-wrap gap-1.5">
                            {comment.citation_hooks.map((h, i) => (
                              <span
                                key={i}
                                className="text-[10px] px-2 py-0.5 rounded bg-purple-100 text-purple-800 border border-purple-300 font-mono"
                              >
                                {h}
                              </span>
                            ))}
                          </div>
                        </div>
                      )}

                      {/* Card Footer Actions */}
                      <div className="flex flex-wrap items-center justify-between gap-2 pt-2 border-t border-stone-100">
                        <span className="text-[10px] text-stone-400 italic">
                          {comment.diplomatic_posture_label}
                        </span>
                        <div className="flex items-center gap-2">
                          <button
                            onClick={() =>
                              handleSendToCalibrator(
                                localResponses[comment.comment_id] ?? comment.drafted_response
                              )
                            }
                            className="text-xs px-2 py-1 rounded bg-stone-100 hover:bg-stone-200 text-stone-700 transition"
                          >
                            ✨ Polish in Voice Calibrator
                          </button>
                          <button
                            onClick={() => handleCopyComment(comment.comment_id)}
                            className="text-xs px-2.5 py-1 rounded bg-stone-900 hover:bg-stone-800 text-stone-100 transition"
                          >
                            {copiedId === comment.comment_id ? '✓ Copied' : '📋 Copy Point'}
                          </button>
                        </div>
                      </div>
                    </div>
                  )
                })}
              </div>

              {/* Manuscript Revision Checklist */}
              {result.manuscript_revision_checklist.length > 0 && (
                <div className="p-5 bg-white border border-stone-200 rounded-xl space-y-3">
                  <h3 className="text-xs font-semibold uppercase tracking-wider text-stone-800 flex items-center justify-between">
                    <span>Manuscript Revision Checklist</span>
                    <span className="text-[11px] text-stone-400 lowercase font-normal">
                      derived from agreed critiques
                    </span>
                  </h3>
                  <div className="space-y-2">
                    {result.manuscript_revision_checklist.map((item: string, idx: number) => {
                      const isDone = checkedItems[idx] ?? false
                      return (
                        <label
                          key={idx}
                          className="flex items-start gap-2.5 text-xs text-stone-800 cursor-pointer p-2 rounded hover:bg-stone-50"
                        >
                          <input
                            type="checkbox"
                            checked={isDone}
                            onChange={() =>
                              setCheckedItems({
                                ...checkedItems,
                                [idx]: !isDone,
                              })
                            }
                            className="mt-0.5 rounded border-stone-300 text-stone-900 focus:ring-0"
                          />
                          <span className={isDone ? 'line-through text-stone-400' : ''}>
                            {item}
                          </span>
                        </label>
                      )
                    })}
                  </div>
                </div>
              )}

              {/* Publication-Ready Document Preview */}
              <div className="p-5 bg-white border border-stone-200 rounded-xl space-y-3">
                <div className="flex items-center justify-between">
                  <h3 className="text-xs font-semibold uppercase tracking-wider text-stone-800">
                    Publication-Ready Rebuttal Letter (Markdown)
                  </h3>
                  <button
                    onClick={handleCopyFullExport}
                    className="text-xs px-3 py-1 rounded bg-stone-900 hover:bg-stone-800 text-stone-100 transition"
                  >
                    {exportCopied ? '✓ Copied' : '📋 Copy Full Letter'}
                  </button>
                </div>
                <pre className="p-4 bg-stone-50 border border-stone-200 rounded-lg text-xs font-mono text-stone-800 overflow-x-auto max-h-72 whitespace-pre-wrap leading-relaxed">
                  {result.export_document_md}
                </pre>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
