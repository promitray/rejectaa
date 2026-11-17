# Rejecta analysis pipeline: from PDF upload to report

Context document for working on the Rejecta paper-analysis backend. Describes what runs today (Nov 2026,
uncommitted work included), in order, with the file for each stage.

**Product.** Rejecta is a pre-submission reviewer. "Professor Aldric Frowns" is a strict editor persona. It
analyses a manuscript against a target journal and returns:
- a score and a verdict
- rejection reasons, each with a quotation from the paper
- a section-by-section analysis with fix plans
- the editor's letter and the journal fit
- citation, integrity and consistency checks

**Design principle.** The model never has the last word on a fact:
- every quotation is matched against the paper;
- every number is checked against the paper;
- every section, figure or claim of absence is verified;
- scores and the decision are computed in code.

The same file must give the same report every time.

**Stack.**
- **Backend:** FastAPI in `rejecta-api/`. Model: OpenAI `gpt-4o-2024-08-06`, temperature 0, seed 20261110. Embeddings: `text-embedding-3-small`.
- **Frontend:** React/Vite in `rejecta-frontend/`.
- **Production DB:** Supabase Postgres, schema `app`.
- **Local RAG/cache DB:** Docker `rejecta-rag-db`, pgvector/pg16, `127.0.0.1:5433`.

---

## 0. Upload endpoint: `rejecta-api/main.py` → `analyse_paper`

Runs in this order:

1. **Receive.** Form fields: the file (PDF or DOCX), `journal`, `mode` (a/b), optional article type, and `fresh` ("Run again").
2. **Report cache.** Key = owner + file SHA-256 + journal + mode + type + `report_version()` + model (`services/analysis_cache.py`).
   - On a hit, the stored report is returned (in memory, 7 days).
   - `report_version()` combines every prompt version, the rubric versions, the journal policy version and the absence-kinds version. Any prompt change therefore gives a fresh report.
3. **Parse:** `services/parser.py` → `ParsedDocument`. See §1.
4. **Revision lookup:** `services/revisions.py`. A fingerprint (MinHash) of abstract + body is looked up in the analysis history (DB table, migration 0010), to recognise a revised version of an earlier upload.
5. **Analyse:** `services/pipeline.py → run_analysis` (§2–§10).
6. **Save:** a history record (scores, issues, fingerprint; no paper text), a Word "working copy" of the PDF (`services/documents`, pdf2docx), and the report cache entry.

---

## 1. Parsing: `services/parser.py` (code only, no model)

- **Text.** PyMuPDF extracts text page by page. Cover pages and running headers/footers are dropped, and ligatures are fixed.
- **Tables and figures (PDF)** (`services/pdf_layout.py`, flag `PDF_LAYOUT_TABLES`):
  - **Tables:** found from their caption ("Table N." / "Table N |"). Rows come from word positions and columns from white space that most data rows share, within the caption's column on two-column pages. A ruled table from PyMuPDF `find_tables` in the same area is used when complete. Each table replaces its loose words in the text with one structured line, `[Table N (p. X) as a table. Columns: … Rows: …]`, and is also stored in `ParsedDocument.tables`.
  - **Figures:** short text blocks inside images or large vector drawings (axis ticks, legends) are left out, only on pages with a figure or scheme caption. Captions, headings and prose are always kept.
  - **What the LLM gets:** figure images are never sent; only the captions.
- **Dehyphenation** (`services/textclean.py`): line-end breaks are joined from the paper's own vocabulary ("re-cently" → "recently", "Ta-ble" → "Table"). Real compounds keep their hyphen ("non-deep", "self-supervised", "sequence-based"), using prefix and suffix keep-lists.
- **Abstract:** found by the "Abstract" label, by text block, or by font style.
- **Article type:** from a label in the file (research_article, communication, review, mini_review, perspective, editorial), or the user's choice.
- **Sections.** A section starts at:
  - a known heading (Introduction, Methods, Results…);
  - a numbered or ALL-CAPS heading with a role keyword;
  - a heading the layout marks (bold/larger) that continues the paper's numbering ("3. Why above phenomena would occur?") or lettered appendices ("A. …").

  Each section gets a key, a role (introduction/methods/results/discussion/thematic/other) and its printed title. Table column headers named "Method" are not headings. `structure` = detected | partial | none.
- **References:** split into entries (numbered, author-year…). Back matter (Acknowledgements, COI…) is cut off.
- **Outline** (`services/outline.py → attach`). For each section:
  - printed label ("2", "B");
  - part: `main`, or `appendix` when after the References heading or titled "Appendix";
  - first page.

  Also extracted:
  - numbered subsections in sequence (2.1, C.1–C.3);
  - figures and tables with captions and pages;
  - venue statements from page 1 ("The ICML 2023 Workshop on …", arXiv stamp);
  - page texts.
- **Integrity marks:** retraction or expression-of-concern watermarks, and the paper's own DOI candidates.

---

## 2. Lookups, run in parallel (`run_analysis`, `asyncio.gather`)

Each lookup is cached per paper for 7 days in the local store (`services/lookup_snapshot.py`), so a busy service cannot change a re-run. `fresh` (Run again) refreshes them.

| Lookup | File | What it does |
|---|---|---|
| Citations | `services/citations.py` | Each reference checked against CrossRef, OpenAlex, Europe PMC, arXiv, DataCite: verified / ghost (DOI or arXiv id does not exist) / unverified / not checked. |
| Journal | `services/journals.py` | OpenAlex source match (exact or approximate), 2-year mean citedness, h-index, recent papers, and the journal's papers most similar to this abstract. |
| Publication record | `services/integrity.py` | Paper's DOI or title → Crossref `updated-by` (publisher and Retraction Watch): retracted / expression of concern / corrected / published / not_found. OpenAlex `is_retracted` is used only as a note. |
| Consistency | `services/consistency.py` | One model call (up to 12k output tokens) extracts checkable values with exact quotes; code verifies each quote and its number. **Field-agnostic quantities** (the paper's field, counts with `core`/`part_of`, N/M fractions, restated results, parameters, each with a canonical name) are compared by code: the same count or result differing between places (results compared at the precision written), a fraction whose denominator is not the stated total ("8/15 datasets" vs 14), sub-groups not summing to their total. Values from one passage, and two values stated together in one sentence (a deliberate variation), are not compared. Also, from before: biology/chemistry protocol values (Methods vs other sections), % claims, units, 96-well density, foreign compounds, durations, arrest phases, and a Cellosaurus lookup for cell lines (contaminated/misidentified = major). Section names and pages come from code. |
| Disclosures | `services/disclosures.py` | Regex for COI / funding statements. No penalty. |

The model is given only stable citation facts (total and ghosts). The citation criterion is scored by code.

---

## 3. Analysis context: `services/analysis_guard.py → AnalysisContext`

Built once and shared by every stage. It holds:
- full text, abstract, body, section texts, roles, titles;
- the outline fields: labels, parts, pages, figures, subsections, venue statements, page texts;
- the publication record, consistency report and disclosures;
- citation counts;
- the target journal and the previous version's issues;
- the normalised corpus used for quote matching (`alnum`: lowercase letters and digits, Greek letters folded).

---

## 4. Main analysis: one large model call (`services/llm/base.py`, `services/llm/openai.py`)

**Prompt** (`PROMPT_VERSION`):
- the whole paper (up to 200k characters) and the abstract;
- target journal and its standing (from citedness);
- **how the journal decides** (`knowledge/journals.yaml` via `services/journal_policy.py`: `soundness` e.g. Scientific Reports and PLOS ONE, `selective` e.g. Nature family and JACS, or `standard`);
- the article-type profile (expected sections, criteria keys);
- the publication record and consistency findings;
- the **verified outline**: sections with main/appendix, pages, subsections, figures, structure observations, and the rules "name only sections in the outline", "say Appendix X", "do not assume Discussion/Limitations exist";
- the venue statements.

**Output JSON:**
- `criteria` scores (research: novelty 25, rigor 25, evidence_support 20, clarity 10, journal_fit 10; reviews have their own keys);
- `rejection_reasons` (problem / evidence quote / impact / solution / severity / criterion);
- per-section score, issue and fix;
- verdict, oracle letter, novelty assessment, journal fit.

**Determinism:** temperature-0 answers are stored by the full request hash (`services/llm/response_cache.py`, local store). The same request gets the same answer.

### 4a. Guard: `analysis_guard.finalize_payload` (code)

- **Sections:** only sections the parser detected are kept, in paper order, with label/part/page attached.
- **Section checklist** (`services/section_checks.py`): deterministic per-role checks (aim, citations, seeds, replicates, statistics, data availability, limitations, figure references and order…), each item quoting the paper.
- **Rejection reasons:** a reason whose evidence quote is not in the paper goes to `unverified_concerns` (not scored).
- **Outline checks** (`outline.check_prose`): a sentence naming a section, subsection or figure that does not exist is removed. The first mention of an appendix section gets "(Appendix X)".
- **Quotes and numbers:** quotations not in the paper are scrubbed, and numbers not in the paper are flagged.
- **Score** (`score_report`):
  - weighted mean of criteria;
  - weights adjusted by the journal policy (soundness: novelty ×0.3, journal_fit ×0.5, rigor/evidence ×1.3; selective: novelty/fit ×1.2);
  - caps: retracted ≤5, expression of concern ≤40, rigor ≤60/45 for 1/3+ major inconsistencies, a criterion with a verified major concern ≤75, desk-reject concern ≤40;
  - penalties for no Methods / no SI.

### 4b. Evidence check: `services/evidence_check.py`

A second call per reason: "does the quoted passage show this problem?" "No" moves the reason to unverified, and the score is recomputed.

---

## 5. Section-by-section review: evidence ledger (runs in parallel with §4)

Entry point: `services/section_review.py → review_sections`. With `EVIDENCE_LEDGER_ENABLED` it uses `services/evidence_ledger.py`. If that fails, it falls back to the older batch review (4 sections per call, quotes checked).

1. **Paper index** (`services/paper_index.py`): every paragraph and sentence gets a stable id (`P12.3`). The model cites ids; code copies the words, so a quotation cannot be invented or altered.
2. **Search index** (`services/retrieval.py`): BM25 plus embeddings, fused by reciprocal rank and deterministic. Paragraph vectors are stored in the local store (`rag.paper_chunks`, `rag.embeddings`).
3. **Claim ledger** (`services/claim_ledger.py`, 1 call on the whole numbered paper):
   - field and paper kind (fixed lists);
   - up to 8 claims, each with type (novelty / comparative / causal / mechanism / generalization / quantitative / practical), main flag and sentence ids;
   - up to 24 facts.

   Code drops entries whose ids, words or numbers are not in the cited sentences.
4. **Audit plan** (`services/rubrics.py` + `knowledge/rubrics/*.yaml`): code, not the model, builds specific questions.
   - Formula: the claims × the rubric items of the selected packs.
   - Packs: universal; cs_ml; chemistry_synthesis / catalysis / computational / materials / analytical; nature.
   - Packs are selected by field and journal. Items carry severity, a fix template and a source (e.g. "NeurIPS Paper Checklist").
   - Items are skipped when the code checklist already decided them, or `not_for` the journal tier (no "broad significance" check at soundness journals).
   - Limits: ≤6 questions per section, ≤24 in total.
5. **Investigation:** 1 call per section, in parallel. Input: the section text with ids, its questions, passages retrieved from other sections, the ledger, and the checklist problems.
   - Each question gets supported / problem / not_reported / cant_tell, with evidence ids.
   - Up to 3 free findings per section, plus a summary and strengths.
   - Rules: every part of a question checked; silence is never stated as a fact; nothing faulted that the SI or appendix holds.
6. **Author defence** (1 call): each "problem" finding that is not a claim of absence is put to an author's advocate with the passages most likely to answer it. Outcomes:
   - resolved → removed;
   - partly → narrowed to what is left;
   - stands → kept.
7. **Specificity gate** (code):
   - a finding must name this paper's content;
   - its fix must be concrete (number, figure/table, named test or term), and generic advice is rejected;
   - free findings with no claim and no concrete detail are dropped.
8. **Merge** (code): one point per section. Merged findings:
   - the same check on several claims;
   - two checks on one claim with the same sentences;
   - alike problems;
   - two absence statements of the same kind for the same claim.
9. **Severity by rule:** the rubric's severity applies. A major check on a non-main claim becomes minor, and a free finding is major only for a main claim.
10. **Output:** per section, the summary, strengths and findings (with impact, claim, check source, evidence ids and page), plus a summary of the ledger (claims, packs, every question and answer, removed findings with reasons).

### 5a. `apply_review` (code)

- **Section score** = start at 85, then:
  - checklist problems −8 major / −2 minor, capped at 20;
  - findings −7 / −2, capped at 14;
  - strengths +2 each, up to +4;
  - result kept within 15–95, with every step listed.
- **Fix plan:** at most 6 steps, major first, each with kind "add" or "change". Used by "Fix with Calibrator".
- Sections the main model did not score still get a card.
- Verified **major** findings become report concerns (`origin = evidence_ledger`, up to 4, main claims first, no duplicates of the model's own concerns). They cap their criterion, and the score is recomputed.

---

## 6. Absence audit: `services/absence_audit.py` (after §5, `ABSENCE_AUDIT_ENABLED`)

Every statement in the report that the paper **lacks** something is checked against the whole paper before it stays. This prevents claiming that evidence is missing when it exists ("lacks a controlled experiment" when Section 3 is that experiment).

1. **Collect.** Absence sentences are found by regex in findings, model concerns, section summaries, criterion reasons, novelty, journal fit, verdict and letter.
   - **Scope** (code): a section's finding or summary is judged on that section, unless it is worded about the whole paper ("the paper never…"). Everything else is judged on the whole paper.
   - **Kind** (`knowledge/absence_claims.yaml`: controlled_experiment, statistics, results_evidence, replicates, baseline, limitations, generalization, data_code, methods_detail, other). Each kind has a search query and an evidence note ("an experiment that varies the suspected cause counts, whatever the paper calls it").
2. **Verify** (1 call). The whole numbered paper is sent when ≤140k characters; otherwise per-item retrieval, including the best paragraphs of every section. The checker:
   - splits each statement into **parts** (each missing element, as a short noun phrase);
   - gives each part found / not found with up to 3 exact quotes;
   - locates each part **on its own**;
   - treats a sentence that only points elsewhere ("can be seen in the appendix") as a pointer, not evidence.
3. **Confirm** (1 call). Every part counted as found is re-checked: does the sentence **state** the thing, or is it only on the topic? Only "states" counts.
4. **Decide** (code). Each quote is located in the paper; the section comes from the sentence id, not from the model. Outcomes:
   - all parts found **in scope** → **present**: the statement is removed;
   - some found, or found only in another section, or only pointed to → **partly**. The statement is rewritten **element by element**, each with its own location ("n is given in Appendix B; a measure of spread is only referred to (…), but the appendices in the file do not show it; a significance test is not given anywhere"). A gap only of this section becomes minor, with a "point to Section Y" fix;
   - nothing found → **absent**: kept and marked "searched".

   A removal is cancelled when another statement of the same section about the same thing is kept.
5. **Refresh:** section scores, fix plans, ledger concerns and the overall score are recomputed.

### 6a. Novelty assessment: `services/novelty.py` (after §6, `NOVELTY_FROM_CLAIMS`)

Replaces the main analysis's free-text `novelty_assessment`; needs the claim ledger (§5), otherwise the old text stays.
One model call (claims + numbered paper + the outline's figures/tables), then code checks. Per claim (Part A):

1. **Claim and scope** (data, models, materials, conditions) with ids.
2. **Prior work by the paper's own account**, listed once per sentence with the claims it applies to and a relation
   (`same_finding` / `builds_on` / `challenges` / `reuses_data_from`). Code: citation must be in the cited sentence;
   the category (`covers`) must be the sentence's own words; a sentence that contradicts or questions earlier work
   ("contrary to", "question", "inconsistent with"…) is forced to `challenges`. Code also applies a sentence to a
   claim the model left out when the claim names its category *and* shares ≥2 other words (≥25%) with it
   (`by_scope`, shown as "within the scope of"). Cited theory → the novelty is the application or test.
3. **Delta**: the concrete difference, with ids and the figure/table/section that shows it, given with its section
   and page (`Table 1 (Benchmarking Results, p. 3)`). Vague deltas ("new insights") are dropped.
4. **Evidence kind** (`new_experiment` / `reanalysis_of_existing_results` / `new_explanation` / `new_scope`).
   Code: a new experiment on a figure/table already used for an earlier claim becomes reanalysis; reanalysis is
   never causal; "causal" without a control/ablation/counter-test becomes correlational.
5. **Strength of the new part**: counts (number must be in its cited sentences), control/ablation/counter-test,
   variance/repeats/statistics (cited sentences must contain SD, ±, CI, p, a test, seeds/runs, replicates…).
   Missing ones are stated as "not reported".

Then: superlatives (code also finds them; *backed* only by a comparison with cited work); status per claim and the
verdict by code (overlapping if every main claim overlaps; new only if a main claim is new and none is a
`same_finding`; else incremental); the model's reason kept only if it matches, cites ids and is not vague.
`analysis.novelty` holds the data for the Novelty panel. Judged only against work the paper cites.
The prompt's JSON shape uses `<placeholders>` only: concrete example values were copied into answers.

**Field accuracy rules (code):**
- *All experiments:* every figure/table the model names plus every one the delta and claim sentences mention.
- *Location:* the printed section or appendix of the figure's page (`section_pages` + `section_parts`); a page where
  an appendix runs is "Appendix A: title", even if the main text refers to the figure. The prompt lists every figure
  and table with this location and its caption.
- *Counts:* the number must be in the cited sentences or the caption of one of the claim's figures/tables (`source`).
- *Variance:* from the model's sentences, else the captions of the claim's figures, else the methods. A pointer
  ("can be seen in the appendix") counts only if an appendix in the file shows values (±, SD = …); otherwise
  `referred_not_shown`. Status: `shown` / `referred_not_shown` / `not_given`.
- *Confound:* an intervention (claim with a control) whose paragraph uses a representation (fingerprints/ECFP/
  Tanimoto, graphs, SMILES, 3D, descriptors) that a compared model's claim names → "possible confound"; also taken
  from the model. Causal support is shown only with that note.
- *Theory* is named by its citation found in the cited sentence ("Rahaman et al., 2019"); none → dropped.
- *Status:* `new` needs a new experiment or explanation, no cited theory, and no same/builds-on/reuses prior;
  reanalysis and new scope are at most incremental.
- *Superlatives:* backed only on the axes (models, descriptors, datasets, metrics) the cited comparison names; the
  others are listed as not covered.

**Round 2 (prompt `2026-11-16.12`; prompts stay generic, no paper-specific examples — a test checks this):**
- *Variance* is three things: repeats (runs/seeds/replicates, usually from the methods, with what they cover as the
  paper words it), error bars per cited figure (does the caption define them; whether drawn cannot be read from text),
  spread values (`shown` / `referred_not_shown` / `not_given`). Repeats are never reported as spread. All claims,
  benchmark claims included.
- *Float location:* main text → "Table N, p. X, cited in Section Y" (first non-caption citation); appendix label only
  when the caption is printed in an appendix or the page lies after the appendices begin.
- *Prior-work scope:* a sentence about an outcome does not cover a causal claim (dropped, noted); a category sentence
  covers member claims. *Challenges:* the prior sentence, the next sentence, or the claim questions/limits the
  earlier belief ("not solely", "cannot be attributed", "rather than").
- *Evidence floats:* every figure/table the claim's sentences or the experiment's paragraphs cite (introduction
  paragraphs add only the cited sentences). The reanalysis rule uses only the floats the delta's own sentences cite.
- *Interventions:* accepted only if the defining sentence (or its paragraph) manipulates something; labels vs inputs
  read from the text. Confound note names the representation (and specific terms), the models whose input sentence
  names the same family, and same vs related. Inputs → side effects (sparsity, scale, type, dimensionality) or "not
  stated". A claim on the same figure gets "same intervention as Cx".
- *Caption counts:* a caption naming its datasets panel by panel is counted "(from the caption)"; full captions are
  read from the page text.
- *Depth fields* (were never in the prompt: the original paste was cut at Part A point 5): contribution type, limits,
  skeptic's objection, what would strengthen it, overall paragraph (sentence by sentence with ids), novelty vs journal
  tier (code, from `journal_policy`). Generic notes ("may not generalise", "test more datasets") are dropped.
- *Verdict label:* "new (vs cited work)".

---

## 7. Report context: `editor_letter.attach_context`

Facts for the report:
- the outline (with subsections);
- structure notes by code ("The main text has no Methods section; it is in Appendix B (p. 9)");
- venue statements;
- the journal policy.

---

## 8. Editor's verdict, letter and journal fit: `services/editor_letter.py` (`EDITOR_LETTER_FROM_FINDINGS`)

Written **last**, from verified material only.

1. **Numbered facts:**
   - `F` concerns (verified reasons and findings, each with location and quote);
   - `S` strengths;
   - `C` claims;
   - `O` structure observations;
   - `V` venue statements;
   - `J` journal facts (how it decides, standing, its closest published papers).
2. **Decision by code:**
   - desk reject if a desk-reject concern exists or score <40;
   - revise first if score <65 or ≥2 major concerns;
   - otherwise send to review.
3. **One call** writes the verdict, a 4–7 sentence letter and 2–4 journal-fit sentences. Every sentence is JSON `{text, refs}`.
4. **Code keeps a sentence only if:**
   - its refs exist;
   - a critical sentence cites F, O or V;
   - its numbers are in the paper or the facts;
   - every named section or figure exists (outline check);
   - its quotations are in the paper.
5. A verdict that contradicts the decision is replaced by a template. If fewer than 2 sentences survive, the main analysis's letter stays.
6. The publication context is presented as "check the journal's prior-presentation policy", never as a merit.
7. Retracted papers keep the code-written verdict.

---

## 9. Revision comparison: `services/revisions.py`

When an earlier version exists: score changes, earlier issues resolved / partly / open (judged by the main model in §4), and same-file or same-journal handling.

---

## 10. Response and frontend: `rejecta-frontend/src/pages/Results.tsx`

**Panels:**
- integrity banner and revision panel;
- score, criteria and "how this score was set";
- rejection reasons (origin badge, evidence with location and page);
- citations table;
- Editor's verdict + "What each sentence is based on" (letter points with their facts);
- journal fit (policy badge, venue statement);
- Section analysis:
  - "Paper structure" panel (outline, subsections, structure notes);
  - evidence-ledger panel (claims, every check and its answer, removed findings);
  - "Missing statements checked" panel (each statement, its scope, parts ✓/↗/⇢/✗, the paper's words);
  - per-section cards (label, appendix tag and page, summary, strengths, findings with claim, impact, "Verified against the text, p. N", "Searched every section", "A gap of this section only", checklist, fix plan, "✨ Calibrate", Heatmap).

**Fix with Calibrator** (`src/focus.ts`, `services/calibrator/engine.py`): the section text plus a numbered list of its issues (problem, quote, "why it matters", fix). Missing content gets `[AUTHOR TO ADD: …]` placeholders, and every issue must be reported back.

---

## Model calls per analysis (fresh, research paper with headings)

| # | Call | Stage |
|---|---|---|
| 1 | Consistency extraction | §2 |
| 2 | Main analysis | §4 |
| 3 | Evidence check | §4b |
| 4 | Claim ledger | §5 |
| 5..n | Investigation, one per section | §5 |
| +1 | Author defence | §5 |
| +1 | Absence audit | §6 |
| +1 | Absence confirmation | §6 |
| +1 | Novelty assessment | §6a |
| +1 | Editor letter | §8 |

That is about 12–16 calls, plus embeddings. A first run takes about 20–75 s; a re-run of the same file takes about 1 s (stored answers and lookups).

## Determinism and storage

- **`services/local_store.py`:** model answers (`llm`), lookups (`lookups`), embeddings and paper chunks.
  - Stored in the local Postgres when `RAG_DATABASE_URL` is set (schema `rag`), else in files under `rejecta-api/.cache/rag`.
  - A Supabase address or the production `DATABASE_URL` is refused.
- **Dev DB:** Docker compose at `rejecta-api/docker/rag-db/`; check it with `python -m scripts.rag_db_check`.
- **Stage scripts:**
  - `python -m scripts.ledger_stage paper.pdf --journal "..."` runs the §5 ledger review alone;
  - `scripts/stage_test.py` is the parser stage (the user's own script).

## Where RAG / embeddings are used

Retrieval today is **inside the uploaded paper only**. There is no vector search over outside literature yet.

**The index** (`services/retrieval.py → build_search`, at the start of §5):
- **Units:** each paragraph from `paper_index` (abstract + sections, appendices included) is one chunk.
- **Two rankings, fused:**
  - BM25 keywords (own implementation: stemming, stopwords, Greek letters folded);
  - cosine similarity of `text-embedding-3-small` vectors.

  They are combined by reciprocal rank fusion (k=60). Scores are rounded and ties go to the earlier paragraph, so retrieval is deterministic. Without embeddings (another provider, or a failed call) the BM25 ranking is used alone.
- **Storage:** vectors are cached by text hash in `rag.embeddings`; each paper's chunks and vectors go to `rag.paper_chunks`, keyed by the hash of the paper text. Both are in the local store (Docker Postgres, or files).
- **pgvector:** installed in the Docker image but not used yet. Vectors are `float4[]` and cosine is computed in Python, which is fine for a few hundred chunks per paper.

**Where retrieval is called:**

| Stage | Query | What is retrieved | Used for |
|---|---|---|---|
| §5 audit plan → investigation | each question's claim + rubric search words | top 3 paragraphs per question from **other** sections (own section excluded), ≤12 per call | the section's call sees, for example, the Methods passages for a Results question |
| §5 author defence | finding's problem + claim | the finding's own paragraphs + top 4 from the whole paper | the author's advocate tries to answer the criticism |
| §6 absence audit | only when the numbered paper is over 140k characters: the kind's query + the statement itself | per item, top 6 for each query + the best 2 paragraphs of **every** section, ≤16 | checking "the paper lacks X" without sending the whole paper |

**Where the full text is sent instead of retrieved passages:**
- main analysis (§4);
- consistency extraction (§2);
- claim ledger (§5.3);
- absence audit for normal-length papers (§6);
- the editor letter, which gets only verified facts.

Whole text is used where a missed passage would mean a wrong claim.

**The "knowledge base" side** is local YAML data selected by code: rubrics by field and journal, journal tiers, absence kinds. It is not vector search.

**Not built yet:** the literature knowledge base, i.e. vector search over related papers and journal profiles, for novelty and journal fit with cited ids.

## Knowledge files (editable data, versioned into the report cache key)

| File | Contents |
|---|---|
| `knowledge/rubrics/*.yaml` | Review checks per field and journal: triggers, section, question, search words, severity, fix, source. |
| `knowledge/journals.yaml` | Journal tiers and what each journal decides on. |
| `knowledge/absence_claims.yaml` | Kinds of "lacks X" statements: detection regex, search query, what counts as evidence. |

## Feature flags (`rejecta-api/config.py`)

- `EVIDENCE_LEDGER_ENABLED`
- `ABSENCE_AUDIT_ENABLED`
- `NOVELTY_FROM_CLAIMS`
- `EDITOR_LETTER_FROM_FINDINGS`
- `LLM_RESPONSE_CACHE`, `LLM_RESPONSE_CACHE_DAYS`
- `RAG_STORE`, `RAG_DATABASE_URL`, `RAG_CACHE_DIR`
- `EMBEDDING_MODEL`
- `ANALYSIS_BODY_BUDGET_CHARS`

Unit tests turn the ledger, audit, letter and caches off by default (`tests/conftest.py`); the evidence-ledger, outline/letter and audit tests turn them on.

## Not built yet

- A literature knowledge base for novelty: novelty still comes from the main model's memory.
- The main call as a pure "writer".
- Labelled outcome data to tune thresholds and caps.
- Score calibration against real decisions.

**Novelty, round 3 (prompt 2026-11-16.13).**
- Repeats: code reads every repeat or tuning sentence in the methods and appendices, cuts each at its exception clauses, and lists every count with its clause (`strength.repeats.subsets`). It says "covers all" only when the clause with the count says so and no exception follows. `applies` gives the count for the datasets of each cited figure or table.
- Limits and the skeptic's objection must share a concrete detail (a number, a name, a protocol word) with the sentence they cite; otherwise they are dropped. Code runs design checks on every paper: unequal tuning budgets, unequal repeats, split type, exclusions, and per claim whether tasks or metrics are mixed. When the skeptic field is empty, the first check that has a sentence id fills it (`from_check`).
- "Would be stronger with" must vary the claim's stated cause while holding the rest fixed. For a causal claim, code writes that control when the model's note fails (`from_code`).
- The overall paragraph has three sentences: strongest new claim, strongest overlap, why the verdict follows. A sentence that restates an unverified superlative is dropped, and code writes that sentence instead.
- Evidence includes every float in the paragraph that introduces a named figure or table.
- Prior work needs a shared subject word with the claim. Members of a category the paper defines ("X such as A and B") count as that category.
- A new claim with correlational support and no control is labelled "new (weakly supported)", and so is the verdict.
- `services/ranking_check.py` compares ranking statements in the text with the table numbers, taking the metric direction from the row, header, sentence or caption. It is added to the consistency report as a minor `ranking_mismatch` finding. Bold cells are not in the extracted text, so "best in bold" is not checked.
