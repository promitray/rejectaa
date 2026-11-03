"""Seed the feature catalog and starter plans.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-03

Feature keys map 1:1 to API endpoints and are owned by code: admins can
rename them, edit descriptions and switch them off globally, but not invent
new ones. Plans and limits below are starting values only; admins change
them from the plan management page.

Limits: (enabled, daily_limit, monthly_limit); None = unlimited.
"""

from collections.abc import Sequence

from db.migration_helpers import APP_SCHEMA as S, run_sql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FEATURES = (
    # key, name, description, sort_order
    ("analyse_paper", "Paper analysis",
     "Full pre-submission review of a PDF (POST /analyse-paper and /analyse).", 10),
    ("voice_calibrate", "Voice Calibrator",
     "Calibrate a draft to the target journal's register (POST /voice-calibrate).", 20),
    ("abstract_forge", "Abstract Forge",
     "Three abstract variants: discoverability, clarity, impact (POST /abstract-forge/generate).", 30),
    ("rebuttal_writer", "Rebuttal Writer",
     "Point-by-point response to reviewer comments (POST /rebuttal-writer/analyze).", 40),
    ("ai_heatmap", "AI Signal Heatmap",
     "Stylometric AI-signal heatmap of a manuscript excerpt (POST /ai-signal-heatmap/analyze).", 50),
)

PLANS = (
    # key, name, description, is_default, sort_order
    ("free", "Free", "Default plan for every new account.", True, 10),
    ("researcher", "Researcher", "Individual researcher plan.", False, 20),
    ("lab", "Lab", "Research group plan with unlimited usage.", False, 30),
    ("partner", "Partner", "Server-to-server integrations such as ChemLabFlow.", False, 40),
)

UNLIMITED = (True, None, None)
PLAN_LIMITS = {
    "free": {
        "analyse_paper": (True, 3, None),
        "voice_calibrate": (True, 5, None),
        "abstract_forge": (True, 5, None),
        "rebuttal_writer": (True, 3, None),
        "ai_heatmap": (True, 20, None),
    },
    "researcher": {
        "analyse_paper": (True, None, 20),
        "voice_calibrate": (True, None, 100),
        "abstract_forge": (True, None, 100),
        "rebuttal_writer": (True, None, 50),
        "ai_heatmap": UNLIMITED,
    },
    "lab": {key: UNLIMITED for key, *_ in FEATURES},
    "partner": {key: UNLIMITED for key, *_ in FEATURES},
}


def _literal(value: object) -> str:
    """SQL literal for the small, trusted constants in this file."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _values(rows: Sequence[Sequence[object]]) -> str:
    return ",\n".join("(" + ", ".join(_literal(v) for v in row) + ")" for row in rows)


def upgrade() -> None:
    plan_feature_rows = [
        (plan_key, feature_key, *limits)
        for plan_key, matrix in PLAN_LIMITS.items()
        for feature_key, limits in matrix.items()
    ]
    run_sql(
        [
            f"""
            INSERT INTO {S}.features (key, name, description, sort_order)
            VALUES {_values(FEATURES)}
            """,
            f"""
            INSERT INTO {S}.plans (key, name, description, is_default, sort_order)
            VALUES {_values(PLANS)}
            """,
            f"""
            INSERT INTO {S}.plan_features (plan_id, feature_key, enabled, daily_limit, monthly_limit)
            SELECT p.id, v.feature_key, v.enabled::boolean,
                   v.daily_limit::integer, v.monthly_limit::integer
            FROM (VALUES {_values(plan_feature_rows)})
                 AS v (plan_key, feature_key, enabled, daily_limit, monthly_limit)
            JOIN {S}.plans p ON p.key = v.plan_key
            """,
        ]
    )


def downgrade() -> None:
    plan_keys = ", ".join(_literal(key) for key, *_ in PLANS)
    feature_keys = ", ".join(_literal(key) for key, *_ in FEATURES)
    run_sql(
        [
            # Fails (FK RESTRICT) if users are still on these plans, on purpose.
            f"DELETE FROM {S}.plans WHERE key IN ({plan_keys})",
            f"DELETE FROM {S}.features WHERE key IN ({feature_keys})",
        ]
    )
