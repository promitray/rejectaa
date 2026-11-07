"""Check references against the real catalogs (CrossRef, Europe PMC, OpenAlex, arXiv) from the command line.

Handy for seeing why a reference is unverified, and for checking a whole bibliography before
uploading a paper. Uses the same code as the API, including OPENALEX_API_KEY from .env.

From rejecta-api/:

    # your own references, one per line (a leading [1] or 1. is fine)
    .venv/Scripts/python.exe scripts/check_citations.py my_references.txt

    # built-in sample: real chemistry references in several styles plus one made-up reference
    .venv/Scripts/python.exe scripts/check_citations.py --sample
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services import citations  # noqa: E402
from services.citation_refs import parse_reference  # noqa: E402

SAMPLE = [
    "Wedler-Jasinski, N.; Lueckerath, T.; Mutlu, H.; Goldmann, A. S.; Walther, A. A.; Stenzel, M. H.; Barner-Kowollik, C. Chem. Commun. 2017, 53, 157-160.",
    "Bian, S.; Scott, A. M.; Cao, Y.; Liang, Y.; Osuna, S.; Houk, K. N.; Braunschweig, A. B. J. Am. Chem. Soc. 2013, 135, 9240-9243.",
    "R. H. Grubbs, Angew. Chem., Int. Ed., 2006, 45, 3760.",
    "Kolb HC, Finn MG, Sharpless KB. Click chemistry: diverse chemical function from a few good reactions. Angew Chem Int Ed Engl. 2001;40(11):2004-2021.",
    "Matyjaszewski, K.; Xia, J. Chem. Rev. 2001, 101, 2921-2990.",
    "A. Suzuki, Angew. Chem. Int. Ed. 50 (2011) 6722-6737.",
    "Smith, J.; Doe, A. J. Am. Chem. Soc. 2019, 141, 99999-99999.  (made up: should NOT verify)",
]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file", nargs="?", help="text file with one reference per line")
    parser.add_argument("--sample", action="store_true", help="use the built-in sample")
    parser.add_argument("--verbose", action="store_true", help="show service-level log lines")
    args = parser.parse_args()

    if args.sample:
        refs = SAMPLE
    elif args.file:
        refs = [line.strip() for line in Path(args.file).read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        parser.error("give a file of references, or --sample")

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(message)s")
    citations.clear_citation_cache()
    started = time.perf_counter()
    results = await citations.verify_citations(refs)
    took = time.perf_counter() - started

    for ref, result in zip(refs, results):
        parsed = parse_reference(ref)
        label = "NOT CHECKED" if result.check_incomplete else result.status.value.upper()
        fingerprint = f"{parsed.first_author} {parsed.year} v{parsed.volume} p{parsed.first_page}"
        print(f"{label:11s} {ref[:88]}")
        print(f"            read as: {fingerprint}")
        if result.status.value == "verified":
            print(f"            -> {result.title or result.venue} ({result.year}) {result.doi or ''}")
        else:
            print(f"            -> {result.diagnostic_reason}")

    counts = Counter("not checked" if r.check_incomplete else r.status.value for r in results)
    print(f"\n{len(refs)} references in {took:.1f}s: {dict(counts)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
