"""FastAPI application entry point for Rejecta API."""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from pydantic import BaseModel
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from auth import features
from auth.errors import ApiError, api_error_handler
from auth.subject import get_subject, public_rate_limit
from auth.tokens import TokenConfigError, get_keyring
from auth.usage import Subject, consume, refund_stale_reservations
from config import settings
from db.pool import check_database, close_pool, init_pool
from routers import admin_insights, admin_partners, admin_plans, admin_users, oauth
from routers import auth as auth_routes
from routers import auth_mfa as auth_mfa_routes
from routers import me as me_routes
from routers import wellknown as wellknown_routes
from models import (
    AnalyseRequest,
    AnalysisPaperResponse,
    AnalysisResult,
    CitationResult,
    CitationStatus,
    AbstractForgeRequest,
    AbstractForgeResponse,
    RebuttalAnalyzeRequest,
    RebuttalAnalyzeResponse,
    CitationSummary,
    HeatmapAnalyzeRequest,
    JournalMatch,
    PaperMeta,
    ParsedDocument,
    VoiceCalibrateRequest,
    VoiceCalibrateResponse,
)
from services import citations, journals, llm, parser
from services.calibrator import VoiceCalibratorService
from services.forge import AbstractForgeEngine, AbstractForgeResult
from services.heatmap import HeatmapAnalysisResult, HeatmapEngine
from services.citations import MAX_REFERENCES, build_citation_summary, verify_citations
from services.journals import match_journal
from services.llm import get_provider_cached
from services.parser import MAX_FILE_SIZE_BYTES, parse_pdf

APP_TITLE = "Rejecta API"
APP_VERSION = "0.1.0"
HEALTH_OK = "ok"
HOST = "0.0.0.0"
PORT = 8000

logger = logging.getLogger("rejecta.api")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Check config, open the database pool, tidy up, and close on shutdown."""
    problems = settings.production_problems()
    if problems:
        raise RuntimeError("Unsafe production configuration: " + "; ".join(problems))
    await init_pool()
    try:
        get_keyring()
    except TokenConfigError as exc:
        logger.warning("Auth disabled until signing keys are configured: %s", exc)
    if settings.DATABASE_URL.strip():
        refunded = await refund_stale_reservations()
        if refunded:
            logger.info("Refunded %s usage reservations left open by a previous run", refunded)
    try:
        yield
    finally:
        await close_pool()


app = FastAPI(
    title=APP_TITLE,
    version=APP_VERSION,
    lifespan=lifespan,
    # Interactive docs and the schema are off in production (DOCS_ENABLED=false).
    docs_url="/docs" if settings.DOCS_ENABLED else None,
    redoc_url=None,
    openapi_url="/openapi.json" if settings.DOCS_ENABLED else None,
    description=(
        "API for the Rejecta app. Locally that is http://localhost:5173. "
        "In production it is https://app.rejecta.ai. "
        "The marketing site at https://rejecta.ai does not call this API."
    ),
)

# Ensure service modules are imported and available for wiring.
_ = (parser, citations, journals, llm)

app.add_exception_handler(ApiError, api_error_handler)
app.include_router(auth_routes.router)
app.include_router(auth_mfa_routes.router)
app.include_router(me_routes.router)
app.include_router(wellknown_routes.router)
app.include_router(admin_users.router)
app.include_router(admin_plans.router)
app.include_router(admin_insights.router)
app.include_router(admin_partners.router)
app.include_router(oauth.router)

# Only the Rejecta app may call the API from a browser. Auth uses a Bearer
# header (not cookies) on direct calls, so credentials stay off; the refresh
# cookie only travels same-origin through the Netlify proxy (/api/auth/*).
# Partner platforms call server-to-server, where CORS does not apply.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins(),
    allow_origin_regex=settings.CORS_ORIGIN_REGEX or None,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Rejecta-Client", "X-Request-Id"],
    expose_headers=["Retry-After", "X-Request-Id"],
    max_age=600,
)


@app.middleware("http")
async def request_logging_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Request id, security headers, and one log line per request (never tokens)."""
    start_time = time.perf_counter()
    incoming = request.headers.get("x-request-id", "")
    request_id = incoming if 8 <= len(incoming) <= 64 and incoming.replace("-", "").isalnum() else uuid.uuid4().hex
    response = await call_next(request)
    duration_ms = (time.perf_counter() - start_time) * 1000

    headers = response.headers
    headers["X-Request-Id"] = request_id
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("Referrer-Policy", "no-referrer")
    if settings.is_production():
        headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    path = request.url.path
    if path.startswith(("/auth", "/admin", "/me", "/oauth")):
        headers.setdefault("Cache-Control", "no-store")

    logger.info(
        "request id=%s method=%s path=%s status_code=%s duration_ms=%.2f",
        request_id,
        request.method,
        path,
        response.status_code,
        duration_ms,
    )
    return response


@app.exception_handler(Exception)
async def global_exception_handler(_: Request, exc: Exception) -> JSONResponse:
    """Return a normalized 500 payload for unhandled exceptions."""
    logger.exception("Unhandled server exception: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Something went wrong. Please try again."},
    )


@app.get("/health")
async def health() -> dict[str, str]:
    """Health probe endpoint for runtime checks."""
    return {
        "status": HEALTH_OK,
        "environment": settings.ENVIRONMENT,
        "llm_provider": settings.LLM_PROVIDER,
        "database": await check_database(),
    }


@app.post("/analyse", response_model=AnalysisResult)
async def analyse(payload: AnalyseRequest, subject: Subject = Depends(get_subject)) -> AnalysisResult:
    """Run paper analysis using the configured provider. Login + quota (analyse_paper)."""
    async with consume(subject, features.ANALYSE_PAPER):
        provider = get_provider_cached()
        return await provider.analyse(
            abstract=payload.abstract,
            sections=payload.sections,
            target_journal=payload.target_journal,
            citation_summary=payload.citation_summary,
            journal_data=payload.journal_data,
            mode=payload.mode,
        )


@app.post("/parse", response_model=ParsedDocument, dependencies=[Depends(public_rate_limit)])
async def parse_endpoint(file: UploadFile = File(...)) -> ParsedDocument:
    """Parse an uploaded PDF and return structured content."""
    filename = file.filename or ""
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    content = await file.read()
    if len(content) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=400,
            detail="File too large — maximum size is 10MB",
        )

    try:
        return parse_pdf(content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="PDF parsing failed unexpectedly") from exc


class CitationsRequest(BaseModel):
    """Request body for citation verification."""

    references: list[str]


class JournalMatchRequest(BaseModel):
    """Request body for journal matching."""

    abstract: str
    target_journal: str


@app.post("/citations", response_model=CitationSummary, dependencies=[Depends(public_rate_limit)])
async def citations_endpoint(body: CitationsRequest) -> CitationSummary:
    """Verify a list of reference strings against CrossRef."""
    if not body.references:
        raise HTTPException(status_code=400, detail="No references provided")
    if len(body.references) > 60:
        raise HTTPException(status_code=400, detail="Maximum 60 references per request")

    results = await verify_citations(body.references)
    return build_citation_summary(results)


@app.post("/journal-match", response_model=JournalMatch, dependencies=[Depends(public_rate_limit)])
async def journal_match_endpoint(body: JournalMatchRequest) -> JournalMatch:
    """Find a journal in OpenAlex and return its metadata."""
    if not body.target_journal.strip():
        raise HTTPException(status_code=400, detail="Journal name is required")
    if not body.abstract.strip():
        raise HTTPException(status_code=400, detail="Abstract is required")
    return await match_journal(body.abstract, body.target_journal)


@app.post("/analyse-paper", response_model=AnalysisPaperResponse)
async def analyse_paper(
    file: UploadFile = File(...),
    journal: str = Form(...),
    mode: str = Form(default="a"),
    email: str = Form(default=""),
    subject: Subject = Depends(get_subject),
) -> AnalysisPaperResponse:
    """Full pipeline: PDF → parse → citations + journal → Claude.

    The only endpoint the frontend calls. Chains all four
    services internally. Citations and journal match run
    concurrently for speed.

    Args:
        file: PDF file upload (multipart form)
        journal: Target journal name e.g. "Nature Medicine"
        mode: "a" = author reviewing own paper,
              "b" = reader analysing someone else's paper
        email: Optional report address from the app form.
               Accepted so the app and API share one contract.
               Not stored or sent yet.

    Returns:
        AnalysisPaperResponse with analysis, citations,
        journal match, and document metadata.
    """
    filename = file.filename or ""
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="PDF files only")

    content = await file.read()
    if len(content) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(status_code=400, detail="File too large — maximum 10MB")

    if not journal.strip():
        raise HTTPException(status_code=400, detail="Journal name is required")

    if mode not in ("a", "b"):
        raise HTTPException(status_code=400, detail="Mode must be 'a' or 'b'")

    report_email = _optional_email(email)

    # Quota is reserved only after cheap validation, and refunded if any step fails.
    async with consume(subject, features.ANALYSE_PAPER):
        start = time.perf_counter()
        try:
            parsed = parse_pdf(content)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.error("PDF parse failed: %s", exc, exc_info=True)
            raise HTTPException(status_code=500, detail="PDF parsing failed unexpectedly") from exc

        citation_outcome, journal_outcome = await asyncio.gather(
            verify_citations(parsed.references),
            match_journal(parsed.abstract, journal.strip()),
            return_exceptions=True,
        )

        if isinstance(citation_outcome, Exception):
            logger.warning("Citation lookup failed; marking all unverified", exc_info=citation_outcome)
            citation_results = [
                CitationResult(raw=ref, status=CitationStatus.unverified)
                for ref in parsed.references[:MAX_REFERENCES]
            ]
        else:
            citation_results = citation_outcome

        if isinstance(journal_outcome, Exception):
            logger.warning("Journal lookup failed; continuing without a match", exc_info=journal_outcome)
            journal_data = JournalMatch(found=False, target=journal.strip())
        else:
            journal_data = journal_outcome

        citation_summary = build_citation_summary(citation_results)
        # Facts only. Editorial warnings stay in the API response for the UI; feeding
        # them to the model skewed scores (e.g. 75 -> 38 on a landmark paper).
        citation_summary_str = (
            f"{citation_summary.verified} verified, "
            f"{citation_summary.ghost} ghost (DOI or arXiv ID does not exist in CrossRef/arXiv; "
            f"possible fabricated reference), "
            f"{citation_summary.unverified} unverified (lookup inconclusive) "
            f"out of {citation_summary.total} total references."
        )

        provider = get_provider_cached()
        try:
            analysis = await provider.analyse(
                abstract=parsed.abstract,
                sections=parsed.sections,
                target_journal=journal.strip(),
                citation_summary=citation_summary_str,
                journal_data=journal_data.model_dump(),
                mode=mode,
            )
        except Exception as exc:
            logger.error("LLM analysis failed: %s", exc, exc_info=True)
            raise HTTPException(status_code=500, detail="Analysis failed — please try again") from exc

        elapsed = round((time.perf_counter() - start) * 1000)
        logger.info(
            "analyse-paper complete in %sms | journal=%s mode=%s words=%s refs=%s provider=%s email_provided=%s",
            elapsed,
            journal,
            mode,
            parsed.word_count,
            len(parsed.references),
            analysis.provider_used,
            report_email is not None,
        )

        return AnalysisPaperResponse(
            analysis=analysis,
            citations=citation_summary,
            journal=journal_data,
            meta=PaperMeta(
                word_count=parsed.word_count,
                sections_found=list(parsed.sections.keys()),
                references_found=len(parsed.references),
                mode=mode,
                processing_ms=elapsed,
            ),
        )



@app.post("/voice-calibrate", response_model=VoiceCalibrateResponse)
async def voice_calibrate_endpoint(
    req: VoiceCalibrateRequest, subject: Subject = Depends(get_subject)
) -> VoiceCalibrateResponse:
    """Calibrate academic draft to target journal register. Login + quota (voice_calibrate)."""
    async with consume(subject, features.VOICE_CALIBRATE):
        provider = get_provider_cached()
        calibrator = VoiceCalibratorService(llm_service=provider)
        result = await calibrator.calibrate(
            text=req.text,
            journal_name=req.journal,
            field=req.field,
            intensity=req.intensity,  # type: ignore[arg-type]
        )
    return VoiceCalibrateResponse(
        calibrated_text=result.calibrated_text,
        original_text=result.original_text,
        status=result.status,
        input_type=result.input_type,
        invariants_verified=result.invariants_verified,
        total_detected_invariants=result.total_detected_invariants,
        total_restored_invariants=result.total_restored_invariants,
        word_count_original=result.word_count_original,
        word_count_calibrated=result.word_count_calibrated,
        synthetic_markers_removed=result.synthetic_markers_removed,
        intensity_applied=result.intensity_applied,
        audit_notes=result.audit_notes,
        block_reasons=result.block_reasons,
        guidance_items=result.guidance_items,
    )


@app.post("/ai-signal-heatmap/analyze", response_model=HeatmapAnalysisResult)
async def heatmap_analyze_endpoint(
    req: HeatmapAnalyzeRequest, subject: Subject = Depends(get_subject)
) -> HeatmapAnalysisResult:
    """Deterministic stylometric heatmap with UTF-16 offsets. Login + quota (ai_heatmap)."""
    async with consume(subject, features.AI_HEATMAP):
        return HeatmapEngine.analyze(
            text=req.text,
            journal_name=req.journal,
            field=req.field,
        )


@app.post("/abstract-forge/generate", response_model=AbstractForgeResult)
async def abstract_forge_endpoint(
    req: AbstractForgeRequest, subject: Subject = Depends(get_subject)
) -> AbstractForgeResult:
    """Three abstract architectures with invariant protection. Login + quota (abstract_forge)."""
    async with consume(subject, features.ABSTRACT_FORGE):
        provider = get_provider_cached()
        forge_engine = AbstractForgeEngine(llm_service=provider)
        return await forge_engine.forge(
            abstract_draft=req.abstract_draft,
            methodology_context=req.methodology_context,
            journal_name=req.journal,
            field=req.field,
        )


@app.post("/rebuttal-writer/analyze", response_model=RebuttalAnalyzeResponse)
async def rebuttal_writer_endpoint(
    req: RebuttalAnalyzeRequest, subject: Subject = Depends(get_subject)
) -> RebuttalAnalyzeResponse:
    """Point-by-point diplomatic rebuttal. Login + quota (rebuttal_writer)."""
    from services.rebuttal import RebuttalEngine

    async with consume(subject, features.REBUTTAL_WRITER):
        provider = get_provider_cached()
        rebuttal_engine = RebuttalEngine(llm_service=provider)
        return await rebuttal_engine.analyze(
            text=req.reviewer_report_text,
            manuscript_context=req.manuscript_context,
            journal=req.journal,
            author_tone=req.author_tone,
        )


def _optional_email(value: str) -> str | None:
    """Accept the app's email field without storing or sending it."""
    cleaned = value.strip()
    if not cleaned:
        return None
    local, separator, domain = cleaned.partition("@")
    if not separator or not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise HTTPException(status_code=400, detail="Enter a valid email")
    return cleaned


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=HOST, port=PORT, reload=True)
