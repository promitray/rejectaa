# Local RAG database (Docker)

A local Postgres (with pgvector) for the evidence-ledger store: the cache of model answers, embeddings, and
each paper's chunk index. Development and testing only. The production database (`DATABASE_URL`, Supabase)
is never used for this, and `services/local_store.py` refuses a Supabase address.

## Start

From `rejecta-api/`:

```bash
# once: a password for the local database (any value; this file is git-ignored)
echo "RAG_DB_PASSWORD=<choose one>" > docker/rag-db/.env

docker compose -f docker/rag-db/docker-compose.yml --env-file docker/rag-db/.env up -d
```

Then in `rejecta-api/.env`:

```
RAG_DATABASE_URL=postgresql://rejecta_rag:<the same password>@localhost:5433/rejecta_local
RAG_STORE=auto
```

The API (and `scripts/ledger_stage.py`) creates schema `rag` and its tables on first use.

## Check

```bash
python -m scripts.rag_db_check          # connects, lists the tables and row counts
docker exec -it rejecta-rag-db psql -U rejecta_rag -d rejecta_local -c "\dt rag.*"
```

## Stop / reset

```bash
docker compose -f docker/rag-db/docker-compose.yml --env-file docker/rag-db/.env down      # stop, keep data
docker compose -f docker/rag-db/docker-compose.yml --env-file docker/rag-db/.env down -v   # stop and delete all data
```

Port 5433 on 127.0.0.1 only, so it does not clash with a Postgres already on 5432 and is not reachable from
other machines.
