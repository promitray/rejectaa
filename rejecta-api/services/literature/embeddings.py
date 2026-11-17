"""Embedding vectors for the literature knowledge base, one model at a time.

- "text-embedding-3-small": the OpenAI provider's `embed` (the analysis already uses it).
- "allenai/specter2": run locally (SPECTER2 base + the proximity adapter), on "title [SEP] abstract", the input the
  model was trained on. Needs `torch`, `transformers` and `adapters`, which are not installed by default: until
  they are, asking for this model raises EmbeddingUnavailable.

Vectors are cached by (model, text) in the local store, so a work or a query is embedded once. A search must use
one model's vectors only (services.literature.store.nearest takes the model).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from services.literature import EMBEDDING_DIMENSIONS
from services.local_store import digest, get_store

logger = logging.getLogger(__name__)

SPECTER2 = "allenai/specter2"
OPENAI_SMALL = "text-embedding-3-small"
MAX_CHARS = 6000
SPECTER_BATCH = 16


class EmbeddingUnavailable(RuntimeError):
    """The model cannot be used here (a missing library or provider)."""


def specter_text(title: str, abstract: str, sep: str = "[SEP]") -> str:
    return f"{title} {sep} {abstract}".strip() if abstract else title


_specter: Any = None


def _load_specter() -> Any:
    global _specter
    if _specter is None:
        try:
            from adapters import AutoAdapterModel  # type: ignore[import-not-found]
            from transformers import AutoTokenizer  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise EmbeddingUnavailable(
                "SPECTER2 needs `pip install torch transformers adapters` (about 2-3 GB)"
            ) from exc
        tokenizer = AutoTokenizer.from_pretrained("allenai/specter2_base")
        model = AutoAdapterModel.from_pretrained("allenai/specter2_base")
        model.load_adapter("allenai/specter2", source="hf", load_as="proximity", set_active=True)
        model.eval()
        _specter = (tokenizer, model)
    return _specter


def _specter_batch(texts: list[str]) -> list[list[float]]:  # pragma: no cover - needs torch
    import torch  # type: ignore[import-not-found]

    tokenizer, model = _load_specter()
    out: list[list[float]] = []
    for start in range(0, len(texts), SPECTER_BATCH):
        batch = texts[start : start + SPECTER_BATCH]
        inputs = tokenizer(batch, padding=True, truncation=True, return_tensors="pt", return_token_type_ids=False, max_length=512)
        with torch.no_grad():
            output = model(**inputs)
        out += [row.tolist() for row in output.last_hidden_state[:, 0, :]]
    return out


async def embed(model: str, texts: list[str], provider: Any = None) -> list[list[float] | None]:
    """Vectors for `texts` with `model`, cached by text. For OpenAI, None per text when the call fails."""
    if model not in EMBEDDING_DIMENSIONS:
        raise ValueError(f"unknown embedding model {model}")
    if not texts:
        return []
    store = get_store()
    clipped = [t[:MAX_CHARS] for t in texts]
    hashes = [digest(model, t) for t in clipped]
    found = await store.get_vectors(model, list(dict.fromkeys(hashes)))
    missing = list(dict.fromkeys(h for h in hashes if h not in found))
    if missing:
        by_hash = dict(zip(hashes, clipped))
        batch = [by_hash[h] for h in missing]
        if model == SPECTER2:
            vectors = await asyncio.to_thread(_specter_batch, batch)
        else:
            if provider is None or not hasattr(provider, "embed"):
                raise EmbeddingUnavailable(f"{model} needs a provider with embeddings")
            try:
                vectors = await provider.embed(batch, model)
            except Exception:  # noqa: BLE001 - keyword ranking still works
                logger.warning("Embedding call failed", exc_info=True)
                return [found.get(h) for h in hashes]
        fresh = {h: [round(float(x), 7) for x in v] for h, v in zip(missing, vectors)}
        await store.put_vectors(model, fresh)
        found.update(fresh)
    return [found.get(h) for h in hashes]


def work_text(model: str, title: str, abstract: str) -> str:
    """The text a work is embedded with, for this model."""
    if model == SPECTER2:
        return specter_text(title, abstract)
    return f"{title}\n\n{abstract}".strip() if abstract else title
