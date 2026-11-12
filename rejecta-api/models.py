"""Pydantic models shared across API endpoints and services."""

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class CitationStatus(str, Enum):
    """Enumeration of citation verification states."""

    verified = "verified"
    ghost = "ghost"
    unverified = "unverified"


class ParsedDocument(BaseModel):
    """Structured representation of a parsed academic document."""

    full_text: str
    abstract: str
    sections: dict[str, str]
    references: list[str]
    word_count: int = Field(ge=0)
    # How the paper was read. All optional so older callers and tests keep working.
    article_type: str | None = None  # research_article | communication | perspective | review | mini_review
    article_type_source: str = "none"  # label | user | none
    structure: str = "none"  # detected | partial | none: how much of the paper sits under real headings
    coverage: float = 0.0
    refers_to_si: bool = False  # the paper points to Supporting Information that was not uploaded
    section_roles: dict[str, str] = Field(default_factory=dict)  # section key -> role
    # section key -> its heading as printed ("Methodology", "Conclusion"); the key itself stays stable
    section_titles: dict[str, str] = Field(default_factory=dict)
    body: str = Field(default="", exclude=True)  # cleaned body text, used when headings are missing
    # What the parser could not read properly (e.g. part of the reference list); shown with the report.
    parse_warnings: list[str] = Field(default_factory=list)
    # Fingerprint of the section names and roles: the same file always gives the same value.
    structure_signature: str = ""
    # For the publication-integrity check: what the file itself says about its status and identity.
    integrity_marks: list["FileMark"] = Field(default_factory=list)
    doi_candidates: list[str] = Field(default_factory=list)  # the paper's own DOI, most likely first
    title: str = ""  # the paper's title as printed (largest type on the first page, or the Word title)
    front_text: str = Field(default="", exclude=True)  # first page text, to confirm a DOI belongs to this paper


class FileMark(BaseModel):
    """A status printed in the file itself, such as a RETRACTED ARTICLE watermark."""

    kind: Literal["retracted", "expression_of_concern", "withdrawn"]
    text: str  # the words as printed
    pages: int = 1  # on how many pages they appear


class IntegrityNotice(BaseModel):
    """A notice the publisher or Retraction Watch registered against the paper (from Crossref)."""

    type: str  # retraction | partial_retraction | withdrawal | removal | expression_of_concern | correction | ...
    label: str = ""
    doi: str | None = None  # the notice's own DOI
    date: str | None = None  # ISO date
    source: str = ""  # publisher | retraction-watch


class PublicationStatus(BaseModel):
    """Whether the uploaded paper is already published, and whether it was retracted or put under concern."""

    status: Literal[
        "retracted", "expression_of_concern", "corrected", "published", "not_found", "not_checked"
    ] = "not_checked"
    doi: str | None = None
    doi_source: str | None = None  # file | title_search
    title: str | None = None
    journal: str | None = None
    publisher: str | None = None
    year: int | None = None
    notices: list[IntegrityNotice] = Field(default_factory=list)
    file_marks: list[FileMark] = Field(default_factory=list)
    is_notice: bool = False  # the uploaded file is itself a retraction or correction notice
    journal_abbreviation: str | None = None
    at_target_journal: bool | None = None  # published in the very journal the author is targeting
    sources: list[str] = Field(default_factory=list)  # services that answered
    notes: list[str] = Field(default_factory=list)  # e.g. sources that disagree


class ConsistencyQuote(BaseModel):
    """A passage of the paper that a consistency finding rests on (checked to be in the paper)."""

    quote: str
    section: str = ""  # abstract, introduction, methods, results, discussion... or "" when outside the sections
    values: str = ""  # the value(s) the passage gives, as read


class ConsistencyFinding(BaseModel):
    """A contradiction or an implausible value that code found by comparing the paper's own numbers."""

    kind: Literal[
        "value_mismatch", "category_mismatch", "percent_claim", "unit_mismatch", "implausible_value",
        "foreign_compound", "problematic_cell_line",
    ]
    severity: Literal["major", "minor"] = "major"
    message: str
    quotes: list[ConsistencyQuote] = Field(default_factory=list)


class CellLineCheck(BaseModel):
    """One cell line named in the paper, looked up in Cellosaurus."""

    name: str
    status: Literal["problematic", "no_known_problem", "not_found", "not_checked"] = "not_checked"
    accession: str | None = None  # Cellosaurus accession, e.g. CVCL_0520
    note: str = ""  # the Cellosaurus "Problematic cell line" comment, when there is one
    quote: str = ""


class ConsistencyReport(BaseModel):
    """What comparing the paper's own values, compounds and cell lines found."""

    status: Literal["checked", "not_checked", "not_applicable"] = "not_checked"
    findings: list[ConsistencyFinding] = Field(default_factory=list)
    facts_checked: int = 0  # values read from the paper and confirmed to be in it
    cell_lines: list[CellLineCheck] = Field(default_factory=list)
    note: str = ""


class Disclosures(BaseModel):
    """The paper's competing-interest and funding statements, as printed. Context for the reader, never a fault."""

    competing_interests: str | None = None
    funding: str | None = None
    declares_none: bool = False  # the authors state they have no competing interests
    interests: list[str] = Field(default_factory=list)  # kinds of interest the statement names: patent, stock...


class PublicationType(str, Enum):
    """Classification of publication format."""

    journal = "journal"
    conference = "conference"
    preprint = "preprint"
    book = "book"
    unknown = "unknown"


class CitationResult(BaseModel):
    """Verification output for a single citation entry."""

    raw: str
    doi: str | None = None
    status: CitationStatus
    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    venue: str | None = None
    publication_type: str = PublicationType.unknown.value
    is_preprint: bool = False
    source_url: str | None = None
    confidence_score: float = Field(ge=0.0, le=1.0, default=1.0)
    diagnostic_reason: str = ""
    suggested_action: str | None = None
    # True when a lookup service was busy or unreachable, so the reference was NOT actually checked.
    check_incomplete: bool = False


class CitationSummary(BaseModel):
    """Aggregated citation verification metrics and bibliometric telemetry."""

    verified: int = Field(ge=0)
    ghost: int = Field(ge=0)
    unverified: int = Field(ge=0)
    # Subset of `unverified` that could not be checked (service busy), as opposed to "not found".
    check_incomplete: int = Field(default=0, ge=0)
    total: int = Field(ge=0)
    preprint_count: int = Field(default=0, ge=0)
    peer_reviewed_count: int = Field(default=0, ge=0)
    preprint_ratio_pct: float = Field(default=0.0, ge=0.0, le=100.0)
    median_citation_age_years: float | None = None
    editorial_warnings: list[str] = Field(default_factory=list)
    items: list[CitationResult]


class RecentPaper(BaseModel):
    """One recent paper from the matched journal."""

    title: str
    year: int | None = None


class JournalMatch(BaseModel):
    """Journal fit and retrieval details for the manuscript."""

    found: bool
    target: str
    openalex_name: str | None = None
    works_count: int | None = None
    recent_papers: list[RecentPaper] = Field(default_factory=list)  # newest research articles
    similar_papers: list[RecentPaper] = Field(default_factory=list)  # the journal's articles closest to this paper
    match_quality: str = "exact"  # exact | approximate (the name typed matched no journal exactly)
    note: str | None = None  # shown to the user when the match is approximate
    two_year_mean_citedness: float | None = None  # how much the journal's recent articles are cited
    h_index: int | None = None


class Mood(str, Enum):
    """Editor mood values the app can render."""

    furious = "furious"
    skeptical = "skeptical"
    unimpressed = "unimpressed"
    neutral = "neutral"
    interested = "interested"
    delighted = "delighted"


class Severity(str, Enum):
    """Rejection-reason severity values the app can colour."""

    desk_reject = "desk_reject"
    major = "major"
    minor = "minor"


class SectionCheck(BaseModel):
    """One item of a section's checklist, decided by code from the text (services.section_checks).

    status: "met" (the paper does it; `quote` shows where), "missing" (it should and does not), "issue"
    (something in the text is wrong or doubtful; `quote` shows it), "info" (a fact, no judgement).
    Every quote is copied from the paper, so the same file always gives the same checklist.
    """

    key: str
    label: str
    status: str
    severity: str | None = None  # "major" | "minor" for missing and issue; None otherwise
    detail: str = ""
    quote: str = ""
    fix: str = ""


class SectionFinding(BaseModel):
    """A problem in one section, from the section review (services.section_review). `quote` was found in the paper."""

    severity: str  # "major" | "minor"
    problem: str
    quote: str
    fix: str


MAX_FOCUS_CHARS = 4000  # the editor's note a section is calibrated with: its issues and their fixes


class FixStep(BaseModel):
    """One step of a section's fix plan (services.section_review.build_fix_plan): what is wrong, where, what to do."""

    step: int
    severity: str  # "major" | "minor"
    problem: str
    quote: str = ""  # the words of the paper it is about; empty when the problem is something missing
    action: str
    source: str  # "review" (the section review) | "checklist" (decided by code)
    # False when the words the step is about are in a part of the section that Calibrate does not receive
    # (a long section is sent in parts): the step cannot be applied to the text it gets.
    in_calibrate_text: bool = True
    # "add": something the section lacks (a placeholder goes where it belongs); "change": words to rewrite
    kind: str = "change"


class SectionStrength(BaseModel):
    point: str
    quote: str = ""


class SectionScore(BaseModel):
    """Evaluation information for one paper section.

    Mode a fills issue and fix. Mode b fills strength and weakness.
    All four are optional so either schema validates.
    """

    score: int = Field(ge=0, le=100)
    issue: str | None = None
    fix: str | None = None
    strength: str | None = None
    weakness: str | None = None
    title: str | None = None  # the heading as the paper prints it, when it differs from the section's name
    role: str | None = None  # introduction | methods | results | discussion | thematic | other
    checks: list[SectionCheck] = Field(default_factory=list)
    # The section review (a model call of its own, every quote checked against the paper)
    summary: str | None = None
    strengths: list[SectionStrength] = Field(default_factory=list)
    findings: list[SectionFinding] = Field(default_factory=list)
    # When the section was reviewed, `score` is computed in code from the findings and the checklist; the model's
    # own number is kept here, and `score_basis` lists each step ("Start 85", "−10 major: ...").
    # What to change, most important first: the section review's findings and the checklist's problems, each
    # with its quotation and a concrete action. Built by code from verified material, with no model call.
    fix_plan: list[FixStep] = Field(default_factory=list)
    llm_score: int | None = None
    score_basis: list[str] = Field(default_factory=list)
    source: str | None = None  # "detected" (a heading in the file) or "derived" (found from the content)
    anchor: str | None = None  # exact words from the paper where a derived section begins
    # The part of the paper the section covers, cut by code from where the section starts to where the next one starts.
    content: str | None = None
    content_truncated: bool = False
    word_count: int | None = None
    start_pct: int | None = None  # where the section starts, as a percentage of the paper's body text
    end_pct: int | None = None
    # The same text prepared for the Calibrate and Heatmap tools: captions left out, cut to a length they accept.
    tool_text: str | None = None
    tool_notes: list[str] = Field(default_factory=list)

    @field_validator("score", mode="before")
    @classmethod
    def clamp_score(cls, value: object) -> int:
        """Keep a bad model score inside the range the app draws."""
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return 0
        try:
            number = int(value)
        except ValueError:
            return 0
        return max(0, min(100, number))


class RejectionReason(BaseModel):
    """One concrete reason the manuscript may be rejected."""

    reason: str = "Unspecified issue"
    severity: Severity = Severity.major
    fix: str = ""
    # The four parts of a reason. `reason` repeats the problem and `fix` the solution, for older clients.
    problem: str = ""
    evidence: str = ""  # words from the manuscript this reason is about; checked against the paper
    location: str = ""  # section and nearby figure of the evidence, worked out by code
    impact_area: str = ""  # novelty | scientific_validity | publication_potential
    impact: str = ""
    solution: str = ""
    # Where the evidence sits, for "Fix with Calibrator": the report's section name (a key of
    # AnalysisResult.sections, or "" when the evidence is outside them) and the paragraphs around it.
    section: str = ""
    tool_text: str | None = None
    tool_notes: list[str] = Field(default_factory=list)
    # Who raised it: the model, or code from the publication record or the consistency check.
    origin: Literal["model", "publication_record", "consistency"] = "model"
    criterion: str = ""  # the scored criterion this concern bears on (a key of AnalysisResult.criteria)
    unverified_reason: str = ""  # for an unverified concern: why it could not be tied to the paper

    @field_validator("severity", mode="before")
    @classmethod
    def coerce_severity(cls, value: object) -> str:
        """Fall back to major when the model invents a severity."""
        if isinstance(value, str) and value in {item.value for item in Severity}:
            return value
        return Severity.major.value


class CriterionScore(BaseModel):
    """One scored criterion. The overall score is a weighted mean of these."""

    score: int = Field(ge=0, le=100)
    reason: str = ""
    weight: float = 0.0
    computed: bool = False  # True when the code computed it (citation integrity), not the model
    # Citation integrity only: share of references whose status could be settled (verified or ghost).
    coverage: float | None = None
    raw_score: int | None = None  # the score before code applied its limits
    adjustments: list[str] = Field(default_factory=list)  # the limits code applied, in words


class PreviousIssueStatus(BaseModel):
    """The model's judgement of one issue raised on the previous version of a revised paper."""

    id: int
    status: Literal["resolved", "partly_resolved", "open"]
    note: str = ""


class AnalysisResult(BaseModel):
    """Overall paper analysis generated by the backend pipeline."""

    mood: Mood = Mood.neutral
    mood_score: int = Field(ge=0, le=100)
    verdict: str
    oracle_letter: str
    sections: dict[str, SectionScore]
    rejection_reasons: list[RejectionReason]
    novelty_assessment: str
    journal_fit: str
    provider_used: str
    paper_format: str | None = None  # article type the analysis used
    field: str | None = None  # the paper's scientific field, passed on to the Calibrate and Heatmap tools
    criteria: dict[str, CriterionScore] = Field(default_factory=dict)
    not_assessed: list[str] = Field(default_factory=list)  # things the file did not let us judge
    grounding_notes: list[str] = Field(default_factory=list)  # model claims that were removed or flagged
    previous_issues: list[PreviousIssueStatus] = Field(default_factory=list)  # revisions only
    # Concerns the model raised that could not be tied to words in the paper. Shown apart, never scored.
    unverified_concerns: list[RejectionReason] = Field(default_factory=list)
    # Inputs of the overall score kept so code can recompute it after a later check (see analysis_guard.score_report).
    score_penalty: int = 0  # points off for what the file did not show (Methods, Supporting Information)
    raw_overall: int | None = None  # the model's own overall estimate, used only when it gave no criteria

    @field_validator("mood", mode="before")
    @classmethod
    def coerce_mood(cls, value: object) -> str:
        """Fall back to neutral when the model returns an unknown mood."""
        if isinstance(value, str) and value in {item.value for item in Mood}:
            return value
        return Mood.neutral.value

    @field_validator("mood_score", mode="before")
    @classmethod
    def clamp_mood_score(cls, value: object) -> int:
        """Keep the score ring inside 0–100."""
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return 0
        try:
            number = int(value)
        except ValueError:
            return 0
        return max(0, min(100, number))


class PaperMeta(BaseModel):
    """Timing and document stats shown under the report."""

    word_count: int = Field(ge=0)
    sections_found: list[str]
    references_found: int = Field(ge=0)
    mode: str
    processing_ms: int = Field(ge=0)
    article_type: str | None = None
    structure_mode: str | None = None  # "detected" headings or "derived" from the content
    cached: bool = False  # the same file was analysed before with the same settings: this is that result
    analysed_at: str | None = None  # when this result was produced (ISO 8601, UTC)


class ScoreChange(BaseModel):
    name: str
    before: int | None = None  # None: not scored in the earlier version
    after: int | None = None  # None: not scored in this version


class PreviousIssue(BaseModel):
    problem: str
    severity: str
    status: str  # resolved | partly_resolved | open | not_assessed
    note: str = ""


class RevisionComparison(BaseModel):
    """This upload next to the earlier version of the same paper that the account analysed."""

    previous_analysis_id: str
    previous_analysed_at: str
    previous_filename: str
    previous_journal: str
    same_file: bool  # the very same file again: any difference is the analysis varying, not the paper
    same_journal: bool
    similarity: float  # share of text the two versions have in common (0-1, estimated)
    score_before: int
    score_after: int
    criteria: list[ScoreChange] = Field(default_factory=list)
    sections: list[ScoreChange] = Field(default_factory=list)
    issues: list[PreviousIssue] = Field(default_factory=list)


class AnalysisPaperResponse(BaseModel):
    """Top-level response object for paper analysis API results."""

    analysis: AnalysisResult
    citations: CitationSummary
    journal: JournalMatch
    meta: PaperMeta
    document: "DocumentInfo | None" = None  # the editable copy, when the user asked to keep one
    document_note: str | None = None  # why no copy was kept, when one was asked for
    revision: RevisionComparison | None = None  # set when this paper is a version of one analysed before
    integrity: PublicationStatus | None = None  # published / retracted / under concern, checked before scoring
    consistency: ConsistencyReport | None = None  # the paper's own numbers, compounds and cell lines compared
    disclosures: Disclosures | None = None  # competing interests and funding, as the paper states them


class AnalyseRequest(BaseModel):
    """Request body for the analysis endpoint."""

    abstract: str
    sections: dict[str, str]
    target_journal: str
    citation_summary: str
    journal_data: dict[str, Any] = Field(default_factory=dict)
    mode: str = "a"


class VoiceCalibrateRequest(BaseModel):
    """Request body for voice calibration."""

    text: str = Field(..., min_length=15, max_length=20000, description="Academic text to calibrate.")
    journal: str = Field(..., min_length=2, max_length=200, description="Target journal name.")
    field: str = Field(default="Computer Science", description="Scientific domain or field.")
    intensity: str = Field(default="rewrite", description="Calibration intensity: polish, rewrite, restructure.")
    focus: str | None = Field(
        default=None,
        max_length=MAX_FOCUS_CHARS,
        description="Editor's note on this section: the issues the review found in it and how to fix each. Present when the text is a manuscript section.",
    )


class VoiceCalibrateResponse(BaseModel):
    """Response body for voice calibration."""

    calibrated_text: str | None = None
    original_text: str
    status: str = Field(default="verified", description="'verified', 'blocked', 'guidance', or 'no_invariants'")
    input_type: str = Field(default="manuscript_draft", description="'manuscript_draft' or 'reviewer_critique'")
    invariants_verified: bool
    total_detected_invariants: int = 0
    total_restored_invariants: int = 0
    word_count_original: int
    word_count_calibrated: int
    synthetic_markers_removed: int
    intensity_applied: str
    audit_notes: list[str] = Field(default_factory=list)
    block_reasons: list[str] = Field(default_factory=list)
    guidance_items: list[str] = Field(default_factory=list)


class DocumentSection(BaseModel):
    """A section of the working copy that can be calibrated and written back."""

    id: str  # "s1", "s2"... in the order of the analysis
    name: str
    mapped: bool  # False when the section could not be found in the working copy
    paragraphs: int = 0


class DocumentInfo(BaseModel):
    """An editable Word copy of the uploaded paper, kept for 24 hours."""

    id: str
    filename: str
    source_format: str  # 'pdf' (rebuilt as one column) or 'docx' (the uploaded file itself)
    version: int
    can_undo: bool
    expires_at: str
    sections: list[DocumentSection]
    notes: list[str] = Field(default_factory=list)


class DocumentCalibrateRequest(BaseModel):
    """Calibrate one section of a working copy. The server takes the text from the copy itself."""

    journal: str = Field(..., min_length=2, max_length=200)
    field: str = Field(default="Multidisciplinary", min_length=2, max_length=100)
    intensity: Literal["polish", "rewrite", "restructure"] = "rewrite"
    focus: str | None = Field(default=None, max_length=MAX_FOCUS_CHARS, description="The review's issues for this section and how to fix each.")
    from_paragraph: int = Field(default=0, ge=0, le=10000, description="Skip this many prose paragraphs of the section (for long sections).")
    evidence: str | None = Field(
        default=None,
        max_length=600,
        description="Words of the paragraph to start from (a rejection reason's evidence). Used when from_paragraph is 0.",
    )


class DocumentProposal(BaseModel):
    """Calibrated text for a section, waiting for the user to apply it to the paper or not."""

    proposal_id: str | None  # None when the calibration was blocked: there is nothing to apply
    section_id: str
    base_version: int
    original_text: str
    proposed_text: str | None
    warnings: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)  # what was left as it is (captions, figures) or left for later
    next_from: int | None = None  # pass as from_paragraph to calibrate the rest of a long section
    calibration: VoiceCalibrateResponse


class ApplyResult(BaseModel):
    """The paper after a proposal was applied."""

    document: DocumentInfo
    section_id: str
    applied_paragraphs: int
    section_text: str
    next_from: int | None = None


class HeatmapAnalyzeRequest(BaseModel):
    """Request body for AI signal heatmap stylometric analysis."""

    text: str = Field(..., min_length=20, max_length=50000, description="Manuscript excerpt to analyze.")
    journal: str = Field(default="", max_length=200, description="Target journal name.")
    field: str = Field(default="", max_length=100, description="Discipline or domain (decides whether passive voice is the norm).")


class AbstractForgeRequest(BaseModel):
    """Request body for synthesizing 3 architectural abstract variants."""

    abstract_draft: str = Field(..., min_length=20, max_length=15000, description="Current abstract or draft summary.")
    methodology_context: str | None = Field(default=None, description="Optional parsed methodology and findings context.")
    journal: str | None = Field(default=None, max_length=200, description="Target journal venue.")
    field: str | None = Field(default=None, max_length=100, description="Discipline or research domain.")
    max_words: int | None = Field(default=None, ge=30, le=600, description="Word limit of the journal's abstract.")


class AbstractForgeResponse(BaseModel):
    """Response body for Abstract Forge containing 3 distinct architectures."""

    source_word_count: int
    target_journal: str
    fact_inventory: dict[str, Any] = Field(default_factory=dict)
    variants: list[Any] = Field(description="List of synthesized AbstractVariantDetail objects")
    comparative_summary: str
    invariants_preserved_count: int


class RebuttalAnalyzeRequest(BaseModel):
    """Request body for peer review rebuttal analysis."""

    reviewer_report_text: str = Field(..., min_length=20, max_length=25000, description="Raw reviewer comments or editorial critique text.")
    manuscript_context: str | None = Field(default=None, description="Optional key findings, methods, data the author can cite.")
    journal: str | None = Field(default=None, description="Target journal name. Optional; no default is assumed.")
    author_tone: Literal["collaborative", "diplomatic_firm", "formal_rigorous"] = Field(
        default="collaborative", description="Preferred diplomatic stance tone."
    )


class RebuttalAnalyzeResponse(BaseModel):
    """Response body for peer review rebuttal analysis."""

    total_comments_identified: int
    reviewer_blocks: list[Any] = Field(description="Per-reviewer summaries and breakdowns")
    category_breakdown: dict[str, int] = Field(description="Count of comments by diplomatic posture")
    overall_negotiation_strategy: str
    comments: list[Any] = Field(description="Discrete RebuttalCommentItem objects")
    manuscript_revision_checklist: list[str] = Field(default_factory=list)
    export_document_md: str


# DocumentInfo is defined after AnalysisPaperResponse, which refers to it; FileMark after ParsedDocument.
AnalysisPaperResponse.model_rebuild()
ParsedDocument.model_rebuild()
