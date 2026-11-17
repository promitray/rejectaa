"""Plain data types of the literature knowledge base."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Work:
    """One published work, as read from OpenAlex."""

    id: str  # OpenAlex id, e.g. "W2741809807"
    title: str
    abstract: str = ""
    abstract_missing: bool = True  # many works have no abstract in OpenAlex (publisher restrictions)
    doi: str | None = None
    arxiv_id: str | None = None
    publication_date: dt.date | None = None
    year: int | None = None
    venue_id: str | None = None  # OpenAlex source id, e.g. "S4210178049"
    venue_name: str | None = None
    work_type: str | None = None
    author_ids: list[str] = field(default_factory=list)
    author_names: list[str] = field(default_factory=list)
    institution_ids: list[str] = field(default_factory=list)
    reference_ids: list[str] = field(default_factory=list)  # OpenAlex ids of the works it cites
    related_ids: list[str] = field(default_factory=list)
    concepts: list[str] = field(default_factory=list)
    cited_by_count: int = 0

    def embedding_text(self) -> str:
        """What is embedded: title, and the abstract when there is one (title-only works are flagged)."""
        return f"{self.title}\n\n{self.abstract}".strip() if not self.abstract_missing else self.title

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "abstract": self.abstract, "abstract_missing": self.abstract_missing,
            "doi": self.doi, "arxiv_id": self.arxiv_id,
            "publication_date": self.publication_date.isoformat() if self.publication_date else None,
            "year": self.year, "venue_id": self.venue_id, "venue_name": self.venue_name, "work_type": self.work_type,
            "author_ids": self.author_ids, "author_names": self.author_names, "institution_ids": self.institution_ids,
            "reference_ids": self.reference_ids, "related_ids": self.related_ids, "concepts": self.concepts,
            "cited_by_count": self.cited_by_count,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Work":
        date = data.get("publication_date")
        return cls(
            id=data["id"], title=data.get("title") or "", abstract=data.get("abstract") or "",
            abstract_missing=bool(data.get("abstract_missing", not data.get("abstract"))),
            doi=data.get("doi"), arxiv_id=data.get("arxiv_id"),
            publication_date=dt.date.fromisoformat(date) if date else None, year=data.get("year"),
            venue_id=data.get("venue_id"), venue_name=data.get("venue_name"), work_type=data.get("work_type"),
            author_ids=list(data.get("author_ids") or []), author_names=list(data.get("author_names") or []),
            institution_ids=list(data.get("institution_ids") or []), reference_ids=list(data.get("reference_ids") or []),
            related_ids=list(data.get("related_ids") or []), concepts=list(data.get("concepts") or []),
            cited_by_count=int(data.get("cited_by_count") or 0),
        )


@dataclass(frozen=True)
class PaperDate:
    """The date before which work counts as prior to the uploaded paper.

    `precision` says how much of the date the source gave: "day", "month" or "year". A coarser date is placed at
    the START of its month or year, so later work can never become evidence against the paper.
    """

    date: dt.date
    source: str  # arxiv_stamp | arxiv_id | publication_record | venue_statement | today
    precision: str  # day | month | year
    evidence: str = ""  # the words the date was read from
