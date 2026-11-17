"""Run the evidence-ledger section review on one real file and print everything it decided.

    python -m scripts.ledger_stage "paper.pdf" --journal "Nature Machine Intelligence"
    python -m scripts.ledger_stage "paper.pdf" --journal "..." --json out.json   # also save the full result

Uses the real model (OPENAI_API_KEY) and the local store (RAG_DATABASE_URL, or files under .cache/rag). A second
run of the same file is answered from the store and must print the same result.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config import settings  # noqa: E402
from services import section_review  # noqa: E402
from services.analysis_guard import AnalysisContext  # noqa: E402
from services.llm import get_provider_cached  # noqa: E402
from services.local_store import get_store  # noqa: E402
from services.parser import parse_document  # noqa: E402


def context_for(parsed) -> AnalysisContext:  # noqa: ANN001
    return AnalysisContext(
        source_text=parsed.full_text,
        article_type=parsed.article_type or "unknown",
        structure_mode="detected" if parsed.structure == "detected" else "derived",
        section_texts={k: v for k, v in parsed.sections.items() if k in parsed.section_roles},
        section_roles=parsed.section_roles,
        section_titles=dict(parsed.section_titles),
        abstract=parsed.abstract,
        body=parsed.body,
        numbers_text=f"{parsed.abstract} {parsed.body}",
    )


async def run(path: Path, journal: str, out: Path | None) -> None:
    settings.EVIDENCE_LEDGER_ENABLED = True
    parsed = parse_document(path.read_bytes(), path.name)
    ctx = context_for(parsed)
    inputs = section_review.inputs_from_parsed(ctx)
    provider = get_provider_cached()
    print(f"file: {path.name}\nsections: {[i.name for i in inputs]}\nstore: {get_store().name}")
    started = time.time()
    review = await section_review.review_sections(inputs, journal, provider, ctx)
    print(f"status: {review.status}  ({time.time() - started:.1f}s)  {review.note}")
    ledger = review.ledger or {}
    print(f"\nfield: {ledger.get('field')}  kind: {ledger.get('paper_kind')}  packs: {[p['name'] for p in ledger.get('packs', [])]}")
    print(f"retrieval: {ledger.get('retrieval')}  questions: {ledger.get('questions')}  answers: {ledger.get('answers')}")
    print(f"removed: {ledger.get('removed')}  ledger entries dropped: {ledger.get('ledger_entries_dropped')}")
    print("\nCLAIMS")
    for claim in ledger.get("claims", []):
        print(f"  {claim['id']}{' (main)' if claim['main'] else ''} [{claim['type']}] {claim['statement']}  <{', '.join(claim['evidence_ids'][:3])}>")
    for name, entry in review.sections.items():
        print(f"\n=== {name}")
        print(f"  summary: {entry.get('summary')}")
        for s in entry.get("strengths", []):
            print(f"  + {s['point']}  <{', '.join(s.get('evidence_ids', []))}>")
        for f in entry.get("findings", []):
            print(f"  - [{f['severity']}] ({f.get('check') or 'review'}) {f['problem']}")
            if f.get("claim"):
                print(f"      claim: {f['claim']}")
            print(f"      quote: {f['quote'][:220]!r}  <{', '.join(f.get('evidence_ids', []))}>")
            print(f"      impact: {f.get('impact')}")
            print(f"      fix: {f['fix']}")
    if out:
        out.write_text(json.dumps({"ledger": ledger, "sections": review.sections}, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nsaved {out}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("file", type=Path)
    parser.add_argument("--journal", default="")
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()
    asyncio.run(run(args.file, args.journal, args.json))


if __name__ == "__main__":
    main()
