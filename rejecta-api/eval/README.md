# Scoring evaluation

Measures whether Rejecta's scores are right, on papers whose correct outcome we know. Every change to
the parser, prompt, scoring or lookups should be checked here before it ships.

## Layout

| Path | What | Committed |
| --- | --- | --- |
| `manifest.yaml` | The papers, their labels and the hand-checked expectations | yes |
| `papers/` | The PDFs (named in the manifest) | no: licensed content; share them privately |
| `cache/` | Saved CrossRef/OpenAlex answers, so runs are comparable | no |
| `runs/<time>-<label>-<code>/` | `results.jsonl`, `summary.md`, `summary.json` for each run | no; copy a baseline's `summary.json` to `baselines/` to keep it |

## Running

From `rejecta-api/`:

```bash
# Parser checks only (no model, no network, seconds). Run on every change.
python -m scripts.eval_scoring --parser-only

# Full run: 3 analyses per paper with the configured LLM provider. Costs model calls.
python -m scripts.eval_scoring --label baseline

# After a fix: same papers, compare with the baseline. Exit code 1 if any metric regressed.
python -m scripts.eval_scoring --label parser-fix --compare eval/runs/<baseline dir>
```

`--live` refreshes the cached lookups, `--only <id> ...` runs selected papers, and
`--split heldout` runs the held-out papers (only for the final check of a phase).

## Adding a paper

1. Use an openly licensed paper (CC-BY) or one you may share inside the team.
2. Save the PDF in `papers/` and add an entry to `manifest.yaml`.
3. Fill in `expect` by reading the paper: count its references, note whether a Methods section is in
   the file, and the article type printed on it. A second person checks the entry and adds their name
   to `labelled_by`.
4. Pick the label: `strong` (published and well regarded, run against the journal it appeared in),
   `weak` (retracted, rejected or clearly flawed) or `out_of_scope` (editorial, news, correction).
5. About one paper in five goes to `split: heldout`. Never change code while looking at held-out results.

## Targets

The metrics and their targets are defined in `scripts/eval_metrics.py` (`TARGETS`). The main ones:
no more than 5% of strong papers desk-rejected, a median score of 70 or more for strong papers, an AUC
of at least 0.80 between strong and weak papers, and a score that moves at most 5 points between runs
for 90% of papers.
