"""Run ONE stage of the analysis pipeline on a real file and print what it produced, so each stage can be
checked by eye before the next one is run.

    python -m scripts.stage_test parser  eval/papers/marc-2024-polythioesters-minireview.pdf
    python -m scripts.stage_test parser  paper.pdf --full      # also print each section's text start

Stages (added one at a time as they are checked): parser
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from services.parser import parse_document  # noqa: E402


def head(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(3, 70 - len(title)))


def stage_parser(path: Path, article_type: str | None, full: bool) -> None:
    p = parse_document(path.read_bytes(), path.name, article_type)
    head("1. PARSER (code only, no LLM)")
    print(f"file            : {path.name}")
    print(f"title           : {p.title!r}")
    print(f"article type    : {p.article_type}  (source: {p.article_type_source})")
    print(f"structure       : {p.structure}  coverage={p.coverage:.2f}  signature={p.structure_signature}")
    print(f"word count      : {p.word_count}   full_text chars: {len(p.full_text)}")
    print(f"refers to SI    : {p.refers_to_si}")
    print(f"DOI candidates  : {p.doi_candidates}")
    print(f"watermark marks : {[(m.kind, m.text, m.pages) for m in p.integrity_marks] or 'none'}")
    print(f"parse warnings  : {p.parse_warnings or 'none'}")

    head("Abstract")
    print(f"[{len(p.abstract)} chars] {p.abstract[:600]}{'...' if len(p.abstract) > 600 else ''}")

    head(f"Sections ({len(p.sections)})")
    print(f"{'key':28} {'role':16} {'chars':>7}  printed heading")
    for key, text in p.sections.items():
        print(f"{key[:28]:28} {p.section_roles.get(key, '-'):16} {len(text):>7}  {p.section_titles.get(key, '')!r}")
        if full:
            print("      " + text[:250].replace("\n", " ") + " ...")

    head(f"References ({len(p.references)})")
    for i, ref in enumerate(p.references[:5], 1):
        print(f"{i:>3}. {ref[:150]}")
    if len(p.references) > 10:
        print("   ...")
    for i, ref in enumerate(p.references[-3:], len(p.references) - 2):
        print(f"{i:>3}. {ref[:150]}")


def dump_parser(path: Path, article_type: str | None, out_dir: Path) -> Path:
    """Write EVERYTHING the parser produced, unshortened, to one text file for comparing with the original."""
    p = parse_document(path.read_bytes(), path.name, article_type)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{path.stem[:40]}__1_parser.txt"
    bar = "=" * 90
    lines = [
        f"PARSER OUTPUT for {path.name}", bar,
        f"title: {p.title}", f"article_type: {p.article_type} ({p.article_type_source})",
        f"structure: {p.structure} coverage={p.coverage:.2f}", f"doi_candidates: {p.doi_candidates}",
        f"integrity_marks: {[(m.kind, m.text) for m in p.integrity_marks]}",
        f"parse_warnings: {p.parse_warnings}", f"word_count: {p.word_count}", "",
        bar, f"ABSTRACT ({len(p.abstract)} chars)", bar, p.abstract, "",
    ]
    for key, text in p.sections.items():
        lines += [bar, f"SECTION key={key!r} role={p.section_roles.get(key)!r} "
                       f"printed_heading={p.section_titles.get(key)!r} ({len(text)} chars)", bar, text, ""]
    lines += [bar, f"REFERENCES ({len(p.references)})", bar]
    lines += [f"[{i}] {r}" for i, r in enumerate(p.references, 1)]
    lines += ["", bar, f"FULL TEXT as the parser sees it ({len(p.full_text)} chars)", bar, p.full_text]
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def main() -> None:
    cli = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    cli.add_argument("stage", choices=["parser"])
    cli.add_argument("file", type=Path)
    cli.add_argument("--type", dest="article_type", default=None, help="article type override")
    cli.add_argument("--full", action="store_true")
    cli.add_argument("--dump", action="store_true", help="write the complete, unshortened output to stage_output/")
    args = cli.parse_args()
    if args.stage == "parser":
        stage_parser(args.file, args.article_type, args.full)
        if args.dump:
            print("\nFull output written to:", dump_parser(args.file, args.article_type, Path(__file__).resolve().parents[1] / "stage_output"))


if __name__ == "__main__":
    main()
