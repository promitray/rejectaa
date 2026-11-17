"""Build the fixed, versioned evaluation set: ~50 published papers (CS/ML and chemistry) with known references and
known journal, frozen to a JSON file so every evaluation uses exactly the same papers.

    python -m services.literature.eval.build_eval_set                 # writes eval_set_v1.json next to this file
    python -m services.literature.eval.build_eval_set --out other.json

Each journal contributes PER_JOURNAL papers, drawn by OpenAlex's reproducible sample (fixed seed) from one
publication year, keeping only research articles with an abstract and at least MIN_REFERENCES resolved
references. The file records the journals, the year, the seed and the filters, so the set can be rebuilt and
checked. Re-running gives the same papers as long as OpenAlex's data has not changed; the frozen file is the
reference, not the rebuild.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from services.literature.openalex import OpenAlexClient  # noqa: E402

EVAL_SET_VERSION = "2026-11-14.1"
DEFAULT_OUT = Path(__file__).resolve().parent / "eval_set_v1.json"
YEAR = 2022
SEED = 20261114
PER_JOURNAL = 5
SAMPLE_SIZE = 40  # drawn per journal before filtering
MIN_REFERENCES = 20

JOURNALS: dict[str, list[str]] = {
    "cs_ml": [
        "Nature Machine Intelligence",
        "IEEE Transactions on Pattern Analysis and Machine Intelligence",
        "Journal of Chemical Information and Modeling",
        "Neural Networks",
        "Pattern Recognition",
    ],
    "chemistry": [
        "Journal of the American Chemical Society",
        "Chemical Science",
        "Organic Letters",
        "ACS Catalysis",
        "Angewandte Chemie International Edition",
    ],
}
_SKIP_TITLES = ("correction", "erratum", "retraction", "editorial", "corrigendum", "in this issue", "preface")


async def build(out: Path) -> dict:
    client = OpenAlexClient()
    papers: list[dict] = []
    journals_used: list[dict] = []
    missing: list[str] = []
    try:
        for field, names in JOURNALS.items():
            for name in names:
                source = await client.find_source(name)
                if source is None:
                    missing.append(name)
                    continue
                filters = f"primary_location.source.id:{source['id']},publication_year:{YEAR},type:article,has_abstract:true"
                sample = await client.sample_works(filters, SAMPLE_SIZE, SEED)
                kept = [
                    w for w in sorted(sample, key=lambda w: w.id)
                    if not w.abstract_missing and len(w.reference_ids) >= MIN_REFERENCES and w.publication_date
                    and not any(word in w.title.lower() for word in _SKIP_TITLES)
                ][:PER_JOURNAL]
                journals_used.append({"field": field, "name": name, "source_id": source["id"], "papers": len(kept)})
                for w in kept:
                    papers.append({"field": field, "journal": {"source_id": source["id"], "name": source["display_name"]}, **w.to_json()})
    finally:
        await client.close()
    data = {
        "version": EVAL_SET_VERSION,
        "built_at": dt.date.today().isoformat(),
        "source": "OpenAlex",
        "selection": {"year": YEAR, "seed": SEED, "per_journal": PER_JOURNAL, "sample_size": SAMPLE_SIZE,
                      "min_references": MIN_REFERENCES, "filters": "type:article, has_abstract:true"},
        "journals": journals_used,
        "journals_not_found": missing,
        "papers": papers,
    }
    out.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    return data


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    data = asyncio.run(build(args.out))
    print(f"{len(data['papers'])} papers from {len(data['journals'])} journals -> {args.out}")
    if data["journals_not_found"]:
        print("not found in OpenAlex:", ", ".join(data["journals_not_found"]))


if __name__ == "__main__":
    main()
