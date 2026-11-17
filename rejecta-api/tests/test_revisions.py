"""Recognising a revised paper, comparing it with the earlier version, and returning the same report for the same file."""

from __future__ import annotations

import asyncio
import json
import random
import uuid
from types import SimpleNamespace
from typing import Any

from models import AnalysisPaperResponse, AnalysisResult, CitationSummary, JournalMatch, PaperMeta
from services import analysis_cache, revisions
from services.documents.store import Owner
from services.llm import base
from services.llm.openai import ANALYSIS_SEED, OpenAIProvider
from tests.test_analysis_guard import make_ctx, payload, run

ME = Owner("user", uuid.UUID("11111111-1111-1111-1111-111111111111"))
SOMEONE_ELSE = Owner("user", uuid.UUID("22222222-2222-2222-2222-222222222222"))

_WORDS = (
    "polymer sulfur thioester ring opening catalyst monomer chain yield molar mass dispersity thermal stability "
    "degradation recycling solvent temperature reaction kinetics spectroscopy refractive index carbonate imidazole "
    "dithiol base step growth copolymer film tensile modulus hydrolysis enzyme lipase bacteria synthesis route"
).split()


def paper(seed: int, words: int = 3000) -> str:
    rng = random.Random(seed)
    return " ".join(rng.choice(_WORDS) for _ in range(words))


def revise(text: str, share: float, seed: int = 99) -> str:
    """Rewrite about `share` of the sentences, as a revision after peer review would."""
    rng = random.Random(seed)
    words = text.split()
    chunks = [words[i : i + 20] for i in range(0, len(words), 20)]
    out = []
    for chunk in chunks:
        out.extend(paper(rng.randint(0, 10**6), len(chunk)).split() if rng.random() < share else chunk)
    return " ".join(out)


def run_async(coro):
    return asyncio.run(coro)


def record(text: str, sha: str = "a" * 64, summary: dict[str, Any] | None = None, journal: str = "Polymer Chemistry"):
    return revisions.NewRecord(
        filename="paper.pdf", journal=journal, article_type="communication", file_sha256=sha,
        signature=revisions.signature(text), summary=summary or {"score": 60, "criteria": {}, "sections": {}, "reasons": []},
    )


# ------------------------------------------------------------------ fingerprint


def test_a_revision_shares_much_of_its_text_and_another_paper_almost_none() -> None:
    original = paper(1)
    assert revisions.similarity(revisions.signature(original), revisions.signature(original)) == 1.0
    assert revisions.similarity(revisions.signature(original), revisions.signature(revise(original, 0.25))) > 0.45
    assert revisions.similarity(revisions.signature(original), revisions.signature(paper(2))) < 0.1


def test_the_fingerprint_is_small_and_has_no_words_of_the_paper() -> None:
    sig = revisions.signature(paper(1))
    assert len(sig) == revisions.SIGNATURE_SIZE and all(isinstance(h, int) and 0 <= h < 2**63 for h in sig)
    assert revisions.signature("too short") == []


# ------------------------------------------------------------------ finding the earlier version


def test_the_earlier_version_of_the_same_paper_is_found_and_another_paper_is_not() -> None:
    store = revisions.MemoryHistoryStore()
    original = paper(1)
    run_async(store.save(ME, record(paper(2), sha="b" * 64)))
    run_async(store.save(ME, record(original, sha="a" * 64)))
    found = run_async(revisions.find_previous(store, ME, revisions.signature(revise(original, 0.25)), "c" * 64))
    assert found is not None and found.file_sha256 == "a" * 64 and not found.same_file
    assert run_async(revisions.find_previous(store, ME, revisions.signature(paper(3)), "d" * 64)) is None


def test_the_same_file_is_recognised_as_such() -> None:
    store = revisions.MemoryHistoryStore()
    run_async(store.save(ME, record(paper(1), sha="a" * 64)))
    found = run_async(revisions.find_previous(store, ME, revisions.signature(paper(1)), "a" * 64))
    assert found is not None and found.same_file and found.similarity == 1.0


def test_another_account_never_sees_my_history() -> None:
    store = revisions.MemoryHistoryStore()
    run_async(store.save(ME, record(paper(1))))
    assert run_async(revisions.find_previous(store, SOMEONE_ELSE, revisions.signature(paper(1)), "a" * 64)) is None


# ------------------------------------------------------------------ the prompt and the model's answer


EARLIER_ISSUES = [
    {"id": 1, "severity": "major", "problem": "No comparison of the step-growth route with ring-opening routes."},
    {"id": 2, "severity": "minor", "problem": "The molar mass is reported without its dispersity."},
]


def test_a_revision_puts_the_earlier_issues_to_the_model() -> None:
    from services.analysis_guard import paper_context

    class P(base.AbstractAnalysisProvider):
        async def analyse(self, *a, **k):  # pragma: no cover
            raise NotImplementedError

        async def generate(self, *a, **k):  # pragma: no cover
            raise NotImplementedError

    with paper_context(make_ctx(previous_issues=EARLIER_ISSUES)):
        message = P().build_user_message("abs", {}, "J", "c", {})
    assert "PREVIOUS VERSION" in message and "1. [major] No comparison of the step-growth route" in message
    with paper_context(make_ctx()):
        assert "PREVIOUS VERSION" not in P().build_user_message("abs", {}, "J", "c", {})


def test_the_models_verdict_on_earlier_issues_is_checked() -> None:
    raw = payload(previous_issues=[
        {"id": 1, "status": "Resolved", "note": "Section 3 now compares both routes."},
        {"id": 1, "status": "open", "note": "duplicate"},
        {"id": 7, "status": "resolved", "note": "no such issue"},
        {"id": 2, "status": "fixed", "note": "not a status"},
    ])
    out = run(raw, make_ctx(previous_issues=EARLIER_ISSUES))
    assert out["previous_issues"] == [{"id": 1, "status": "resolved", "note": "Section 3 now compares both routes."}]
    assert run(raw, make_ctx())["previous_issues"] == []  # not a revision: ignored


# ------------------------------------------------------------------ comparing


def analysis(score: int, sections: dict[str, int], statuses: list[dict[str, Any]]) -> AnalysisResult:
    return AnalysisResult.model_validate({
        "mood_score": score, "verdict": "v", "oracle_letter": "o", "novelty_assessment": "n", "journal_fit": "j",
        "provider_used": "test", "rejection_reasons": [],
        "sections": {name: {"score": s} for name, s in sections.items()},
        "criteria": {"rigor": {"score": score, "weight": 25}},
        "previous_issues": statuses,
    })


def test_the_comparison_shows_scores_before_and_after_and_what_became_of_each_issue() -> None:
    previous = revisions.PreviousAnalysis(
        uuid.uuid4(), revisions._now(), "v1.pdf", "Polymer Chemistry", "communication", "a" * 64, [],
        {"score": 62, "criteria": {"rigor": 60}, "sections": {"Introduction": 70, "Experimental": 55},
         "reasons": [{"severity": i["severity"], "problem": i["problem"]} for i in EARLIER_ISSUES]},
        similarity=0.7,
    )
    now = analysis(78, {"Introduction": 75, "Results and discussion": 80}, [{"id": 1, "status": "resolved", "note": "done"}])
    comparison = revisions.compare(previous, now, "polymer chemistry")
    assert (comparison.score_before, comparison.score_after, comparison.same_journal) == (62, 78, True)
    sections = {c.name: (c.before, c.after) for c in comparison.sections}
    assert sections == {"Introduction": (70, 75), "Results and discussion": (None, 80), "Experimental": (55, None)}
    assert [(i.status, i.note) for i in comparison.issues] == [("resolved", "done"), ("not_assessed", "")]
    assert previous.issues()[0]["id"] == 1


def _previous(journal: str, same_file: bool = False) -> revisions.PreviousAnalysis:
    return revisions.PreviousAnalysis(
        uuid.uuid4(), revisions._now(), "v1.pdf", journal, "research_article", "a" * 64, [],
        {"score": 82, "criteria": {}, "sections": {},
         "reasons": [{"severity": i["severity"], "problem": i["problem"]} for i in EARLIER_ISSUES]},
        similarity=1.0, same_file=same_file,
    )


def test_a_long_journal_text_is_the_same_journal_as_its_stored_copy() -> None:
    long_journal = "Journal of Chemical Information and Modeling (JCIM) " + "Journal of Cheminformatics (BMC) " * 8
    assert len(long_journal) > revisions.MAX_JOURNAL_CHARS
    stored = long_journal.strip()[: revisions.MAX_JOURNAL_CHARS]  # what the database keeps
    now = analysis(80, {}, [])
    assert revisions.compare(_previous(stored), now, long_journal).same_journal is True
    assert revisions.compare(_previous(stored), now, "Nature").same_journal is False


def test_the_same_file_again_lists_no_issues_and_the_model_is_not_asked_about_them() -> None:
    now = analysis(80, {}, [])
    assert revisions.compare(_previous("J", same_file=True), now, "J").issues == []
    assert len(revisions.compare(_previous("J"), now, "J").issues) == len(EARLIER_ISSUES)


def test_the_kept_summary_has_scores_and_issues_but_no_text_of_the_paper() -> None:
    result = AnalysisResult.model_validate({
        "mood_score": 70, "verdict": "v", "oracle_letter": "o", "novelty_assessment": "n", "journal_fit": "j",
        "provider_used": "t", "criteria": {},
        "sections": {"Introduction": {"score": 70, "content": "SECRET PAPER TEXT"}},
        "rejection_reasons": [{"problem": "P", "severity": "major", "evidence": "SECRET QUOTE"}],
    })
    summary = json.dumps(revisions.summarize(result))
    assert "SECRET" not in summary and '"Introduction": 70' in summary


# ------------------------------------------------------------------ the same file again


def response(score: int) -> AnalysisPaperResponse:
    return AnalysisPaperResponse(
        analysis=analysis(score, {"Introduction": score}, []),
        citations=CitationSummary(verified=0, ghost=0, unverified=0, total=0, items=[]),
        journal=JournalMatch(found=False, target="J"),
        meta=PaperMeta(word_count=1, sections_found=[], references_found=0, mode="a", processing_ms=1),
    )


def test_the_same_file_and_settings_get_the_stored_report_and_anything_else_does_not() -> None:
    analysis_cache.clear()
    key = analysis_cache.cache_key(ME, "a" * 64, "Polymer Chemistry", "a", None, "openai:gpt-4o-2024-08-06")
    analysis_cache.put(key, response(71))
    assert analysis_cache.get(key).analysis.mood_score == 71
    assert analysis_cache.cache_key(ME, "a" * 64, " polymer chemistry ", "a", None, "openai:gpt-4o-2024-08-06") == key
    for other in (
        analysis_cache.cache_key(SOMEONE_ELSE, "a" * 64, "Polymer Chemistry", "a", None, "openai:gpt-4o-2024-08-06"),
        analysis_cache.cache_key(ME, "b" * 64, "Polymer Chemistry", "a", None, "openai:gpt-4o-2024-08-06"),
        analysis_cache.cache_key(ME, "a" * 64, "Nature", "a", None, "openai:gpt-4o-2024-08-06"),
        analysis_cache.cache_key(ME, "a" * 64, "Polymer Chemistry", "a", None, "anthropic:claude"),
    ):
        assert analysis_cache.get(other) is None
    analysis_cache.clear()


def test_the_analysis_call_uses_a_fixed_seed() -> None:
    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider.model = "gpt-4o-2024-08-06"
    calls: list[dict[str, Any]] = []

    async def create(**kwargs: Any) -> Any:
        calls.append(kwargs)
        choice = SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload())), finish_reason="stop")
        return SimpleNamespace(choices=[choice], model="gpt-4o-2024-08-06")

    provider.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    asyncio.run(provider.analyse("abs", {}, "J", "c", {}))
    assert calls[0]["seed"] == ANALYSIS_SEED and calls[0]["temperature"] == 0
