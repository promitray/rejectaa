"""Reference recall@k of the P1 retrieval on the frozen evaluation set.

    python -m services.literature.eval.run_recall                                  # text-embedding-3-small, hybrid order
    python -m services.literature.eval.run_recall --rerank                         # with the LLM rerank of the top 30
    python -m services.literature.eval.run_recall --model allenai/specter2         # needs torch/transformers/adapters
    python -m services.literature.eval.run_recall --limit 10 --json out.json

Route (a) is off (the paper's references are what is being looked for); routes (b) OpenAlex search and (c) the
local knowledge base are on, both before the paper's date. Every result passes the harness's leak filter.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from services.literature import DEFAULT_EMBEDDING_MODEL  # noqa: E402
from services.literature.candidates import Options, gather  # noqa: E402
from services.literature.embeddings import embed  # noqa: E402
from services.literature.eval import harness  # noqa: E402
from services.literature.manuscript import Manuscript  # noqa: E402
from services.literature.openalex import OpenAlexClient  # noqa: E402
from services.literature.retrieve import rank_pool  # noqa: E402
from services.literature.store import LiteratureStore  # noqa: E402
from services.literature.types import PaperDate, Work  # noqa: E402
from services.llm import get_provider_cached  # noqa: E402
from services.local_store import digest  # noqa: E402


async def run(model: str, use_rerank: bool, limit: int | None, k: int) -> harness.Report:
    version, papers = harness.load_eval_set()
    papers = papers[:limit] if limit else papers
    provider = get_provider_cached()
    store = LiteratureStore()
    client = OpenAlexClient()

    async def retriever(query: harness.EvalQuery, options: harness.RetrievalOptions) -> list[Work]:
        ms = Manuscript(paper_hash=digest("eval", query.title), title=query.title, abstract=query.abstract,
                        author_names=list(query.author_names))
        paper_date = PaperDate(query.date, "eval", "day")

        async def local(before):  # noqa: ANN001
            vector = (await embed(model, [ms.query_text()], provider))[0]
            near = await store.nearest(model, vector, k=50, before=before) if vector else []
            works = await store.get_works([i for i, _ in near])
            return [works[i] for i, _ in near if i in works]

        pool = await gather(ms, paper_date, client, Options(use_paper_references=options.use_paper_references), local)
        ranked = await rank_pool(pool, ms, model, provider, store, use_rerank)
        return [c.work for c in ranked]

    try:
        report = await harness.reference_recall(retriever, papers, k=k)
    finally:
        await client.close()
        await store.close()
    by_field: dict[str, list[float]] = defaultdict(list)
    for p in report.per_paper:
        by_field[p["field"]].append(p["recall"])
    print(f"eval set {version} | model {model} | rerank {use_rerank} | n={report.n}")
    print(f"reference recall@{k}: {report.mean:.3f}")
    for field_name, values in sorted(by_field.items()):
        print(f"  {field_name}: {sum(values) / len(values):.3f} (n={len(values)})")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=DEFAULT_EMBEDDING_MODEL)
    parser.add_argument("--rerank", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--k", type=int, default=20)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()
    report = asyncio.run(run(args.model, args.rerank, args.limit, args.k))
    if args.json:
        args.json.write_text(json.dumps(report.__dict__, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
