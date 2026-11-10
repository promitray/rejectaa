"""Metrics used by scripts/eval_scoring.py to judge scoring accuracy."""

from scripts import eval_metrics as m


def _paper(pid, label, **expect):
    return {"id": pid, "label": label, "group": "B", "journal": "J", "expect": expect}


def _runs(*scores, desk=(), criteria=None):
    return [
        {"score": s, "desk_reject": i in desk, "criteria": criteria or {"novelty": s}, "ghost": 0,
         "verified": 1, "total": 1, "journal_name": "J"}
        for i, s in enumerate(scores)
    ]


def test_auc_perfect_reversed_and_ties():
    assert m.auc([80, 90], [30, 40]) == 1.0
    assert m.auc([30], [80]) == 0.0
    assert m.auc([50], [50]) == 0.5
    assert m.auc([], [1]) is None


def test_desk_reject_needs_a_majority_of_runs():
    paper = _paper("p", m.STRONG)
    assert m.paper_record(paper, {}, _runs(70, 72, 71, desk=(0,)))["desk_reject"] is False
    assert m.paper_record(paper, {}, _runs(40, 42, 41, desk=(0, 2)))["desk_reject"] is True


def test_record_median_spread_and_criteria():
    rec = m.paper_record(_paper("p", m.STRONG), {}, _runs(60, 70, 66))
    assert rec["median_score"] == 66 and rec["spread"] == 10
    assert rec["criteria"]["novelty"] == 66


def test_parser_checks_only_for_given_expectations():
    paper = _paper("p", m.STRONG, article_type="research_article", references=40, has_methods=True)
    rec = m.paper_record(paper, {"article_type": "research_article", "references": 30, "section_roles": ["results"]}, [])
    assert rec["article_type_ok"] is True
    assert rec["reference_recall"] == 0.75
    assert rec["methods_found"] is False
    bare = m.paper_record(_paper("q", m.WEAK), {"references": 3}, [])
    assert "reference_recall" not in bare and "methods_found" not in bare


def test_reference_recall_is_capped_at_one():
    rec = m.paper_record(_paper("p", m.STRONG, references=10), {"references": 14}, [])
    assert rec["reference_recall"] == 1.0


def test_journal_match_is_case_insensitive():
    paper = _paper("p", m.STRONG, journal_match="Chemical Communications")
    runs = _runs(70)
    runs[0]["journal_name"] = "chemical communications"
    assert m.paper_record(paper, {}, runs)["journal_match_ok"] is True
    runs[0]["journal_name"] = "Structural Chemistry & Crystallography Communication"
    assert m.paper_record(paper, {}, runs)["journal_match_ok"] is False


def test_summarize_headline_metrics():
    records = [
        m.paper_record(_paper("s1", m.STRONG), {}, _runs(80, 82, 81)),
        m.paper_record(_paper("s2", m.STRONG), {}, _runs(40, 41, 39, desk=(0, 1, 2))),
        m.paper_record(_paper("w1", m.WEAK), {}, _runs(30, 50, 35)),
        m.paper_record(_paper("o1", m.OUT_OF_SCOPE), {}, _runs(40, 40, 40, desk=(0, 1))),
    ]
    s = m.summarize(records)
    assert s["false_desk_reject_rate"] == 0.5
    assert s["strong_median_score"] == 60.5
    assert s["strong_share_at_least_60"] == 0.5
    assert s["auc_strong_vs_weak"] == 1.0
    assert s["median_gap_strong_weak"] == 60.5 - 35
    assert s["spread_ok_share"] == 0.75  # the weak paper moved 20 points
    assert s["out_of_scope_desk_reject_rate"] == 1.0
    assert s["false_ghosts_strong"] == 0


def test_summarize_without_runs_leaves_score_metrics_empty():
    rec = m.paper_record(_paper("p", m.STRONG, references=5), {"references": 4}, [])
    s = m.summarize([rec])
    assert s["strong_median_score"] is None and s["auc_strong_vs_weak"] is None
    assert s["reference_recall"] == 0.8


def test_meets_and_compare_flag_only_moves_past_tolerance():
    target = m.TARGETS_BY_KEY["strong_median_score"]
    assert m.meets(target, 70) is True and m.meets(target, 69) is False and m.meets(target, None) is None
    rows = {r["key"]: r for r in m.compare(
        {"strong_median_score": 70, "false_desk_reject_rate": 0.10},
        {"strong_median_score": 68.5, "false_desk_reject_rate": 0.20},
    )}
    assert rows["strong_median_score"]["regressed"] is False  # within the 2-point tolerance
    assert rows["false_desk_reject_rate"]["regressed"] is True
    assert rows["auc_strong_vs_weak"]["regressed"] is None


def test_section_name_stability_is_the_mean_overlap_between_runs():
    runs = _runs(70, 71, 70)
    for run, names in zip(runs, (["Introduction", "Results and discussion"], ["Introduction", "Results and discussion"], ["Introduction", "Mechanism"])):
        run["sections"] = names
    rec = m.paper_record(_paper("p", m.STRONG), {}, runs)
    assert round(rec["section_stability"], 3) == round((1 + 1 / 3 + 1 / 3) / 3, 3)
    assert m.summarize([rec])["section_name_stability"] == rec["section_stability"]
