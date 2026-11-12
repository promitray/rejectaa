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
  /** True when a lookup service was busy, so this reference was not actually checked. */
  check_incomplete?: boolean
}

export interface CitationSummary {
  verified: number
  ghost: number
  /** All unverified references, including those that could not be checked. */
  unverified: number
  /** Subset of `unverified` that could not be checked (service busy), not "not found". */
  check_incomplete?: number
  total: number
  preprint_count?: number
  peer_reviewed_count?: number
  preprint_ratio_pct?: number
  median_citation_age_years?: number | null
  editorial_warnings?: string[]
  items: CitationResult[]
}

export type ArticleType = 'research_article' | 'communication' | 'perspective' | 'review' | 'mini_review' | 'editorial'

/** One step of a section's fix plan: what is wrong, the words it is about, and what to do. */
export interface FixStep {
  step: number
  severity: 'major' | 'minor'
  problem: string
  /** The words of the paper the step is about; empty when something is missing. */
  quote: string
  action: string
  /** "review": the section review (quoted from the paper). "checklist": decided by code. */
  source: 'review' | 'checklist'
  /** False when the words the step is about are in a part of the section that Calibrate does not receive. */
  in_calibrate_text?: boolean
  /** "add": something the section lacks (a placeholder goes where it belongs). "change": words to rewrite. */
  kind?: 'add' | 'change'
}

/** A problem from the section review. `quote` was found in the paper by the server. */
export interface SectionFinding {
  severity: 'major' | 'minor'
  problem: string
  quote: string
  fix: string
}

export interface SectionStrength {
  point: string
  quote: string
}

/** One checklist item decided by code from the paper's text; every quote is copied from the paper. */
export interface SectionCheck {
  key: string
  label: string
  /** met: the paper does it. missing: it should and does not. issue: something in the text is wrong. info: a fact. */
  status: 'met' | 'missing' | 'issue' | 'info'
  severity?: 'major' | 'minor' | null
  detail: string
  quote: string
  fix: string
}

export interface SectionScore {
  score: number
  issue?: string
  fix?: string
  strength?: string
  weakness?: string
  /** The heading as the paper prints it ("Conclusion"), when it differs from the section's key ("discussion"). */
  title?: string | null
  role?: string | null
  checks?: SectionCheck[]
  /** The section review: a summary, up to two strengths and up to four findings, each quoted from the paper. */
  summary?: string | null
  strengths?: SectionStrength[]
  findings?: SectionFinding[]
  /** What to change, most important first (verified findings and checklist problems). Empty when nothing needs fixing. */
  fix_plan?: FixStep[]
  /** The model's own number, kept when `score` was computed in code from the findings and the checklist. */
  llm_score?: number | null
  /** The steps of the computed score ("Start at 85 ...", "−10 major finding: ..."). */
  score_basis?: string[]
  /** "detected": a heading in the file. "derived": found from the content, starting at `anchor`. */
  source?: 'detected' | 'derived' | null
  anchor?: string | null
  /** The text of the paper this section covers (cut by the server from where it starts to where the next starts). */
  content?: string | null
  content_truncated?: boolean
  word_count?: number | null
  /** Where the section starts and ends, as a percentage of the paper's body text. */
  start_pct?: number | null
  end_pct?: number | null
  /** The section text prepared by the server for Calibrate and Heatmap (captions left out, length limited). */
  tool_text?: string | null
  /** What the server left out of or cut from `tool_text`. */
  tool_notes?: string[]
}

export interface CriterionScore {
  score: number
  reason: string
  weight: number
  /** True when the score was computed from the reference check, not by the model. */
  computed: boolean
  /** Citation integrity only: share of references whose status could be settled (verified or ghost). */
  coverage?: number | null
  /** The model's score before code applied its limits. */
  raw_score?: number | null
  /** The limits code applied, in words (e.g. "Capped at 60: 2 major inconsistencies ..."). */
  adjustments?: string[]
}

export interface RejectionReason {
  reason: string
  severity: 'desk_reject' | 'major' | 'minor'
  fix: string
  /** The four parts of a reason. `reason` repeats the problem and `fix` the solution. */
  problem?: string
  /** Words from the manuscript this reason is about (checked against the file). */
  evidence?: string
  /** Section and nearby figure of the evidence, found by code. */
  location?: string
  impact_area?: 'novelty' | 'scientific_validity' | 'publication_potential' | ''
  impact?: string
  solution?: string
  /** The report section the evidence sits in (a key of `sections`), or '' when it is outside them. */
  section?: string
  /** The paragraphs around the evidence, prepared by the server for "Fix with Calibrator". */
  tool_text?: string | null
  tool_notes?: string[]
  /** Who raised it: the model, or code (publication record, consistency check). */
  origin?: 'model' | 'publication_record' | 'consistency'
  /** The scored criterion this concern bears on. */
  criterion?: string
  /** For an unverified concern: why it could not be tied to the paper. */
  unverified_reason?: string
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
  /** Article type the analysis used. */
  paper_format?: ArticleType | 'unknown' | null
  /** The paper's scientific field, passed on to the Calibrate and Heatmap tools. */
  field?: string | null
  /** The fixed criteria the overall score is the weighted mean of. */
  criteria?: Record<string, CriterionScore>
  /** What the uploaded file did not let us judge (for example Supporting Information). */
  not_assessed?: string[]
  /** Claims in the report that were removed or flagged because the manuscript does not support them. */
  grounding_notes?: string[]
  /** Concerns the model raised that could not be tied to words in the paper. Shown apart, never scored. */
  unverified_concerns?: RejectionReason[]
}

export interface IntegrityNotice {
  type: string
  label?: string
  doi?: string | null
  date?: string | null
  source?: string
}

/** Whether the paper is already published, and whether it was retracted or put under concern. */
export interface PublicationStatus {
  status: 'retracted' | 'expression_of_concern' | 'corrected' | 'published' | 'not_found' | 'not_checked'
  doi?: string | null
  doi_source?: string | null
  title?: string | null
  journal?: string | null
  publisher?: string | null
  year?: number | null
  notices?: IntegrityNotice[]
  file_marks?: { kind: string; text: string; pages: number }[]
  is_notice?: boolean
  journal_abbreviation?: string | null
  /** Published in the very journal the author is targeting. */
  at_target_journal?: boolean | null
  sources?: string[]
  notes?: string[]
}

export interface ConsistencyQuote {
  quote: string
  section: string
  values: string
}

export interface ConsistencyFinding {
  kind: string
  severity: 'major' | 'minor'
  message: string
  quotes: ConsistencyQuote[]
}

export interface CellLineCheck {
  name: string
  status: 'problematic' | 'no_known_problem' | 'not_found' | 'not_checked'
  accession?: string | null
  note?: string
  quote?: string
}

/** The paper's own values, compounds and cell lines, compared by code. */
export interface ConsistencyReport {
  status: 'checked' | 'not_checked' | 'not_applicable'
  findings: ConsistencyFinding[]
  facts_checked: number
  cell_lines: CellLineCheck[]
  note?: string
}

/** Competing-interest and funding statements as the paper prints them. Context, never a fault. */
export interface Disclosures {
  competing_interests?: string | null
  funding?: string | null
  declares_none: boolean
  interests: string[]
}

export interface ScoreChange {
  name: string
  /** null: not scored in that version */
  before: number | null
  after: number | null
}

export interface PreviousIssue {
  problem: string
  severity: string
  status: 'resolved' | 'partly_resolved' | 'open' | 'not_assessed'
  note: string
}

/** This upload next to the earlier version of the same paper that the account analysed. */
export interface RevisionComparison {
  previous_analysis_id: string
  previous_analysed_at: string
  previous_filename: string
  previous_journal: string
  /** The very same file again: any difference is the analysis varying, not the paper. */
  same_file: boolean
  same_journal: boolean
  similarity: number
  score_before: number
  score_after: number
  criteria: ScoreChange[]
  sections: ScoreChange[]
  issues: PreviousIssue[]
}

export interface JournalMatch {
  found: boolean
  target: string
  openalex_name: string | null
  works_count: number | null
  recent_papers: Array<{ title: string; year: number | null }>
  similar_papers?: Array<{ title: string; year: number | null }>
  match_quality?: 'exact' | 'approximate'
  note?: string | null
  two_year_mean_citedness?: number | null
  h_index?: number | null
}

export interface DocumentSection {
  id: string
  name: string
  /** False when the section could not be found in the working copy. */
  mapped: boolean
  paragraphs: number
}

/** An editable Word copy of the uploaded paper, kept on the server for 24 hours. */
export interface DocumentInfo {
  id: string
  filename: string
  source_format: 'pdf' | 'docx'
  version: number
  can_undo: boolean
  expires_at: string
  sections: DocumentSection[]
  notes: string[]
}

/** Calibrated text for a section, not yet in the paper. */
export interface DocumentProposal {
  /** null when the calibration was blocked: nothing to apply. */
  proposal_id: string | null
  section_id: string
  base_version: number
  original_text: string
  proposed_text: string | null
  warnings: string[]
  notes: string[]
  /** Pass as from_paragraph to calibrate the rest of a long section. */
  next_from: number | null
  calibration: VoiceCalibrateResponse
}

export interface ApplyResult {
  document: DocumentInfo
  section_id: string
  applied_paragraphs: number
  section_text: string
  next_from: number | null
}

export interface AnalysisPaperResponse {
  analysis: AnalysisResult
  citations: CitationSummary
  journal: JournalMatch
  /** The editable copy, when the user asked to keep one. */
  document?: DocumentInfo | null
  /** Why no copy was kept, when one was asked for. */
  document_note?: string | null
  /** Set when this paper is a version of one the account analysed before. */
  revision?: RevisionComparison | null
  /** Published / retracted / under concern, checked before the paper is scored. */
  integrity?: PublicationStatus | null
  consistency?: ConsistencyReport | null
  disclosures?: Disclosures | null
  meta: {
    word_count: number
    sections_found: string[]
    references_found: number
    mode: string
    processing_ms: number
    article_type?: ArticleType | 'unknown' | null
    /** "detected" headings, or sections "derived" from the content when the file has no headings. */
    structure_mode?: 'detected' | 'derived' | null
    /** The same file with the same settings was analysed before: this is that report. */
    cached?: boolean
    analysed_at?: string | null
  }
}

export type Mode = 'a' | 'b'

export interface VoiceCalibrateRequest {
  text: string
  journal: string
  field?: string
  intensity?: 'polish' | 'rewrite' | 'restructure'
  /** The review's note on the section being calibrated (issue and fix), up to 700 characters. */
  focus?: string
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
  /** Patterns seen across the whole paragraph (stock-phrase density, same openings, three-item lists, em dashes). */
  paragraph_signals?: string[]
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
  /** True when a protected number or citation was lost: the text is withheld. */
  blocked?: boolean
  block_reasons?: string[]
  /** Problems still present after the server's retry. */
  warnings?: string[]
}

export interface AbstractForgeRequest {
  abstract_draft: string
  methodology_context?: string
  journal?: string
  field?: string
  /** The journal's abstract word limit. */
  max_words?: number
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
  max_words?: number | null
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
