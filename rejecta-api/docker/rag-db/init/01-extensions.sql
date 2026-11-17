-- Runs once, when the data volume is first created.
-- pgvector, for the literature knowledge base (similarity search inside Postgres). The paper index of
-- services.local_store creates its own schema "rag" and tables on first use.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS rag AUTHORIZATION rejecta_rag;
