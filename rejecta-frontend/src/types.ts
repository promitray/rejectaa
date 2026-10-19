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
