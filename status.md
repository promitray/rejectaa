# Rejecta API — current status

Last checked: 14 Sep 2026. The API lives in `rejecta-api/`. The UI lives in `rejecta-frontend/`.

The API is a FastAPI service (`Rejecta API` v0.1.0) that parses an academic PDF, checks its references, looks up the target journal on OpenAlex, and asks an LLM for a pre-submission critique.

## How to run

```bash
cd rejecta-api
source ../venv/bin/activate
uvicorn main:app --reload
```

- App: `http://localhost:8000`
- Interactive docs: `http://localhost:8000/docs`
- CORS allows the app origin (`localhost:5173` and `https://app.rejecta.ai`)

Frontend, in a second terminal:

```bash
cd rejecta-frontend
npm install
cp .env.example .env
npm run dev
```

Open `http://localhost:5173`. The only API call it makes is `POST /analyse-paper`.

`rejecta-api/.env` is already present. `rejecta-api/.env.example` lists the keys without secrets. Current provider setting is `LLM_PROVIDER=openai` with `OPENAI_MODEL=gpt-4o`. Anthropic and Hugging Face providers exist in code but are not the active one unless you change `.env`.

## What exists

| Area | Status | Notes |
|---|---|---|
| PDF parser | Working | Extracts abstract, intro/methods/results/discussion, references, word count. PDF only, max 10MB. |
| Citation check | Working | CrossRef lookup, with an arXiv fallback. Max 60 references. |
| Journal matcher | Working | OpenAlex source search plus up to 5 recent papers. Fake names return `found=false`. |
| LLM analysis | Working | OpenAI, Anthropic, and Hugging Face adapters. Modes `a` (author) and `b` (reader) use different section schemas. |
| Full pipeline | Working | `POST /analyse-paper` is the only endpoint a frontend should call. |
| Frontend | Working | Vite + React app in `rejecta-frontend/`. Upload, processing, and results pages. No auth or payments. |

### Endpoints

| Method | Path | What you can do |
|---|---|---|
| `GET` | `/health` | Check the process, environment, and active LLM provider. |
| `POST` | `/parse` | Upload a PDF and get structured text. |
| `POST` | `/citations` | Verify a JSON list of reference strings. |
| `POST` | `/journal-match` | Look up a journal name plus a dummy abstract. |
| `POST` | `/analyse` | Send already-extracted text and get an LLM critique. Does not parse a PDF. |
| `POST` | `/analyse-paper` | Upload a PDF, journal name, and mode. Runs the full chain and returns one JSON object. |

`POST /analyse-paper` internally:

1. Validates PDF extension, size, non-empty journal, and mode `a` or `b`.
2. Parses the PDF.
3. Runs citation verification and journal matching at the same time.
4. Calls the configured LLM with that context.
5. Returns `analysis`, `citations`, `journal`, and `meta` (word count, sections found, reference count, mode, `processing_ms`).

## What you can do right now

With both servers running, open `http://localhost:5173`, upload a PDF, pick a journal, and read the critique. Mode `a` is “My paper”. Mode `b` is “Someone else’s paper” and shows strengths and weaknesses instead of issue/fix.

The email field is required on the form but is not sent to the API. The backend has no email endpoint yet.

Without the UI, use Swagger (`/docs`) or curl.

Full analysis of the sample file:

```bash
curl -X POST http://localhost:8000/analyse-paper \
  -F "file=@any_academic_paper.pdf" \
  -F "journal=Nature Machine Intelligence" \
  -F "mode=a"
```

Use `mode=b` for a reader-style review. Mode `a` returns section `issue`/`fix`. Mode `b` returns section `strength`/`weakness`.

Or run the pieces separately: parse a PDF, then send its references to `/citations`, then look up the journal, then call `/analyse` with the extracted text.

## Verified on 14 Sep 2026

- `/journal-match` for Nature Machine Intelligence and PLOS ONE returns `found=true` and recent papers. A made-up journal name returns `found=false`.
- `/analyse-paper` on `any_academic_paper.pdf` completed in about 10–16 seconds for both modes. The letter referred to content from that file (sample size, AUROC), not generic filler.
- A whitespace-only journal name returns HTTP 400 (`Journal name is required`) if the field is actually sent. Curl's `-F "journal= "` drops the field and FastAPI returns 422 instead. Use `--form-string "journal= "` to exercise the 400 path.

The sample PDF is a short fixture (128 words, 3 references). Those references came back `unverified` because they are fictional and have no DOI. That is expected for this file, not a pipeline failure.

## What is not built

- No marketing site, auth, or login. `rejecta.ai` should be a separate landing page. Try now goes to `app.rejecta.ai`. Do not add a login button until accounts exist.
- No saved reports, payments, or file storage. The email field is accepted by the API and not sent or stored. The UI does not persist results across a refresh.
- No background job queue. A long paper can sit on the request until the LLM returns. A 45s+ call is possible; Railway-style request timeouts are not handled yet (ARQ + Redis was the planned follow-up).
- Journal matching is name search only. The abstract is accepted but not used for semantic fit.
- Parser coverage depends on heading patterns. Unusual layouts may miss sections or references.
- README still only lists `/health` and `/analyse`. This file is the current picture.
