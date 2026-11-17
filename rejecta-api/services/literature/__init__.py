"""Literature knowledge base: related papers and journal profiles, searched by vectors (pgvector).

P0 (this package so far): the storage schema, the OpenAlex client, the paper-date resolver, the W/J retrieval
snapshots, and a fixed evaluation set with a leakage-safe harness. Nothing here is wired into the analysis
pipeline yet; `settings.LITERATURE_KB_ENABLED` stays False until integration is approved. See README.md.
"""

from __future__ import annotations

# Bumped when a table or a stored payload changes shape. At integration time this (not the size of the knowledge
# base) is what goes into the report cache key; see README.md, "Cache key".
KB_SCHEMA_VERSION = "lit-2026-11-14.1"

# Embedding models the store can hold, with their vector size. One search uses one model's vectors only;
# changing the model means embedding again, never mixing.
EMBEDDING_DIMENSIONS: dict[str, int] = {
    "text-embedding-3-small": 1536,
    "allenai/specter2": 768,
}
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
