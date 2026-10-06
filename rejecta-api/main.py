"""FastAPI application entry point for Rejecta API."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from pydantic import BaseModel
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from config import settings
from models import (
    AnalyseRequest,
    AnalysisPaperResponse,
    AnalysisResult,
    CitationResult,
    CitationStatus,
    CitationSummary,
    JournalMatch,
    PaperMeta,
    ParsedDocument,
)
from services import citations, journals, llm, parser
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

app = FastAPI(
    title=APP_TITLE,
    version=APP_VERSION,
    description=(
        "API for the Rejecta app. Locally that is http://localhost:5173. "
        "In production it is https://app.rejecta.ai. "
        "The marketing site at https://rejecta.ai does not call this API."
    ),
)

# Ensure service modules are imported and available for wiring.
_ = (parser, citations, journals, llm)

# Public unauthenticated API. Do not send cookies; a wildcard origin is valid
# and covers Netlify, scivalon.com, and preview URLs. allow_credentials=True
# would block Access-Control-Allow-Origin: * and surface as "Failed to fetch".
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_origin_regex=r"https://([a-z0-9-]+\.)*(netlify\.app|scivalon\.com|rejecta\.ai)",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_logging_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Log request method/path/status and request duration."""
    start_time = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - start_time) * 1000
    logger.info(
        "request method=%s path=%s status_code=%s duration_ms=%.2f",
        request.method,
        request.url.path,
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
    }


@app.post("/analyse", response_model=AnalysisResult)
async def analyse(payload: AnalyseRequest) -> AnalysisResult:
    """Run paper analysis using the configured provider."""
    provider = get_provider_cached()
    return await provider.analyse(
        abstract=payload.abstract,
        sections=payload.sections,
        target_journal=payload.target_journal,
        citation_summary=payload.citation_summary,
        journal_data=payload.journal_data,
        mode=payload.mode,
    )


@app.post("/parse", response_model=ParsedDocument)
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


@app.post("/citations", response_model=CitationSummary)
async def citations_endpoint(body: CitationsRequest) -> CitationSummary:
    """Verify a list of reference strings against CrossRef."""
    if not body.references:
        raise HTTPException(status_code=400, detail="No references provided")
    if len(body.references) > 60:
        raise HTTPException(status_code=400, detail="Maximum 60 references per request")

    results = await verify_citations(body.references)
    return build_citation_summary(results)


@app.post("/journal-match", response_model=JournalMatch)
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
    citation_summary_str = (
        f"{citation_summary.verified} verified, {citation_summary.ghost} ghost/missing "
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
