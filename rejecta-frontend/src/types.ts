export type Mood =
  | 'furious'
  | 'skeptical'
  | 'unimpressed'
  | 'neutral'
  | 'interested'
  | 'delighted'

export type CitationStatus = 'verified' | 'ghost' | 'unverified'

export interface CitationResult {
  raw: string
  doi: string | null
  status: CitationStatus
  title: string | null
  authors?: string[]
  year: number | null
  venue?: string | null
  is_preprint?: boolean
  source_url?: string | null
  diagnostic_reason?: string
  suggested_action?: string | null
}

export interface CitationSummary {
  verified: number
  ghost: number
  unverified: number
  total: number
  preprint_count?: number
  peer_reviewed_count?: number
  preprint_ratio_pct?: number
  median_citation_age_years?: number | null
  editorial_warnings?: string[]
  items: CitationResult[]
}

export interface SectionScore {
  score: number
  issue?: string
  fix?: string
  strength?: string
  weakness?: string
}

export interface RejectionReason {
  reason: string
  severity: 'desk_reject' | 'major' | 'minor'
  fix: string
}

export interface AnalysisResult {
  mood: Mood
  mood_score: number
  verdict: string
  oracle_letter: string
  sections: Record<string, SectionScore>
  rejection_reasons: RejectionReason[]
  novelty_assessment: string
  journal_fit: string
  provider_used: string
}

export interface JournalMatch {
  found: boolean
  target: string
  openalex_name: string | null
  works_count: number | null
  recent_papers: Array<{ title: string; year: number | null }>
}

export interface AnalysisPaperResponse {
  analysis: AnalysisResult
  citations: CitationSummary
  journal: JournalMatch
  meta: {
    word_count: number
    sections_found: string[]
    references_found: number
    mode: string
    processing_ms: number
  }
}

export type Mode = 'a' | 'b'

export interface VoiceCalibrateRequest {
  text: string
  journal: string
  field?: string
  intensity?: 'polish' | 'rewrite' | 'restructure'
}

export interface VoiceCalibrateResponse {
  calibrated_text: string | null
  original_text: string
  status: 'verified' | 'blocked' | 'guidance' | 'no_invariants'
  input_type: 'manuscript_draft' | 'reviewer_critique'
  invariants_verified: boolean
  total_detected_invariants: number
  total_restored_invariants: number
  word_count_original: number
  word_count_calibrated: number
  synthetic_markers_removed: number
  intensity_applied: string
  audit_notes: string[]
  block_reasons?: string[]
  guidance_items?: string[]
}

export interface SentenceHeatDetail {
  sentence_index: number
  text: string
  start_utf16: number
  end_utf16: number
  signal_score: number
  level: 'clean' | 'moderate' | 'elevated'
  feature_scores: {
    sentence_length_uniformity: number
    transition_repetition: number
    passive_construction_ratio: number
    lexical_repetition: number
  }
  diagnostics: string[]
  evidence_spans: string[]
  suggested_remedy: string | null
}

export interface ParagraphHeatDetail {
  paragraph_index: number
  text: string
  start_utf16: number
  end_utf16: number
  paragraph_heat_score: number
  level: 'clean' | 'moderate' | 'elevated'
  sentence_count: number
  paragraph_burstiness_cv: number
  sentences: SentenceHeatDetail[]
  primary_issue: string | null
}

export interface HeatmapAnalyzeRequest {
  text: string
  journal?: string
  field?: string
}

export interface HeatmapAnalyzeResponse {
  analysis_version: string
  offset_unit: string
  source_text_hash: string
  overall_cadence_score: number
  overall_level: 'low_signals' | 'mixed_signals' | 'elevated_signals'
  total_paragraphs: number
  total_sentences: number
  total_words: number
  flagged_sentences_count: number
  paragraphs: ParagraphHeatDetail[]
  executive_assessment: string
  readability_metrics: {
    average_sentence_length: number
    sentence_burstiness_index: number
    burstiness_qualitative_tag?: string
    flagged_sentence_ratio: number
  }
  warnings: string[]
}

export interface ReadinessScoreBreakdown {
  overall_readiness_score: number
  empirical_anchoring: number
  comparison_grounding: number
  tradeoff_retention: number
  narrative_discipline: number
}

export interface ClaimAuditDetail {
  is_faithful: boolean
  generalized_baselines_detected: string[]
  omitted_tradeoffs_detected: string[]
  unsupported_causality_detected: string[]
  audit_verdict: string
}

export interface AbstractVariantDetail {
  variant_type: 'discoverability_seo' | 'clarity_direct' | 'high_impact_narrative'
  title_label: string
  target_persona: string
  abstract_text: string
  word_count: number
  readiness_index?: ReadinessScoreBreakdown
  predicted_acceptance_score: number
  score_mood: string
  key_highlights: string[]
  seo_keywords?: string[]
  claim_audit?: ClaimAuditDetail
  invariants_verified: boolean
}

export interface AbstractForgeRequest {
  abstract_draft: string
  methodology_context?: string
  journal?: string
  field?: string
}

export interface InvariantDetailSummary {
  total_spans: number
  by_category: Record<string, number>
  all_spans_verified: boolean
  summary_label: string
}

export interface AbstractForgeResponse {
  source_word_count: number
  target_journal: string
  fact_inventory?: {
    explicit_tradeoffs: string[]
    explicit_comparisons: string[]
    mandatory_paired_stats?: string[]
    has_tradeoffs: boolean
    has_comparisons: boolean
    has_paired_stats?: boolean
  }
  variants: AbstractVariantDetail[]
  comparative_summary: string
  invariants_preserved_count: number
  invariant_telemetry?: InvariantDetailSummary
}

export interface ToneAuditResult {
  is_clean: boolean
  diplomatic_score: number
  flags: string[]
}

export interface RebuttalCommentItem {
  comment_id: string
  reviewer_id: string
  original_quote: string
  category: 'agree_and_amend' | 'clarify_misconception' | 'partial_agree_bounded' | 'contest_with_evidence'
  diplomatic_posture_label: string
  core_critique_summary: string
  drafted_response: string
  action_in_manuscript: string
  citation_hooks: string[]
  tone_audit: ToneAuditResult
}

export interface ReviewerBlock {
  reviewer_id: string
  total_comments: number
  category_breakdown: Record<string, number>
  negotiation_summary: string
}

export interface RebuttalAnalyzeRequest {
  reviewer_report_text: string
  manuscript_context?: string
  journal?: string
  author_tone?: 'collaborative' | 'diplomatic_firm' | 'formal_rigorous'
}

export interface RebuttalAnalyzeResponse {
  total_comments_identified: number
  reviewer_blocks: ReviewerBlock[]
  category_breakdown: Record<string, number>
  overall_negotiation_strategy: string
  comments: RebuttalCommentItem[]
  manuscript_revision_checklist: string[]
  export_document_md: string
}
