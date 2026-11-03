"""Feature keys owned by code. Each maps to API endpoints (seeded in 0004)."""

ANALYSE_PAPER = "analyse_paper"  # POST /analyse-paper, POST /analyse
VOICE_CALIBRATE = "voice_calibrate"  # POST /voice-calibrate
ABSTRACT_FORGE = "abstract_forge"  # POST /abstract-forge/generate
REBUTTAL_WRITER = "rebuttal_writer"  # POST /rebuttal-writer/analyze
AI_HEATMAP = "ai_heatmap"  # POST /ai-signal-heatmap/analyze

ALL_FEATURES = (ANALYSE_PAPER, VOICE_CALIBRATE, ABSTRACT_FORGE, REBUTTAL_WRITER, AI_HEATMAP)
