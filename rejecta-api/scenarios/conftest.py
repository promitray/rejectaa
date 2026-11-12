"""Pytest configuration and fixtures for Rejecta Scenario evaluation."""

from __future__ import annotations

import csv
import os
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

# Ensure rejecta-api root is on sys.path
API_ROOT = Path(__file__).resolve().parents[1]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

# Primary local test papers directory and CI fallback
PRIMARY_TEST_PAPERS_DIR = Path(r"D:\rejecta test papaer\test_papers")
FALLBACK_TEST_PAPERS_DIR = API_ROOT / "eval" / "papers"


@pytest.fixture(scope="session")
def test_papers_dir() -> Path:
    """Resolve directory containing academic test PDFs."""
    env_dir = os.environ.get("TEST_PAPERS_DIR")
    if env_dir and Path(env_dir).exists():
        return Path(env_dir)
    if PRIMARY_TEST_PAPERS_DIR.exists():
        return PRIMARY_TEST_PAPERS_DIR
    if FALLBACK_TEST_PAPERS_DIR.exists():
        return FALLBACK_TEST_PAPERS_DIR
    raise FileNotFoundError(
        f"No valid test papers directory found. Checked '{env_dir}', "
        f"'{PRIMARY_TEST_PAPERS_DIR}', and '{FALLBACK_TEST_PAPERS_DIR}'."
    )


@pytest.fixture(scope="session")
def manifest_entries(test_papers_dir: Path) -> list[dict[str, Any]]:
    """Load metadata and ground-truth expectations for test papers."""
    manifest_csv = test_papers_dir / "manifest.csv"
    entries: list[dict[str, Any]] = []

    if manifest_csv.exists():
        with open(manifest_csv, encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                file_name = row.get("file", "").strip()
                if not file_name or file_name == "MANUAL_DOWNLOAD":
                    continue
                # Determine category subfolder
                label = row.get("label", "").strip()
                if label == "published":
                    target_path = test_papers_dir / "published" / file_name
                elif label == "retracted":
                    target_path = test_papers_dir / "retracted" / file_name
                elif label == "preprint_unpublished":
                    target_path = test_papers_dir / "preprint_unpublished" / file_name
                else:
                    target_path = test_papers_dir / file_name

                if target_path.exists():
                    entries.append({
                        "id": Path(file_name).stem,
                        "file_path": target_path,
                        "file_name": file_name,
                        "label": label,
                        "tier": row.get("tier", "unknown"),
                        "title": row.get("title", ""),
                        "journal": row.get("journal", "General Chemistry"),
                        "year": row.get("year", ""),
                        "doi": row.get("doi", ""),
                    })

    # Also load from eval/manifest.yaml if available
    eval_manifest = API_ROOT / "eval" / "manifest.yaml"
    if eval_manifest.exists():
        data = yaml.safe_load(eval_manifest.read_text(encoding="utf-8")) or {}
        for p in data.get("papers", []):
            pdf_path = API_ROOT / "eval" / "papers" / p.get("file", "")
            if pdf_path.exists():
                entries.append({
                    "id": p.get("id"),
                    "file_path": pdf_path,
                    "file_name": p.get("file"),
                    "label": p.get("label", "strong"),
                    "tier": "high",
                    "title": p.get("id", ""),
                    "journal": p.get("journal", "Nature"),
                    "year": "2024",
                    "doi": "",
                    "expect": p.get("expect", {}),
                })

    return entries

