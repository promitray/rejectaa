# Literature knowledge base (`services/literature/`)

**Purpose:** novelty and journal fit decided against real papers, not the model's memory. Every statement about
other work cites a retrieved id (`[W12]`, `[J3]`), and code checks that the id was retrieved.

**Status:** P0 is done (foundations). It is **not wired into the analysis**: `settings.LITERATURE_KB_ENABLED` is False.

## Isolation rules (agreed)

| Allowed | Not allowed |
|---|---|
| Branch `feature/literature-kb`; new files under `services/literature/` | No edits to `claim_ledger.py`, `analysis_cache.py`, `report_version()` or the pipeline |
| New tests under `tests/literature/` | No merge, no commit |
| Edits only to `config.py` (one flag) and `tests/conftest.py` (keep it off in tests) | |

## P0 contents

| File | What it does |
|---|---|
| `__init__.py` | `KB_SCHEMA_VERSION`; embedding models and their vector sizes |
| `types.py` | `Work`, `PaperDate` |
| `schema.py` | Tables in schema `literature` (local pgvector DB): `works`, `work_embeddings` (one vector column per size, a partial HNSW cosine index per model), `journal_profiles`, `journal_members`, `retrieval_snapshots` |
| `store.py` | asyncpg store. Refuses Supabase and `DATABASE_URL`. Upserts works and embeddings, does kNN with a date cut-off and exclusions (one model's vectors only, deterministic tie-break), reads and writes W/J snapshots, writes journal profiles |
| `openalex.py` | Paced OpenAlex client (shared limiter, API key if set, answers cached in the local store). Works, search, reproducible samples, source lookup; abstracts rebuilt from the inverted index |
| `dates.py` | The paper's date: arXiv stamp → arXiv id (month) → publication record (year) → venue statement (year) → today. A year or month alone counts from its **start**, so later work can never be evidence. Also the 6-month concurrent window and prior / concurrent / later / undated classification |
| `eval/build_eval_set.py`, `eval/eval_set_v1.json` | The fixed, versioned eval set: 50 published 2022 papers, 25 CS/ML and 25 chemistry, 5 from each of 10 journals, by OpenAlex reproducible sample (seed 20261114), each with ≥20 references and an abstract |
| `eval/harness.py` | Leakage-safe reference recall@k and journal top-k (see "Evaluation") |

Verify the schema against the Docker DB with:

```
python -c "import asyncio; from services.literature.store import LiteratureStore as S; s=S(); print(asyncio.run(s.counts()))"
```

## P1 contents (prior-work retrieval; not wired into the analysis)

| File | What it does |
|---|---|
| `manuscript.py` | What the search knows about the upload: title, abstract, claims, the byline (names and affiliations read by code from page 1; "Anonymous" or none → anonymised), its own DOIs and arXiv ids, reference DOIs, venue statements, paper hash |
| `authors.py` | Each manuscript author resolved to an OpenAlex author id by name + affiliation + co-author overlap. A name is resolved only when one candidate is uniquely best with ≥1 point of evidence; it is never guessed. Prior-version labels: `prior_version_same_authors` (shared resolved id + very similar), `same_authors`, `possible_prior_version` (not resolvable or anonymised + very similar). None counts against novelty |
| `candidates.py` | Routes: (a) the paper's references by DOI, their related works and the most-cited works citing them (off in the eval); (b) OpenAlex search: title, abstract key words, claims, plus the most-cited works on the title query; (c) local kNN; (d) bibliographic coupling: works cited by several of the closest search hits. All routes are before the paper's date. Only an exact DOI or arXiv id excludes a work as "itself". Date classes: prior and concurrent kept; later and undated dropped |
| `embeddings.py` | One model per search: `text-embedding-3-small` (provider), or `allenai/specter2` run locally ("title [SEP] abstract"; needs torch/transformers/adapters, not installed). Cached by text |
| `ranking.py` | Hybrid: BM25 + cosine + coupling count, weighted reciprocal-rank fusion, deterministic. Then an LLM rerank of the top 30: relevance 0–3 and shared dimensions (task / method / data_material / finding). Only ids it was shown count; title-only works are capped at 2 |
| `retrieve.py` | End to end: date → authors → candidates → store + embed → rank → labels. The top 15 (plus any prior-version-labelled work) and the coverage go into the **W snapshot** (paper hash + model); "Run again" refreshes it |
| `eval/run_recall.py` | Reference recall@k on the frozen set, with route (a) off and the leak filter on |

## Decisions carried into P1–P4 (from the review)

1. **Own contribution vs acknowledged background.** A new step in this package reads the claim ledger's output and tags each claim `own_contribution` or `acknowledged_background`. The ledger itself is not changed. A W that the paper cites and acknowledges for the same point is `acknowledged_overlap`: no novelty penalty.
2. **Authors.** Uploaded manuscripts are not in OpenAlex, so their authors are resolved by name + affiliation + co-author overlap.
   - Resolved, with a shared OpenAlex author id → `prior_version_same_authors`.
   - Not resolvable, or the manuscript is anonymised, and title/abstract similarity is very high → `possible_prior_version`.
   - Neither ever counts against novelty. Both are shown to the user: the first as a prior-presentation fact, the second as a question.
   - Only an exact DOI or arXiv id match excludes a work as "the paper itself".
3. **"Already done"** needs:
   - ≥2 matching dimensions (task, method, dataset/material, finding);
   - a confirm step ("does the same work" vs "only on the topic");
   - an abstract (title-only works never count);
   - unacknowledged overlap;
   - prior, not concurrent.

   One dimension → `overlapping`.
4. **Dates.** A year alone counts from 1 Jan, and a month alone from the 1st. A candidate known only by its year counts from 31 Dec, so it is never wrongly prior. Work within 6 months before the paper → `concurrent`, weak weight.
5. **Snapshots.** W keyed by paper hash; J keyed by paper hash + target journal. "Run again" refreshes both.
6. **Journal fit:**
   - kNN density: mean similarity to the paper's k nearest journal papers, as a percentile of the journal's own papers, computed **leave-one-out**;
   - a profile below 200 papers → low-confidence fit;
   - alternative journals shown with tier and policy.
7. **Novelty score:** computed in code from the verdicts when there are ≥10 relevant candidates. Otherwise the model's score stays, capped, with a "low coverage" note.

Also carried forward:
- **Missing abstracts:** title-only embedding with `abstract_missing`, never evidence for "already done", and counted in the coverage note.
- **Embedding model:** one search uses one model; changing the model means embedding again.
- **Later (P4):** InChIKey/PubChem matching for chemistry; Semantic Scholar licence check before SPECTER2.

## Cache key (documented only; to be done at integration, after approval)

Today the report cache key is owner + file hash + journal + mode + type + `report_version()` + model (`services/analysis_cache.py`). At integration:

- **Add:** `KB_SCHEMA_VERSION` and the embedding model, only when `LITERATURE_KB_ENABLED` is on.
- **Do not add:** the size or version of the knowledge base, or a snapshot hash. The knowledge base grows on every upload, so either would miss every cached report. A snapshot hash is not even known when the cache is looked up, which is before the analysis.
- **Why this is still deterministic:** the W and J snapshots are stored per paper (and per journal for J), so the same paper always gets the same retrieved works. "Run again" (`fresh`) refreshes the snapshots.

## Evaluation (`eval/harness.py`)

- **Reference recall@20:**
  - the retriever gets only what a manuscript carries (title, abstract, date, author names);
  - references are hidden and candidate route (a) (the paper's references, their related and cited-by works) is **off**;
  - results are filtered as at analysis time: not the paper itself, nothing from its date on, nothing that cites it, nothing undated.
- **Journal top-3:** whether the real journal is in the top 3.

Both run on the frozen `eval_set_v1.json`. The P1 retriever and the P3 ranker plug in as callables. In P1, compare `text-embedding-3-small` with SPECTER2 on recall@20.

## Phases

- **P0 (done):** schema, store, OpenAlex client, dates, snapshots, eval set, harness.
- **P1:**
  - paper date, author resolution and prior-version labels;
  - candidates from (a) references, (b) OpenAlex search and (c) local vectors, with exclusions and date classes;
  - embedding, hybrid search and rerank;
  - W snapshot;
  - recall@20 for both embedding models.
- **P2:**
  - claim tagging (own vs background);
  - dimensions per W;
  - verdicts (`new` / `incremental` / `overlapping` / `acknowledged_overlap` / `concurrent` / `already_done`) with a confirm step;
  - novelty score with fallback;
  - W facts for the letter.
- **P3:**
  - journal profiles (≥200 papers, leave-one-out kNN density);
  - fit, closest J papers and alternative journals;
  - J snapshot;
  - top-3.
- **P4:** missing-citation findings, InChIKey matching, Semantic Scholar/SPECTER2, offline seeding.
