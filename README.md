# Rejecta

Pre-submission desk review for academic papers. The product is split on purpose.

| Host | What | In this repo |
|---|---|---|
| `rejecta.ai` | Marketing site. Try now and, later, Log in both go to the app. | Not built yet. Make this separately. |
| `app.rejecta.ai` | The app: upload a PDF, get the critique. | `rejecta-frontend/` |
| `api.rejecta.ai` | The API the app calls. | `rejecta-api/` |

Do not put the app on `rejecta.ai`, and do not add a login button until accounts exist. Until then the landing page only needs **Try now** → `https://app.rejecta.ai`.

## Get started locally

Two terminals. The app talks to the API on port 8000. CORS already allows `http://localhost:5173` and `https://app.rejecta.ai`.

API:

```bash
cd rejecta-api
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# then set OPENAI_API_KEY or ANTHROPIC_API_KEY, and LLM_PROVIDER
uvicorn main:app --reload
```

The existing virtualenv at the repo root still works: `source ../venv/bin/activate` from `rejecta-api/`.

App:

```bash
cd rejecta-frontend
npm install
cp .env.example .env
npm run dev
```

Open `http://localhost:5173`. API docs are at `http://localhost:8000/docs`.

If `.env` already exists, do not overwrite it. `LLM_PROVIDER` is `openai`, `anthropic`, or `huggingface`.

## What the app calls

The UI only calls `POST /analyse-paper` with multipart fields `file`, `journal`, `mode` (`a` or `b`), and `email`.

`email` is accepted so the form and the API match. It is not stored or sent yet. Other endpoints (`/health`, `/parse`, `/citations`, `/journal-match`, `/analyse`) are for debugging.
