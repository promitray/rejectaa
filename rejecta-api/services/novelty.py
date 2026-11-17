"""The novelty assessment: what is genuinely new in this paper, judged against what the paper itself says earlier
work did. It does not summarise the paper.

Part A, per claim of the claim ledger (services.claim_ledger):
1. The claim and its scope (which data, models, materials, conditions it covers).
2. Prior work by the paper's own account, from the Introduction, Related Work and wherever citations appear. Every
   prior-work sentence is applied to ALL claims within its scope: a sentence about a category ("non-deep models",
   "GNNs") also covers claims about any member of it. Relation: builds_on / challenges / reuses_data_from /
   same_finding; a sentence where the paper contradicts or questions earlier work is "challenges", never
   "builds_on". An explanation resting on a known theory the paper cites: the novelty is the application or test.
3. The delta: what exactly is new compared with that prior work, named concretely.
4. Evidence kind: new_experiment / reanalysis_of_existing_results / new_explanation / new_scope. A claim supported
   only by re-reading a table or figure already used for another claim is reanalysis. Correlational support is
   called correlational.
5. Strength of the evidence for the new part only: how many datasets / samples / compounds / conditions; a control,
   ablation or counter-test; variance, repeats or statistical tests.
Plus: superlatives the paper uses about itself are unverified unless the paper compares itself with cited earlier
work; one verdict (new / incremental / overlapping) with a reason. Every sentence cites sentence ids [P..] or a
figure/table with its section and page; no vague praise.

How it is enforced: one model call returns the assessment as data; code keeps only what it can check:
- every id must exist; a named citation must be in the sentences cited for it; a prior-work sentence's category
  ("covers") must be words of those sentences;
- a prior-work sentence is applied by code to every claim whose statement names its category, when the model left
  that claim out (marked `by_scope`);
- "builds_on" or "same_finding" on a sentence that contradicts or questions the earlier work becomes "challenges";
- the figure or table showing the delta must be in the outline, and is given with its section and page;
- "new_experiment" on a figure or table already used for an earlier claim becomes reanalysis;
- "causal" support without a control, ablation or counter-test becomes correlational;
- a count ("14 datasets") is kept only if the number is in its cited sentences;
- vague deltas and reasons ("new insights", "valuable contribution") are dropped;
- each claim's status and the verdict are decided by code.
The text shown to the reader is written by code from the checked data.

This judges novelty against the paper's own account of earlier work only. Work the paper does not cite is the job
of the literature knowledge base (services/literature, not yet wired in), and the report says so.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from models import AnalysisResult
from services.analysis_guard import AnalysisContext, alnum
from services.paper_index import PaperIndex, ids_in, sort_key

logger = logging.getLogger(__name__)

NOVELTY_PROMPT_VERSION = "2026-11-16.13"
FULL_PAPER_CHARS = 140_000
MAX_CLAIMS = 8
MAX_PRIOR = 16
RESPONSE_MAX_TOKENS = 9000
RELATIONS = ("same_finding", "builds_on", "challenges", "reuses_data_from")
EVIDENCE_KINDS = ("new_experiment", "reanalysis_of_existing_results", "new_explanation", "new_scope", "none")
SUPPORT = ("causal", "correlational", "descriptive")
CONTRIBUTION_TYPES = ("method", "dataset", "benchmark", "empirical_finding", "explanation", "tool", "theory",
                      "application")
# "new" is judged against the earlier work the paper cites; work it does not cite is not searched
VERDICT_LABELS = {"new": "new (vs cited work)", "incremental": "incremental", "overlapping": "overlapping"}
_SUPERLATIVE = re.compile(
    r"(?i)\b(for the first time|the first|first (?:to|time|study|report|demonstration)|most comprehensive|"
    r"state[- ]of[- ]the[- ]art|unprecedented|largest|best[- ]performing|outperform\w* all|novel|pioneering)\b"
)
_CITES = re.compile(r"et al\.|\[\d{1,3}(?:[,–-]\s*\d{1,3})*\]|\([A-Z][^()]{0,60}(?:19|20)\d{2}[a-z]?\)")
_CITATION_PART = re.compile(r"[A-Z][A-Za-z\-']+|\d{1,4}")
_CHALLENGES = re.compile(
    r"(?i)\b(contrary to|contradict\w*|challeng\w*|call\w* into question|question\w*|disagree\w*|inconsistent with|"
    r"refut\w*|in contrast to|at odds with|overturn\w*|fail\w* to (?:hold|replicate|generali[sz]e))\b"
)
_VAGUE = re.compile(
    r"(?i)\b(?:new|novel|valuable|important|significant|deeper|fresh) (?:insights?|understanding|perspectives?)\b|"
    r"\bsheds? (?:new )?light\b|\bvaluable contribution\b|\bimportant contribution\b|"
    r"\b(?:may|might|could) not (?:generali[sz]e|apply|hold|account for all)\b|\bgenerali[sz]ab\w*\b|"
    r"\b(?:wider|broader) (?:range|variety|set)\b|\b(?:may|might|could) vary\b|\bdataset-specific\b|"
    r"\blimited to the [\w ,]{0,40}?(?:tested|used|studied|considered|examined)\b|"
    r"\bfurther (?:analysis|research|studies|testing|investigation|work|theoretical)\b|"
    r"\b(?:testing|test|including|use) (?:additional|more|other) (?:datasets?|models?|experiments?)\b"
)
_MANIPULATES = re.compile(
    r"(?i)\b(transform\w*|smooth\w*|perturb\w*|shuffl\w*|permut\w*|remov\w*|replac\w*|nois\w*|mask\w*|"
    r"modif\w*|manipulat\w*|augment\w*|corrupt\w*|swap\w*|delet\w*|ablat\w*|intervention|inject\w*|"
    r"rotat\w*|orthogonal\w*|decreas\w* the level|increas\w* the level)\b"
)
_VARIANCE = re.compile(
    r"(?i)(±|\+/-|\bstandard (?:deviation|error)s?\b|\bs\.?d\.?\b|\bs\.?e\.?m?\.?\b|\berror bars?\b|\bconfidence interval|"
    r"\b\d+\s*%?\s*CI\b|\bvarian\w*|\bp\s*[<=>]|\bp-values?\b|\bsignifican\w*|\b(?:t|wilcoxon|mann|anova|chi)[- ]?\w*\s*tests?\b|"
    r"\bseeds?\b|\b(?:independent |repeated )?runs\b|\brepeat\w*|\breplicat\w*|\btriplicate|\bduplicate|\bn\s*=\s*\d)"
)
_STOP = {"the", "a", "an", "of", "on", "in", "for", "and", "or", "to", "with", "by", "as", "at", "from", "its",
         "their", "that", "this", "these", "those", "is", "are", "be", "using", "based"}

_SYSTEM = (
    "You are a senior reviewer assessing what is genuinely new in a manuscript. You do not summarise it. You use only "
    "the manuscript given, cite the ids of the sentences you rely on, and never claim anything about work the "
    "manuscript does not mention. Return ONLY valid JSON."
)

_RULES = """Assess what is genuinely new, claim by claim. Do not summarise the paper. Never write vague praise ("provides
new insights", "valuable contribution") without naming the specific insight and where it is shown. Use only this
paper; every statement cites the ids of the sentences it rests on.

PART A, for each claim listed below:

1. CLAIM, SCOPE AND TYPE. The data, models, materials and conditions the claim covers, with ids. Contribution type,
   one of: method / dataset / benchmark / empirical_finding / explanation / tool / theory / application.

2. PRIOR WORK BY THE PAPER'S OWN ACCOUNT. Search the Introduction, Related Work and wherever citations appear for
   earlier work the paper says did the same or a similar thing. List each such sentence ONCE in "prior_work" with
   the citations exactly as printed, the subject it is about in the paper's own words ("covers"), and every claim it
   applies to. A sentence applies to a claim only if it is about the same subject AND the same assertion:
   - a sentence about an outcome does not cover a claim about the cause of that outcome;
   - a sentence that names a category covers claims about members of that category (as the paper defines them).
   Relation per claim:
     same_finding      - the paper says earlier work already reported this finding;
     builds_on         - the paper extends or uses that work;
     challenges        - the paper questions, contradicts or limits that earlier work or belief (never builds_on);
     reuses_data_from  - the paper uses that work's data, datasets or results.
   If an explanation rests on a known theory the paper cites, give it under "theory" with the id of the sentence
   that cites it: the novelty is the application or test, not the theory. That application or test IS the delta:
   still describe it under "delta". An experiment the paper designs is new unless the paper says earlier work ran it.

3. THE DELTA. What exactly is new compared with that prior work, in one or two specific sentences naming the
   concrete difference ("adds <which models / materials / conditions / analyses> that the cited earlier work did not
   include"), with ids. List ALL figures and tables the claim's sentences or the experiment's paragraph refer to,
   as printed. If nothing is new, leave the delta empty.

4. EVIDENCE KIND: new_experiment / reanalysis_of_existing_results / new_explanation / new_scope. A claim supported
   only by re-reading a table or figure already used for another claim is reanalysis_of_existing_results. Support:
   "causal" only with a control, ablation or counter-test that isolates the cause; "correlational" if it rests on
   co-occurrence; "descriptive" for a measured result.

5. INTERVENTIONS. If the claim rests on an intervention (a manipulation of the data, labels, inputs or conditions):
   - "target": what it changes: "labels", "inputs" (the compared models' own inputs) or "other";
   - "representation": which representation or quantity defines it, with the ids of the sentence that defines it;
   - "models": the compared models that take input from the same family of representation, with the ids of the
     sentence that says so;
   - if the target is the models' own inputs, it is not itself a confound: say under "side_effects" whether it also
     changes other properties of the data (sparsity, scale, type, dimensionality), with the ids of its definition.

6. STRENGTH OF EVIDENCE. Read the cited figures and tables, their captions and the methods.
   - counts: datasets / targets / compounds / samples, the number as printed, ids or the figure/table ("source");
   - control, ablation or counter-test: what, ids; or empty;
   - repeats: runs / seeds / replicates / folds stated (usually in the methods), ids. Read the whole passage,
     exception clauses included: where different subsets of datasets or models get different repeat counts or
     tuning budgets, list each subset with its own count. Say "all" only if the clause with the count says so
     and no exception follows. Give the count that applies to the datasets of the cited figure or table;
   - spread: whether spread values (standard deviations, errors, intervals) are shown, ids. A pointer ("can be found
     in the appendix") is not a value: give its id under "pointer_ids".

7. DEPTH. For each claim:
   - limits and skeptic: each must cite a concrete detail of THIS paper (a number, a protocol choice, a subset,
     an exclusion) in the sentence whose id it gives. Check systematically: unequal tuning or compute budgets
     between compared models; the split type; differences in task, metric or scale between the datasets
     compared; models or datasets left out. An objection that would fit any paper is not accepted;
   - strengthen: the control that isolates the claim's stated cause: vary the suspected cause while holding
     everything else fixed. Not a list of other factors to add.
   Generic notes ("may not generalise", "test more datasets", "further research") are not accepted; leave a field
   empty rather than write one.
   Fill "intervention" only when the claim rests on a manipulation the paper performs; leave it out otherwise.

8. SUPERLATIVES. Every superlative the paper uses about itself ("first", "most comprehensive", "state of the art",
   "novel", "outperforms all"): its id; the ids of sentences where the paper compares itself with earlier work it
   cites in a way that shows it, or none (a sentence describing the study itself is not evidence); the axes that
   comparison covers: models, descriptors, datasets, metrics.

PART B:
9. OVERALL, three sentences, each with its ids: "strongest_new" (the strongest new claim and its evidence),
   "strongest_overlap" (the strongest overlap with cited work), "why_verdict" (why the verdict follows). Do not
   restate a superlative the paper gives no comparison for.
10. VERDICT: "new", "incremental" or "overlapping", with one sentence of reason and its ids. "new" needs a new
   experiment or a new explanation that no earlier work the paper cites already supports; re-reading an existing
   table is at most "incremental".

Return JSON of this shape. Angle brackets are placeholders: fill every value from THIS paper, never copy them.
{"prior_work": [{"ids": ["<id>"], "citations": ["<citation as printed>"], "covers": "<subject, paper's words>",
     "applies_to": [{"claim": "<C id>", "relation": "same_finding|builds_on|challenges|reuses_data_from"}]}],
 "claims": [{"claim": "<C id>",
     "scope": {"text": "<data, models, materials, conditions>", "ids": ["<id>"]},
     "contribution_type": "method|dataset|benchmark|empirical_finding|explanation|tool|theory|application",
     "theory": {"ids": ["<id of the sentence citing it>"], "note": "<citation as printed>"},
     "delta": {"text": "<the concrete difference>", "ids": ["<id>"], "shown_in": ["<every Figure/Table as printed>"]},
     "evidence_kind": "new_experiment|reanalysis_of_existing_results|new_explanation|new_scope|none",
     "support": "causal|correlational|descriptive",
     "intervention": {"target": "labels|inputs|other", "representation": {"text": "<...>", "ids": ["<id>"]},
                      "models": {"names": ["<model as printed>"], "ids": ["<id>"]},
                      "side_effects": {"text": "<...>", "ids": ["<id>"]}},
     "strength": {"counts": [{"n": "<number as printed>", "what": "<datasets/targets/compounds/samples>", "ids": ["<id>"], "source": "<Figure/Table>"}],
                  "control": {"text": "<control, ablation or counter-test>", "ids": ["<id>"]},
                  "repeats": {"text": "<runs/seeds/replicates as stated>", "ids": ["<id>"], "covers": ["<Figure/Table or all experiments>"]},
                  "spread": {"text": "<spread values as shown>", "ids": ["<id>"], "pointer_ids": ["<id>"]}},
     "limits": {"text": "<...>", "ids": ["<id>"]},
     "skeptic": {"text": "<...>", "ids": ["<id>"]},
     "strengthen": {"text": "<...>", "ids": ["<id>"]}}],
 "superlatives": [{"ids": ["<id>"], "text": "<phrase>", "evidence_ids": ["<id>"], "axes_backed": ["models|descriptors|datasets|metrics"]}],
 "overall": {"strongest_new": {"claim": "<C id>", "text": "<one sentence>", "ids": ["<id>"]},
             "strongest_overlap": {"claim": "<C id>", "text": "<one sentence>", "ids": ["<id>"]},
             "why_verdict": {"text": "<one sentence>", "ids": ["<id>"]}},
 "verdict": "new|incremental|overlapping", "reason": "<one sentence>", "reason_ids": ["<id>"]}
Leave a list empty, or a text empty with no ids, when the paper gives nothing for it."""


def _clean(value: Any, limit: int = 400) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().strip("\"“”")[:limit]


def _ids(value: Any, index: PaperIndex) -> list[str]:
    return sorted({i for i in ids_in(value) if i in index.sentences}, key=sort_key)


def _citation_in(citation: str, text: str) -> bool:
    """A citation as printed ("Mayr et al., 2018", "[12]") whose name and year (or number) are in the sentences."""
    parts = [p for p in _CITATION_PART.findall(citation) if p.lower() not in {"et", "al", "and"}]
    if not parts:
        return False
    hay = alnum(text)
    return all(alnum(p) in hay for p in parts)


def _ref(ids: list[str]) -> str:
    return f" [{', '.join(ids[:4])}]" if ids else ""


def _numbered(index: PaperIndex) -> str:
    whole = "\n\n".join(f"## {name.upper()}\n{index.numbered(name)}" for name in index.sections)
    if len(whole) <= FULL_PAPER_CHARS:
        return whole
    share = FULL_PAPER_CHARS // max(1, len(index.sections))
    return "\n\n".join(f"## {name.upper()}\n{index.numbered(name, share)}" for name in index.sections)


def code_superlatives(index: PaperIndex, roles: dict[str, str]) -> list[str]:
    """Sentence ids where the paper praises itself, in the abstract, introduction and conclusion."""
    wanted = {"abstract"} | {name for name, role in roles.items() if role in ("introduction", "discussion")}
    out: list[str] = []
    for sid, sentence in index.sentences.items():
        if sentence.section in wanted and _SUPERLATIVE.search(sentence.text) and re.search(
            r"(?i)\b(we|our|this (?:study|work|paper))\b", sentence.text
        ):
            out.append(sid)
    return sorted(out, key=sort_key)


def place_key(text: str) -> str:
    """'Fig. 2', 'Figure 2' and 'figure 2a' the same; 'Sec. 3.2' and 'Section 3.2' the same."""
    t = re.sub(r"(?i)^\s*fig(?:ure)?s?\.?\s*", "figure ", text)
    t = re.sub(r"(?i)^\s*(sec\.|§)\s*", "section ", t)
    return re.sub(r"(?<=\d)[a-z]$", "", alnum(t))


def known_places(ctx: AnalysisContext) -> set[str]:
    """Figures, tables, schemes, numbered sections and subsections, and section titles in the file's outline."""
    out = {place_key(f["label"]) for f in (ctx.figures or []) if f.get("label")}
    for label in list((ctx.section_labels or {}).values()) + [str(s.get("label") or "") for s in (ctx.subsections or [])
                                                              if isinstance(s, dict)]:
        if label:
            out |= {place_key(f"section {label}"), place_key(f"appendix {label}")}
    out |= {place_key(t) for t in (ctx.section_titles or {}).values() if t}
    return out


def _phrase(text: str) -> str:
    match = _SUPERLATIVE.search(text)
    return re.sub(r"[- ]+", " ", match.group(0).lower()) if match else ""


_AXES = {
    "models": re.compile(r"(?i)\b(models?|architectures?|methods?|baselines?|networks?|transformers?|gnns?|algorithms?)\b"),
    "descriptors": re.compile(r"(?i)\b(descriptors?|fingerprints?|representations?|graphs?|3d|features?|embeddings?)\b"),
    "datasets": re.compile(r"(?i)\b(datasets?|tasks?|targets?|splits?|compounds?|molecules|samples?)\b"),
    "metrics": re.compile(r"(?i)\b(metrics?|auc\w*|rmse|mae|accuracy|f1|r2|scores?)\b"),
}


def _superlatives(raw: dict[str, Any], index: PaperIndex, ctx: AnalysisContext) -> list[dict[str, Any]]:
    """Each superlative the paper uses about itself, once per phrase with every sentence that uses it. Backed only
    when the evidence is the paper comparing itself with earlier work it cites (a sentence describing the study
    itself is not evidence), and then only on the axes that comparison names (models, descriptors, datasets,
    metrics); the axes it does not cover are listed."""
    groups: dict[str, dict[str, Any]] = {}

    def add(ids: list[str], text: str, evidence: list[str], axes: list[str]) -> None:
        key = _phrase(index.text_of(ids)) or _phrase(text) or alnum(text)[:40]
        if not key:
            return
        group = groups.setdefault(key, {"ids": [], "text": _phrase(index.text_of(ids)) or text, "evidence_ids": [],
                                        "claimed_axes": set()})
        group["ids"] = sorted(set(group["ids"]) | set(ids), key=sort_key)
        group["evidence_ids"] = sorted(set(group["evidence_ids"]) | set(evidence), key=sort_key)
        group["claimed_axes"] |= set(axes)

    for s in raw.get("superlatives") or []:
        if isinstance(s, dict) and (ids := _ids(s.get("ids"), index)):
            evidence = [i for i in _ids(s.get("evidence_ids"), index) if i not in ids]
            axes = [a for a in (str(x).lower() for x in s.get("axes_backed") or []) if a in _AXES]
            add(ids, _clean(s.get("text"), 120), [i for i in evidence if _CITES.search(index.sentences[i].text)], axes)
    for sid in code_superlatives(index, ctx.section_roles):
        if not any(sid in g["ids"] for g in groups.values()):
            add([sid], "", [], [])
    out = []
    for g in groups.values():
        evidence = [i for i in g.pop("evidence_ids") if i not in g["ids"]]
        named = {axis for axis, pattern in _AXES.items() if pattern.search(index.text_of(evidence))} if evidence else set()
        claimed = g.pop("claimed_axes")
        backed = sorted(named & claimed) if named & claimed else sorted(named)
        out.append({**g, "evidence_ids": evidence, "supported": bool(evidence and backed),
                    "axes_backed": backed if evidence else [],
                    "axes_not_covered": [a for a in _AXES if a not in backed] if evidence and backed else []})
    return sorted(out, key=lambda g: sort_key(g["ids"][0]))


def _words(text: str) -> set[str]:
    """Content words, hyphens joined and plurals cut: 'non-deep models' -> {'nondeep', 'model'}."""
    out: set[str] = set()
    for w in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", (text or "").lower()):
        w = w.replace("-", "")
        if w in _STOP or len(w) < 2:
            continue
        out.add(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w)
    return out


def _within(covers: str, statement: str, prior_text: str) -> bool:
    """The claim names the category the prior-work sentence is about AND is about the same thing: besides the
    category, at least two of its words (and a quarter of them) are in that sentence. A category as broad as
    "deep models" alone would tie every claim to every sentence about deep models."""
    need = _words(covers)
    claim = _words(statement)
    if not need or not need <= claim:
        return False
    rest = claim - need
    shared = rest & _words(prior_text)
    return len(shared) >= 2 and len(shared) >= 0.25 * len(rest)


_MENTION = re.compile(r"(?i)\b(fig(?:ure)?s?\.?|tables?|schemes?)\s*([A-Z]?\d+|[A-Z]\b)[a-z]?")
_POINTER = re.compile(
    r"(?i)\b(?:can be (?:seen|found)|is|are|see|refer to|available|given|provided|shown|reported|deferred|moved|"
    r"placed)\b[^.]{0,40}\b(?:in|at|to)\s+(?:the\s+)?(?:online\s+)?"
    r"(?:appendix|appendices|supplementary|supplemental|supporting information|esi|\bsi\b|extended data)"
)
_SHOWS_VARIANCE = re.compile(r"(?i)±|\+/-|\d\s*\(\s*\d*\.\d+\s*\)|\bstd\b[^.]{0,30}\d|standard deviations? (?:of|is|are|were|was)\s*\d"
                             r"|error bars? (?:show|represent|indicate|denote)")
_CITATION = re.compile(
    r"[A-Z][A-Za-z'\-]+(?: (?:&|and) [A-Z][A-Za-z'\-]+| et al\.)?(?:,\s*|\s+\()(?:19|20)\d{2}[a-z]?\)?"
    r"|\[\d{1,3}(?:[,–-]\s*\d{1,3})*\]"
)
_REPEATS = re.compile(
    r"(?i)\b(?:\d+|two|three|four|five|six|seven|eight|nine|ten)\s+(?:independent\s+|random\s+|different\s+)?"
    r"(?:runs|seeds|repeats|repetitions|trials|replicates|folds|times)\b|\brandom seeds?\b|\brepeated\b|\breplicat\w*"
    r"|\btriplicate|\bduplicate\b|cross[- ]validation"
)
_ERROR_BARS = re.compile(
    r"(?i)error bars?|shaded (?:area|region|band)s?|whiskers?|confidence (?:band|interval)s?|±|standard deviations?|"
    r"standard errors?|\bs\.d\.|\bs\.e\.m?\."
)
_ALL_EXPERIMENTS = re.compile(r"(?i)\ball (?:the )?(?:experiments|results|runs|models|evaluations)\b|\beach (?:experiment|model|dataset)\b")
_PANELS = re.compile(r"(?:\b(?:Left|Middle|Centre|Center|Right|Top|Bottom|Upper|Lower)\s*:|\(([a-h])\)\s*)\s*([A-Z][\w\-+]*)")
_ENUMERATED = re.compile(r"(?i)\b(datasets?|targets?|tasks?|proteins?|compounds?|systems?|materials?|benchmarks?)\b")
_PROPERTIES = re.compile(r"(?i)\b(spars\w*|dense|density|scale\w*|range|type|binary|continuous|discrete|integer|"
                         r"dimension\w*|distribution|magnitude)\b")
_FAMILIES = {
    "fingerprints": re.compile(r"(?i)fingerprints?|\becfp\w*|\bfcfp\w*|\bmorgan\b|\bmaccs\b|tanimoto"),
    "molecular graphs": re.compile(r"(?i)\bgraphs?\b"),
    "SMILES strings": re.compile(r"(?i)\bsmiles\b"),
    "3D structures": re.compile(r"(?i)\b3d\b|conformers?|coordinates"),
    "descriptors": re.compile(r"(?i)\bdescriptors?\b"),
    "images": re.compile(r"(?i)\bimages?\b|pixels?"),
    "sequences": re.compile(r"(?i)\bsequences?\b"),
}
_SPECIFIC = re.compile(r"(?i)\b(ecfp\w*|fcfp\w*|morgan|maccs|atom[- ]pairs?|smiles|selfies|rdkit|mordred|3d|2d)\b")


def _order(ctx: AnalysisContext) -> list[str]:
    """Section keys in reading order, with a start page."""
    return [k for k, _ in sorted(((k, v) for k, v in (ctx.section_pages or {}).items() if v), key=lambda kv: kv[1])]


def _section_name(key: str, ctx: AnalysisContext) -> str:
    """'Appendix A: title' or 'Section 2: title', as printed."""
    label = (ctx.section_labels or {}).get(key) or ""
    title = (ctx.section_titles or {}).get(key) or key
    if (ctx.section_parts or {}).get(key) == "appendix":
        return f"Appendix {label}: {title}" if label else f"Appendix: {title}"
    return f"Section {label}: {title}" if label else title


def _appendix_on_page(page: int, ctx: AnalysisContext) -> str | None:
    """The appendix printed on that page, if one runs there."""
    order = _order(ctx)
    pages = ctx.section_pages or {}
    for n, k in enumerate(order):
        end = pages[order[n + 1]] if n + 1 < len(order) else 10_000
        if (ctx.section_parts or {}).get(k) == "appendix" and pages[k] <= page <= end:
            return k
    return None


def _label_pattern(label: str) -> str:
    kind, _, number = label.partition(" ")
    head = r"Fig(?:ure|s?\.)?" if kind.lower().startswith("fig") else re.escape(kind)
    return rf"\b{head}\s*{re.escape(number)}(?![0-9])"


def _is_caption(text: str, pattern: str) -> bool:
    return text.lstrip().startswith("[") or bool(re.match(rf"(?i)\s*{pattern}\s*[.:|]", text))


def full_caption(label: str, ctx: AnalysisContext) -> str:
    """The figure's or table's whole caption, from the page text (the outline keeps only its start)."""
    start = re.compile(rf"(?im)^\s*{_label_pattern(label)}\s*[.:|]")
    for text in ctx.page_texts or []:
        match = start.search(text)
        if not match:
            continue
        lines = text[match.start():match.start() + 900].split("\n")
        out = [lines[0].strip()]
        for line in lines[1:]:
            line = line.strip()
            if not line or re.match(r"^(?:[A-Z0-9]{1,3}\.\s+[A-Z]|(?:Fig(?:ure)?|Table|Scheme)\s*\d)", line):
                break
            if line[:1].islower() and out[-1].endswith((".", ":")):
                break  # the caption ended and the column's body text goes on
            out.append(line)
        return re.sub(r"-\s+(?=[a-z])", "", " ".join(out))[:700]
    figure = next((f for f in ctx.figures or [] if place_key(f.get("label", "")) == place_key(label)), None)
    return (figure or {}).get("caption", "")


def caption_count(caption: str) -> dict[str, Any] | None:
    """A caption that names its datasets or targets panel by panel ("Left: A; Middle: B; Right: C"): their count."""
    noun = _ENUMERATED.search(caption or "")
    if not noun:
        return None
    names = list(dict.fromkeys(m.group(2) for m in _PANELS.finditer(caption)))
    if len(names) < 2:
        return None
    what = noun.group(1).lower()
    return {"n": str(len(names)), "what": what if what.endswith("s") else what + "s", "names": names}


def _place(label: str, index: PaperIndex, ctx: AnalysisContext) -> dict[str, Any]:
    """A figure or table as printed. In the main text: its page and the section that first cites it. In an appendix
    (its caption is printed in an appendix, or its page lies after the appendices begin): the appendix label."""
    key = place_key(label)
    out: dict[str, Any] = {"label": label}
    figure = next((f for f in ctx.figures or [] if place_key(f.get("label", "")) == key), None)
    if figure is None:
        return out
    out["label"] = figure["label"]
    pattern = _label_pattern(figure["label"])
    caption_in = cited_in = None
    for sid in sorted(index.sentences, key=sort_key):
        sentence = index.sentences[sid]
        if sentence.section == "abstract" or not re.search(rf"(?i){pattern}", sentence.text):
            continue
        if _is_caption(sentence.text, pattern):
            caption_in = caption_in or sentence.section
        elif cited_in is None:
            cited_in = sentence.section
    page = figure.get("page")
    parts = ctx.section_parts or {}
    appendix = None
    if caption_in and parts.get(caption_in) == "appendix":
        appendix = caption_in
    elif page and caption_in is None:
        starts = [ctx.section_pages[k] for k in parts if parts[k] == "appendix" and (ctx.section_pages or {}).get(k)]
        if starts and (page > min(starts) or (page == min(starts) and cited_in and parts.get(cited_in) == "appendix")):
            appendix = _appendix_on_page(page, ctx)
    if appendix:
        out["appendix"] = _section_name(appendix, ctx)
    if cited_in and not appendix:
        out["cited_in"] = _section_name(cited_in, ctx)
    if page:
        out["page"] = page
    out["caption"] = full_caption(figure["label"], ctx)
    return out


def _mentions(text: str) -> set[str]:
    return {place_key(f"{m.group(1)} {m.group(2)}") for m in _MENTION.finditer(text or "")}


def place_text(place: dict[str, Any] | None) -> str:
    """'Table 1, p. 3, cited in Section 2: Results' or 'Figure 4, Appendix A: title, p. 9'."""
    if not place:
        return ""
    if place.get("appendix"):
        return f"{place['label']}, {place['appendix']}" + (f", p. {place['page']}" if place.get("page") else "")
    bits = [place["label"]] + ([f"p. {place['page']}"] if place.get("page") else [])
    bits += [f"cited in {place['cited_in']}"] if place.get("cited_in") else []
    return ", ".join(bits)


def _paragraph_ids(ids: list[str], index: PaperIndex, roles: dict[str, str] | None = None) -> list[str]:
    """Every sentence of the paragraphs these sentences are in. With `roles`, an introduction or abstract paragraph
    adds only the given sentences: its other sentences point at overview figures, not at the experiment."""
    out: list[str] = []
    for p in dict.fromkeys(index.sentences[i].paragraph for i in ids if i in index.sentences):
        section = index.paragraphs[p].section
        if roles is not None and (section == "abstract" or roles.get(section) == "introduction"):
            out += [s for s in index.paragraphs[p].sentence_ids if s in ids]
        else:
            out += list(index.paragraphs[p].sentence_ids)
    return out


def _table_text(place: dict[str, Any], index: PaperIndex) -> str:
    """The table as the parser laid it out ('[Table 1 (p. 3) as a table. Columns ...]')."""
    head = f"[{place['label']} ("
    return " ".join(s.text for s in index.sentences.values() if s.text.startswith(head))


def _variance(raw: dict[str, Any], places: list[dict[str, Any]], context_ids: list[str], index: PaperIndex,
              ctx: AnalysisContext) -> dict[str, Any]:
    """Three things, separately:
    (a) repeats: runs / seeds / replicates stated (usually in the methods) and which experiments they cover;
    (b) error bars: per cited figure, whether its caption defines them (whether they are drawn cannot be read from text);
    (c) spread: whether spread values are shown. A pointer ("can be found in the appendix") counts only if an appendix
        in the file shows the values; otherwise "referred to, not shown". Repeats are never reported as spread."""
    labels = {place_key(p["label"]): p["label"] for p in places}
    # (a) repeats: the whole passage, clause by clause; "covers all" only from a clause with no exception
    given = raw.get("repeats") if isinstance(raw.get("repeats"), dict) else {}
    passage = _repeat_passage(_ids(given.get("ids"), index), index, ctx)
    subsets = _subsets(passage, index)
    rep_ids = [i for i in passage if any(x["kind"] == "repeats" and i in x["ids"] for x in subsets)
               or (_REPEATS.search(index.sentences[i].text) and not _TUNING.search(index.sentences[i].text))]
    repeats: dict[str, Any] = {"text": "", "ids": [], "covers": [], "subsets": subsets, "applies": []}
    if rep_ids:
        rep_subsets = [x for x in subsets if x["kind"] == "repeats"]
        uniform = len({x["n"] for x in rep_subsets if x["count"]}) <= 1 and not any(x["exception"] for x in rep_subsets)
        covers = [labels.get(k) or k for k in sorted(_mentions(index.text_of(rep_ids)))]
        if uniform and not covers:
            for x in rep_subsets:
                everything = _ALL_EXPERIMENTS.search(x["applies_to"])
                if x["count"] and everything:
                    covers = [f"as stated, “{everything.group(0)}”"]
                    break
        stated = index.sentences[rep_ids[0]].text
        repeats = {"text": stated if len(stated) <= 200 else stated[:200], "ids": rep_ids, "covers": covers,
                   "subsets": subsets, "applies": _applies(subsets, places, index)}
    # (b) error bars, per figure
    error_bars = []
    for p in places:
        if not p["label"].lower().startswith("fig"):
            continue
        caption = p.get("caption", "")
        defined = _ERROR_BARS.search(caption)
        error_bars.append({"label": p["label"], "caption_defines": bool(defined),
                           "definition": caption[max(0, defined.start() - 40):defined.end() + 60].strip() if defined else ""})
    # (c) spread values
    spread_raw = raw.get("spread") if isinstance(raw.get("spread"), dict) else {}
    shown_ids = [i for i in _ids(spread_raw.get("ids"), index) if _SHOWS_VARIANCE.search(index.sentences[i].text)]
    if shown_ids:
        return {"repeats": repeats, "error_bars": error_bars,
                "spread": {"status": "shown", "text": _clean(spread_raw.get("text"), 160) or "shown", "ids": shown_ids}}
    for p in places:
        if _SHOWS_VARIANCE.search(_table_text(p, index)) or _SHOWS_VARIANCE.search(p.get("caption", "")):
            return {"repeats": repeats, "error_bars": error_bars,
                    "spread": {"status": "shown", "text": f"shown in {p['label']}", "ids": []}}
    pointers = [i for i in _ids(spread_raw.get("pointer_ids"), index) + _ids(spread_raw.get("ids"), index)
                + context_ids if i in index.sentences and _POINTER.search(index.sentences[i].text)
                and _ERROR_BARS.search(index.sentences[i].text)]
    pointer_captions = [p["label"] for p in places if _POINTER.search(p.get("caption", "")) and _ERROR_BARS.search(p.get("caption", ""))]
    if pointers or pointer_captions:
        shown = _appendix_variance(index, ctx)
        if shown:
            return {"repeats": repeats, "error_bars": error_bars, "spread": {
                "status": "shown", "text": f"shown in {_section_name(index.sentences[shown[0]].section, ctx)}", "ids": shown}}
        where = f"the caption of {', '.join(pointer_captions)}" if pointer_captions else ""
        return {"repeats": repeats, "error_bars": error_bars, "spread": {
            "status": "referred_not_shown", "text": where, "ids": sorted(set(pointers), key=sort_key)[:2]}}
    return {"repeats": repeats, "error_bars": error_bars, "spread": {"status": "not_given", "text": "", "ids": []}}


def _appendix_variance(index: PaperIndex, ctx: AnalysisContext) -> list[str]:
    """Sentences of an appendix in the file that show variance (values with ± or a standard deviation)."""
    appendix = {k for k, part in (ctx.section_parts or {}).items() if part == "appendix"}
    return [sid for sid in sorted(index.sentences, key=sort_key)
            if index.sentences[sid].section in appendix and _SHOWS_VARIANCE.search(index.sentences[sid].text)][:3]


def _family(text: str) -> str | None:
    return next((name for name, pattern in _FAMILIES.items() if pattern.search(text or "")), None)


def _intervention(raw: Any, control_ids: list[str], delta_ids: list[str], claims: list[dict[str, Any]],
                  index: PaperIndex) -> dict[str, Any] | None:
    """An intervention the claim rests on, said in plain words: which representation defines it, which compared
    models take input from the same family, and whether it is the same or only a related representation. One that
    is applied to the models' own inputs is not itself a confound: what else it changes in the data is reported."""
    raw = raw if isinstance(raw, dict) else {}
    target = str(raw.get("target") or "").lower()
    rep = _part(raw.get("representation"), index, 160)
    models_raw = raw.get("models") if isinstance(raw.get("models"), dict) else {}
    model_ids = _ids(models_raw.get("ids"), index)
    model_text = index.text_of(model_ids)
    names = [n for n in (_clean(x, 30) for x in models_raw.get("names") or []) if n and alnum(n) in alnum(model_text)]
    if not rep["ids"] and control_ids:
        # the model gave none: look at the experiment's paragraph for the representation that defines it
        for sid in _paragraph_ids(control_ids + delta_ids, index):
            if _family(index.sentences[sid].text):
                rep = {"text": "", "ids": [sid]}
                break
    if not rep["ids"]:
        return None
    definition = _paragraph_ids(rep["ids"], index)
    rep_text = index.text_of(rep["ids"])
    if not _MANIPULATES.search(rep_text) and not _MANIPULATES.search(index.text_of(definition)):
        return None  # a description of the inputs or the setup, not a manipulation
    # what it changes, from the paper's words: labels, or the models' own inputs
    if re.search(r"(?i)\blabels?\b|\btarget (?:values?|functions?)\b", rep_text):
        target = "labels"
    elif re.search(r"(?i)\binputs?\b|\bfeatures?\b", rep_text):
        target = "inputs"
    family = _family(rep_text)
    if family is None:  # the representation is usually defined in the next sentences of the same paragraph
        hit = next((s for s in definition if _family(index.sentences[s].text)), None)
        if hit:
            family = _family(index.sentences[hit].text)
            rep = {"text": rep["text"], "ids": sorted(set(rep["ids"]) | {hit}, key=sort_key)}
            rep_text = index.text_of(rep["ids"])
    if target == "inputs":
        side = _part(raw.get("side_effects"), index, 200)
        if side["ids"] and not _PROPERTIES.search(side["text"]):
            side = {"text": "", "ids": []}
        return {"kind": "inputs", "representation": rep, "family": family, "side_effects": side}
    if family is None:
        return None
    if names and not _FAMILIES[family].search(model_text):
        names = []  # the cited sentence does not say what these models take as input
    if not names:
        # the sentence that says which models take this family as input (e.g. a "<representation> -> <models>" line)
        for sid in sorted(index.sentences, key=sort_key):
            text = index.sentences[sid].text
            if sid in rep["ids"] or not _FAMILIES[family].search(text):
                continue
            found = [m for m in re.findall(r"\b[A-Z][A-Za-z0-9]{1,7}\b", text)
                     if m.isupper() and not _FAMILIES[family].search(m) and not _SPECIFIC.search(m)]
            if len(found) >= 2 and re.search(r"→|↦|->|\binputs?\b|\btake\w*\b|\buse[sd]?\b", text):
                names, model_ids, model_text = list(dict.fromkeys(found))[:6], [sid], text
                break
    if not names:
        compared = [c for c in claims if _FAMILIES[family].search(c["statement"])]
        if not compared:
            return None
        names = [f"the models of {compared[0]['id']}"]
        model_text = compared[0]["statement"]
    rep_specific = {alnum(m) for m in _SPECIFIC.findall(rep_text)}
    model_specific = {alnum(m) for m in _SPECIFIC.findall(model_text)}
    same = bool(rep_specific & model_specific)
    terms = list(dict.fromkeys(m.upper() if len(m) <= 6 else m for m in re.findall(
        r"(?i)\b(?:ecfp\w*|fcfp\w*|morgan|maccs|tanimoto|atom[- ]pairs?|smiles|selfies|rdkit|mordred)\b", rep_text)))
    return {"kind": "confound", "representation": {**rep, "text": rep["text"] or family}, "family": family,
            "terms": terms, "models": {"names": names, "ids": model_ids}, "same": same,
            "shared": sorted(rep_specific & model_specific)}


def _balanced(citation: str) -> str:
    citation = citation.strip().lstrip("(")
    return citation[:-1] if citation.endswith(")") and citation.count(")") > citation.count("(") else citation


def _theory(value: Any, index: PaperIndex) -> dict[str, Any] | None:
    """A known theory the explanation rests on, named by its citation as printed. Without a citation in its
    sentences it is not a cited theory."""
    value = value if isinstance(value, dict) else {}
    ids = _ids(value.get("ids"), index)
    if not ids:
        return None
    hay = index.text_of(ids)
    cites = list(dict.fromkeys(_balanced(m.group(0)) for m in _CITATION.finditer(hay)))
    if not cites:
        return None
    return {"text": _clean(value.get("note") or value.get("text"), 160), "ids": ids, "citations": cites[:3]}


def _part(value: Any, index: PaperIndex, limit: int = 240) -> dict[str, Any]:
    value = value if isinstance(value, dict) else {}
    ids = _ids(value.get("ids"), index)
    text = _clean(value.get("text") or value.get("note"), limit)
    return {"text": text, "ids": ids} if ids and text else {"text": "", "ids": []}


_CAUSAL = re.compile(
    r"(?i)\b(attribut\w*|because|due to|caus\w*|explain\w*|mechanis\w*|reasons?|driven by|underl\w*|bottlenecks?|"
    r"why|stems? from|arises? from|origin\w*|accounts? for)\b"
)
_QUESTIONS = re.compile(
    r"(?i)\b(not (?:solely|only|necessarily|simply|merely|entirely)|cannot be (?:solely |simply |only |entirely )?"
    r"(?:attributed|explained)|rather than|overestimat\w*|underestimat\w*|misleading|is not the (?:only|main|sole))\b"
)


def _is_causal(claim: dict[str, Any]) -> bool:
    return str(claim.get("type") or "").lower() == "causal" or bool(_CAUSAL.search(claim["statement"]))


def _following(ids: list[str], index: PaperIndex) -> str:
    """The sentence after the last of `ids` in the same paragraph: where a paper often answers what it cited."""
    if not ids or ids[-1] not in index.sentences:
        return ""
    paragraph = index.paragraphs[index.sentences[ids[-1]].paragraph].sentence_ids
    at = paragraph.index(ids[-1])
    return index.sentences[paragraph[at + 1]].text if at + 1 < len(paragraph) else ""


def _prior_work(raw: dict[str, Any], index: PaperIndex, by_id: dict[str, dict[str, Any]],
                dropped: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Per claim: the earlier work the paper itself cites for it. A sentence applies to a claim only if it is about the
    same subject and the same assertion: a sentence about an outcome does not cover a claim about its cause. Where
    the paper questions or limits the earlier work or belief, the relation is "challenges", never "builds_on"."""
    per_claim: dict[str, list[dict[str, Any]]] = {cid: [] for cid in by_id}
    members = _members(index)
    statement = {cid: _with_categories(c["statement"], members) for cid, c in by_id.items()}
    for p in (raw.get("prior_work") or [])[:MAX_PRIOR]:
        if not isinstance(p, dict):
            continue
        ids = _ids(p.get("ids"), index)
        text = index.text_of(ids)
        citations = [c for c in (_clean(x, 80) for x in p.get("citations") or []) if c and _citation_in(c, text)]
        if not ids or not citations:
            if ids or p.get("citations"):
                dropped.append("a prior-work citation is not in the sentences cited for it")
            continue
        covers = _clean(p.get("covers"), 80)
        if covers and not _words(covers) <= _words(text):
            covers = ""  # the subject must be the paper's own words
        about_cause = bool(_CAUSAL.search(text))
        questioned = bool(_CHALLENGES.search(text) or _QUESTIONS.search(text + " " + _following(ids, index)))

        def relation_for(claim: dict[str, Any], relation: str) -> str:
            if relation in ("builds_on", "same_finding") and (questioned or _QUESTIONS.search(claim["statement"])
                                                             or _CHALLENGES.search(claim["statement"])):
                return "challenges"
            return relation

        listed: set[str] = set()
        for a in p.get("applies_to") or []:
            if not isinstance(a, dict) or str(a.get("claim")) not in by_id:
                continue
            cid = str(a["claim"])
            listed.add(cid)
            if _is_causal(by_id[cid]) and not about_cause:
                dropped.append(f"{citations[0]} is about an outcome, not the cause that {cid} claims")
                continue
            if not _stems(statement[cid]) & _stems(f"{text} {covers}"):
                dropped.append(f"{citations[0]} is about a different subject than {cid}")
                continue
            relation = str(a.get("relation") or "").lower()
            relation = relation if relation in RELATIONS else "builds_on"
            per_claim[cid].append({"ids": ids, "citations": citations, "covers": covers,
                                   "relation": relation_for(by_id[cid], relation), "by_scope": False})
        if covers:
            for cid, claim in by_id.items():
                if (cid not in listed and _within(covers, statement[cid], text)
                        and not (_is_causal(claim) and not about_cause)):
                    per_claim[cid].append({"ids": ids, "citations": citations, "covers": covers,
                                           "relation": relation_for(claim, "builds_on"), "by_scope": True})
    return per_claim


def _places(item: dict[str, Any], sentence_ids: list[str], index: PaperIndex, ctx: AnalysisContext,
            dropped: list[str], whole_paragraphs: bool = True) -> list[dict[str, Any]]:
    """Every figure and table that supports the claim: the model's, and every one that the claim's sentences or the
    experiment's paragraphs refer to (`whole_paragraphs`), or only the given sentences."""
    known = known_places(ctx)
    delta_raw = item.get("delta") if isinstance(item.get("delta"), dict) else {}
    named = delta_raw.get("shown_in") or []
    named = [named] if isinstance(named, str) else [x for x in named if isinstance(x, str)]
    labels: list[str] = []
    for label in named:
        label = _clean(label, 40)
        if not label:
            continue
        if place_key(label) in known:
            labels.append(label)
        else:
            dropped.append(f"'{label}' is not in the paper's outline")
    figure_keys = {place_key(f["label"]): f["label"] for f in ctx.figures or [] if f.get("label")}
    scan = (_paragraph_ids(sentence_ids, index, ctx.section_roles or {}) if whole_paragraphs
            else [i for i in sentence_ids if i in index.sentences])
    if whole_paragraphs:  # the experiment's paragraph: where each named figure or table is introduced
        roles = ctx.section_roles or {}
        seeds = list(labels) + [figure_keys[k] for i in scan for k in _mentions(index.sentences[i].text) if k in figure_keys]
        for label in dict.fromkeys(seeds):
            pattern = _label_pattern(figure_keys.get(place_key(label), label))
            intro = next((sid for sid in sorted(index.sentences, key=sort_key)
                          if index.sentences[sid].section != "abstract"
                          and roles.get(index.sentences[sid].section) != "introduction"
                          and re.search(rf"(?i){pattern}", index.sentences[sid].text)
                          and not _is_caption(index.sentences[sid].text, pattern)), None)
            if intro:
                scan += [i for i in index.paragraphs[index.sentences[intro].paragraph].sentence_ids if i not in scan]
    for sid in scan:
        if index.sentences[sid].text.lstrip().startswith("["):
            continue  # the parser's table layout repeats the label
        for key in sorted(_mentions(index.sentences[sid].text)):
            if key in figure_keys:
                labels.append(figure_keys[key])
    out: list[dict[str, Any]] = []
    for label in labels:
        if all(place_key(p["label"]) != place_key(label) for p in out):
            out.append(_place(label, index, ctx))
    return sorted(out, key=lambda p: (0 if p["label"].startswith("Fig") else 1, p["label"]))


def _counts(raw: Any, places: list[dict[str, Any]], index: PaperIndex, cid: str, dropped: list[str]) -> list[dict[str, Any]]:
    """Counts of datasets / targets / compounds, kept when the number is in its sentences or a caption of the claim's
    figures and tables; a caption that names its datasets or targets one by one is counted ("from the caption")."""
    captions = " ".join(p.get("caption", "") for p in places)
    out: list[dict[str, Any]] = []
    for c in raw or []:
        if not isinstance(c, dict):
            continue
        ids = _ids(c.get("ids"), index)
        n = _clean(c.get("n"), 20)
        what = _clean(c.get("what"), 60)
        number = re.compile(r"(?<![\d.])" + re.escape(n) + r"(?![\d])") if n else None
        if n and what and number and (number.search(index.text_of(ids)) or number.search(captions)):
            source = _clean(c.get("source"), 40)
            out.append({"n": n, "what": what, "ids": ids, "from_caption": False,
                        "source": source if source and any(place_key(source) == place_key(p["label"]) for p in places) else ""})
        elif n and not any(caption_count(p.get("caption", "")) for p in places):
            dropped.append(f"the count '{n} {what}' for {cid} is not in its sentences or captions")
    for p in places:
        counted = caption_count(p.get("caption", ""))
        if counted and not any(x["source"] == p["label"] and x["n"] == counted["n"] for x in out):
            out.append({"n": counted["n"], "what": counted["what"], "ids": [], "from_caption": True,
                        "source": p["label"], "names": counted["names"]})
    return out


def _depth(item: dict[str, Any], key: str, index: PaperIndex, dropped: list[str], cid: str,
           claim: dict[str, Any] | None = None) -> dict[str, Any]:
    """Limits and the skeptic's objection must name a concrete detail of this paper (a number, a protocol choice, a
    subset, an exclusion) that is in the sentences they cite. "Would be stronger with" must name a control that varies
    the claim's stated cause while holding the rest fixed."""
    value = _part(item.get(key), index, 300)
    if value["text"] and _VAGUE.search(value["text"]):
        dropped.append(f"the {key} note for {cid} was vague")
        return {"text": "", "ids": []}
    if value["ids"] and key in ("limits", "skeptic") and not _concrete(value["text"]) & _concrete(index.text_of(value["ids"])):
        dropped.append(f"the {key} note for {cid} names no concrete detail of the paper (a number, protocol choice, "
                       f"subset or exclusion) from the sentences it cites")
        return {"text": "", "ids": []}
    if value["ids"] and key == "strengthen" and not _isolates(value["text"], _cause(claim or {"statement": "", "type": ""})):
        dropped.append(f"the strengthen note for {cid} does not name a control that varies the stated cause while "
                       f"holding the rest fixed")
        return {"text": "", "ids": []}
    if isinstance(item.get(key), dict) and item[key].get("text") and not value["ids"]:
        dropped.append(f"the {key} note for {cid} cites no sentence")
    return value


def _journal_note(verdict: str, ctx: AnalysisContext) -> dict[str, str] | None:
    """Novelty against the target journal's tier (knowledge/journals.yaml), written by code."""
    if not ctx.journal:
        return None
    from services.journal_policy import SELECTIVE, SOUNDNESS, policy_for

    policy = policy_for(ctx.journal)
    if policy.tier == SOUNDNESS:
        text = (f"{policy.journal} reviews for soundness: novelty is not a criterion there, so the evidence weighs "
                f"more than the {verdict} verdict")
    elif policy.tier == SELECTIVE:
        text = {"new": f"against the work it cites, this meets the novelty a selective journal such as {policy.journal} expects",
                "incremental": f"an incremental advance is usually below the novelty a selective journal such as "
                               f"{policy.journal} expects",
                "overlapping": f"overlap with cited work is unlikely to meet the bar of a selective journal such as "
                               f"{policy.journal}"}[verdict]
    else:
        text = {"new": f"the novelty is not in question for {policy.journal}",
                "incremental": f"an incremental advance is usually acceptable at {policy.journal} if the evidence holds",
                "overlapping": f"the overlap with cited work would need to be justified for {policy.journal}"}[verdict]
    return {"tier": policy.tier, "journal": policy.journal, "text": text}


# ---------------------------------------------------------------- round 3: repeats by subset, design checks, depth

_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
              "ten": 10, "twenty": 20, "thirty": 30, "fifty": 50, "hundred": 100}
_STRONG_REPEATS = ("seed", "run", "repeat", "repetition", "replicate")
_COUNT = re.compile(
    rf"(?i)(?<!figure )(?<!fig\. )(?<!table )\b(?P<n>\d[\d,]*|{'|'.join(_NUM_WORDS)})\s+(?:(?:independent|random|different|optimi[sz]ation|tuning|"
    r"search|hyper-?parameter|bayesian)\s+){0,2}(?P<unit>runs|seeds|repeats|repetitions|replicates|folds|times|"
    r"trials|iterations|evaluations|configurations|gpu[- ]hours|cpu[- ]hours)\b(?!\s+(?:the|a|an|this|it|each)\b)")
_TUNING = re.compile(r"(?i)hyper-?parameters?|\btun(?:e|ed|ing)\b|\bbudgets?\b|grid search|random search|"
                     r"bayesian optimi|\bsearch(?:ed)? over\b")
_EXCEPTION = re.compile(r"(?i)\b(?:except(?:\s+(?:for|on|in|that|when))?|apart from|other than|"
                        r"with the exception of|whereas|but)\b|;")
_SPLIT = re.compile(
    r"(?i)\b(?:random(?:ly)?|scaffold|stratified|temporal|time[- ]based|chronological|cluster(?:ed|-based)?|"
    r"leave-[\w-]+-out|\d+-fold)\s+(?:[\w-]+\s+){0,2}?(?:split\w*|partition\w*|cross[- ]validation)\b"
    r"|\bsplit\w* (?:[\w-]+ ){0,3}?(?:randomly|by (?:scaffold|time|date|cluster|group)\w*)")
_EXCLUDES = re.compile(r"(?i)\b(exclud\w*|omit\w*|discard\w*|left out|not (?:included|considered|evaluated|run)|"
                       r"(?:removed|dropped) from)\b")
_TASKS = re.compile(r"(?i)\b(regression|classification|ranking|segmentation|detection|generation)\b")
_COMPARES = re.compile(r"(?i)\b(outperform\w*|underperform\w*|beat\w*|better|worse|than|gap|superior|inferior|"
                       r"versus|vs\.?|compar\w*)\b")
_PROTOCOL = re.compile(
    r"(?i)\b(split\w*|scaffold\w*|random\w*|stratif\w*|seeds?|runs?|trials?|budgets?|tun(?:e|ed|ing)|"
    r"hyper-?parameters?|exclud\w*|omit\w*|remov\w*|subsets?|except\w*|metrics?|regression|classification|"
    r"cross[- ]validation|folds?|epochs?|thresholds?|cut-?offs?|filter\w*|smooth\w*|nois\w*|ablat\w*)\b")
_COMMON_CAPS = {"the", "we", "this", "in", "our", "a", "an", "it", "these", "for", "to", "as", "on", "however",
                "here", "each", "all", "both", "when", "with", "figure", "fig", "table", "section", "appendix", "if"}
_HOLD = re.compile(r"(?i)\b(while (?:keeping|holding|fixing)|keeping|holding|held|fixed|constant|unchanged|same|"
                   r"identical|equal|matched|only)\b")
_VARY = re.compile(r"(?i)\b(vary|varies|varying|varied|chang\w*|manipulat\w*|remov\w*|ablat\w*|swap\w*|replac\w*|"
                   r"shuffl\w*|perturb\w*|increas\w*|decreas\w*|reduc\w*|subsampl\w*|without|switch\w*|control\w*)\b")
_CAUSE_RIGHT = re.compile(r"(?i)\b(?:due to|because of|attributed to|attributable to|driven by|caused by|"
                          r"explained by|stems? from|arises? from|results? from|owing to)\b")
_CAUSE_LEFT = re.compile(r"(?i)\b(?:causes?|caused|drives?|explains?|leads? to|results? in|is responsible for|"
                         r"accounts? for|determines?|governs?)\b")
_GENERIC_SUBJECT = {"model", "data", "dataset", "result", "method", "approach", "study", "work", "performance",
                    "paper", "task", "earlier", "previou", "show", "report", "reported", "al", "et"}
_MEMBERS = re.compile(
    r"(?P<cat>[A-Za-z][\w-]*\s+[A-Za-z][\w-]*s),?\s+(?:such as|e\.g\.,?|including|like)\s+"
    r"(?P<mem>[A-Z][\w-]*(?:(?:,\s*(?:and\s+|or\s+)?|\s+and\s+|\s+or\s+)[A-Z][\w-]*)*)"
    r"|(?P<mem2>[A-Z][\w-]*(?:(?:,\s*(?:and\s+)?|\s+and\s+)[A-Z][\w-]*)*),?\s+(?:which|that)\s+are\s+(?:all\s+)?"
    r"(?P<cat2>[A-Za-z][\w-]*\s+[A-Za-z][\w-]*s)\b")


def _number_of(n: str) -> int | None:
    n = n.lower().replace(",", "")
    return int(n) if n.isdigit() else _NUM_WORDS.get(n)


def _segments(text: str) -> list[tuple[str, bool]]:
    """A sentence cut at its exception clauses ("except on A and B, where ...") and before a second count
    ("100 trials for X and 20 trials for Y"): (clause, is_exception)."""
    cuts = {0} | {m.start() for m in _EXCEPTION.finditer(text)}
    counts = list(_COUNT.finditer(text))
    for prev, m in zip(counts, counts[1:]):
        if any(prev.end() <= c <= m.start() for c in cuts if c):
            continue
        between = text[prev.end():m.start()]
        j = max(between.rfind(" and "), between.rfind(", "))
        if j >= 0:
            cuts.add(prev.end() + j)
    ordered = sorted(cuts)
    out = []
    for a, b in zip(ordered, ordered[1:] + [len(text)]):
        piece = text[a:b]
        seg = re.sub(r"^(?:and|,)\s+", "", piece.strip(" ,;.")).strip(" ,;.")
        if seg:
            out.append((seg, bool(_EXCEPTION.match(piece.lstrip(" ,")))))
    return out


def _repeat_passage(given: list[str], index: PaperIndex, ctx: AnalysisContext) -> list[str]:
    """The sentences that state repeats or tuning budgets: the model's, then every one in the methods or an appendix."""
    roles, parts = ctx.section_roles or {}, ctx.section_parts or {}
    where = {k for k, r in roles.items() if r == "methods"} | {k for k, p in parts.items() if p == "appendix"}

    def stated(s: str) -> bool:
        s = re.sub(r"(?i)\b(?:fig(?:ure)?s?\.?|tables?)\s*\d+", "", s)  # "Figure 4 repeats ..." is not a count
        return bool(_COUNT.search(s) or _REPEATS.search(s))

    ids = [i for i in given if stated(index.sentences[i].text)]
    for sid in sorted(index.sentences, key=sort_key):
        s = index.sentences[sid]
        if s.section in where and sid not in ids and not s.text.lstrip().startswith("[") and stated(s.text):
            ids.append(sid)
    return ids[:8]


def _subsets(passage: list[str], index: PaperIndex) -> list[dict[str, Any]]:
    """Each count of the passage with the clause it belongs to: repeats (seeds, runs, replicates) or a tuning budget
    (trials, iterations), and whether the clause is an exception to the rest of the sentence."""
    out: list[dict[str, Any]] = []
    for sid in passage:
        text = index.sentences[sid].text
        tuning_sentence = bool(_TUNING.search(text))
        counts = list(_COUNT.finditer(text))
        strong = any(m.group("unit").lower().startswith(_STRONG_REPEATS) for m in counts)
        for seg, exception in _segments(text):
            found = list(_COUNT.finditer(seg))
            for m in found:
                unit = m.group("unit").lower()
                kind = "tuning" if tuning_sentence and not unit.startswith(_STRONG_REPEATS) else "repeats"
                out.append({"count": re.sub(r"\s+", " ", m.group(0)), "n": _number_of(m.group("n")), "kind": kind,
                            "applies_to": seg[:160], "exception": exception, "ids": [sid]})
            if not found and exception and counts:
                kind = "tuning" if tuning_sentence and not strong else "repeats"
                out.append({"count": "", "n": None, "kind": kind, "applies_to": seg[:160], "exception": True,
                            "ids": [sid]})
    return out


def _place_names(place: dict[str, Any], index: PaperIndex) -> list[str]:
    """The datasets or targets a figure or table shows: named in its caption, or the row labels of the table."""
    counted = caption_count(place.get("caption", ""))
    if counted:
        return counted["names"]
    from services.ranking_check import _name, parse_tables

    tables = parse_tables(_table_text(place, index))
    return [_name(r[0]) for r in tables[0].rows] if tables else []


def _named_in(name: str, text: str) -> bool:
    return bool(re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text))


def _applies(subsets: list[dict[str, Any]], places: list[dict[str, Any]], index: PaperIndex) -> list[dict[str, str]]:
    """The repeat count that applies to the datasets of each cited figure or table."""
    rep = [s for s in subsets if s["kind"] == "repeats" and s["count"]]
    if not rep:
        return []
    default = [s for s in rep if not s["exception"]]
    uniform = len({s["n"] for s in rep}) == 1 and not any(s["exception"] for s in subsets if s["kind"] == "repeats")
    out = []
    for p in places:
        names = _place_names(p, index)
        by_count: dict[str, list[str]] = {}
        for name in names:
            hit = (next((s for s in rep if s["exception"] and _named_in(name, s["applies_to"])), None)
                   or next((s for s in rep if _named_in(name, s["applies_to"])), None)
                   or (default[0] if default and len({s["n"] for s in default}) == 1 else None))
            if hit:
                by_count.setdefault(hit["count"], []).append(name)
        if uniform:
            text = rep[0]["count"]
        elif names and by_count:
            text = "; ".join(f"{c} for {', '.join(n)}" for c, n in by_count.items())
            missing = [n for n in names if all(n not in v for v in by_count.values())]
            text += f"; not stated for {', '.join(missing)}" if missing else ""
        else:
            text = "which of the stated counts applies is not stated"
        out.append({"label": p["label"], "text": text})
    return out


def _concrete(text: str) -> set[str]:
    """The concrete details a sentence names: numbers, names (capitalised words, acronyms), protocol words."""
    out = set(re.findall(r"\d+(?:\.\d+)?", text or ""))
    out |= {str(v) for w, v in _NUM_WORDS.items() if re.search(rf"(?i)\b{w}\b", text or "")}
    out |= {w.lower() for w in re.findall(r"\b[A-Z][A-Za-z0-9\-+]*", text or "")
            if len(w) > 1 and w.lower() not in _COMMON_CAPS}
    out |= {m.group(0).lower()[:5] for m in _PROTOCOL.finditer(text or "")}
    return out


def _cause(claim: dict[str, Any]) -> str:
    """The cause a causal claim names, in its own words ("X causes Y" -> X; "Y is due to X" -> X)."""
    if not _is_causal(claim):
        return ""
    s = claim["statement"]
    m = _CAUSE_RIGHT.search(s)
    if m:
        cause = s[m.end():]
    else:
        m = _CAUSE_LEFT.search(s)
        if not m:
            return ""
        cause = s[:m.start()]
    cause = re.split(r"(?i)[,;]|\b(?:rather than|and not|but)\b", cause)[0].strip()
    for _ in range(2):
        cause = re.sub(r"(?i)^(?:the|a|an|low|high|small|large|reduced|increased|poor|limited|more|less)\s+", "", cause)
    return cause.strip(" .")


def _stems(text: str) -> set[str]:
    return {w[:5] for w in _words(text) if w not in _GENERIC_SUBJECT}


def _isolates(text: str, cause: str) -> bool:
    """A control that varies the stated cause while holding the rest fixed."""
    if not (_HOLD.search(text) and _VARY.search(text)):
        return False
    return not cause or bool(_stems(text) & _stems(cause))


def _members(index: PaperIndex) -> list[tuple[str, list[str]]]:
    """Category and members as the paper defines them ("<category> such as A and B", "A and B, which are <category>")."""
    out = []
    for s in index.sentences.values():
        for m in _MEMBERS.finditer(s.text):
            cat = m.group("cat") or m.group("cat2")
            names = re.split(r",\s*(?:and\s+|or\s+)?|\s+and\s+|\s+or\s+", m.group("mem") or m.group("mem2"))
            out.append((cat, [n for n in names if n]))
    return out


def _with_categories(statement: str, members: list[tuple[str, list[str]]]) -> str:
    """The statement with the category of each member it names added, so a sentence about the category covers it."""
    extra = [cat for cat, names in members if any(_named_in(n, statement) for n in names)]
    return " ".join([statement, *dict.fromkeys(extra)])


def _design_checks(subsets: list[dict[str, Any]], index: PaperIndex, ctx: AnalysisContext) -> list[dict[str, Any]]:
    """Checked for every paper: unequal tuning budgets or repeat counts between compared subsets, the split type,
    and models or datasets the paper excludes. Each names the sentence it rests on."""
    out: list[dict[str, Any]] = []

    def listed(items: list[dict[str, Any]]) -> str:
        return "; ".join(f"{s['count']} ({s['applies_to']})" for s in items if s["count"])

    def ids_of(items: list[dict[str, Any]]) -> list[str]:
        return sorted({i for s in items for i in s["ids"]}, key=sort_key)

    budgets = [s for s in subsets if s["kind"] == "tuning" and s["count"]]
    if len({s["n"] for s in budgets}) > 1:
        out.append({"check": "unequal_budget", "text": f"tuning budgets differ: {listed(budgets)}", "ids": ids_of(budgets)})
    reps = [s for s in subsets if s["kind"] == "repeats"]
    if len({s["n"] for s in reps if s["count"]}) > 1 or any(s["exception"] for s in reps):
        out.append({"check": "unequal_repeats", "text": f"repeat counts differ between subsets: {listed(reps)}"
                    + ("; an exception with no count is stated" if any(not s["count"] for s in reps) else ""),
                    "ids": ids_of(reps)})
    roles, parts = ctx.section_roles or {}, ctx.section_parts or {}
    where = {k for k, r in roles.items() if r in ("methods", "results")} | {k for k, p in parts.items() if p == "appendix"}
    ordered = [sid for sid in sorted(index.sentences, key=sort_key) if index.sentences[sid].section in where
               and not index.sentences[sid].text.lstrip().startswith("[")]
    split = next(((sid, m) for sid in ordered if (m := _SPLIT.search(index.sentences[sid].text))), None)
    if split:
        out.append({"check": "split", "text": f"split: “{split[1].group(0)}”", "ids": [split[0]]})
    else:
        out.append({"check": "split", "text": "the paper does not say how the data are split", "ids": []})
    for sid in [s for s in ordered if _EXCLUDES.search(index.sentences[s].text)][:2]:
        out.append({"check": "exclusion", "text": f"excluded: “{_clean(index.sentences[sid].text, 200)}”", "ids": [sid]})
    return out


def _mix(places: list[dict[str, Any]], index: PaperIndex) -> dict[str, Any] | None:
    """The claim's figures and tables mix tasks or metrics of opposite direction (a pooled ranking mixes scales)."""
    from services.ranking_check import _HIGHER, _LOWER

    hay = " ".join(p.get("caption", "") + " " + _table_text(p, index) for p in places)
    tasks = sorted({t.lower() for t in _TASKS.findall(hay)})
    lower = sorted({m.upper() for m in _LOWER.findall(hay)})
    higher = sorted({m.upper() for m in _HIGHER.findall(hay)})
    bits = []
    if len(tasks) > 1:
        bits.append(f"task ({', '.join(tasks)})")
    if lower and higher:
        bits.append(f"metric ({', '.join(higher)}: higher is better; {', '.join(lower)}: lower is better)")
    if not bits:
        return None
    return {"check": "task_metric_mix", "ids": [], "source": [p["label"] for p in places],
            "text": f"the compared datasets differ in {' and '.join(bits)} ({', '.join(p['label'] for p in places)})"}


_OBJECTION = {
    "unequal_budget": "the compared models are not tuned equally ({}), so part of the difference may come from tuning",
    "unequal_repeats": "the {}, so the spread is not comparable across them",
    "exclusion": "the comparison leaves something out: {}",
    "split": "the evaluation rests on one split type ({}); the claim is not tested under another",
}


def _weak(c: dict[str, Any]) -> bool:
    return c["status"] == "new" and c["support"] == "correlational" and not c["strength"]["control"]["ids"]


def _status_label(c: dict[str, Any]) -> str:
    return "new (weakly supported)" if _weak(c) else VERDICT_LABELS[c["status"]]


def _overall(raw: Any, contributions: list[dict[str, Any]], verdict: str, reason: dict[str, Any],
             superlatives: list[dict[str, Any]], index: PaperIndex, dropped: list[str]) -> list[dict[str, Any]]:
    """Three sentences: the strongest new claim, the strongest overlap with cited work, and why the verdict follows.
    The model's sentence is kept when it checks out; otherwise code writes it from the checked data. A sentence that
    restates a superlative listed as unverified is dropped."""
    raw = raw if isinstance(raw, dict) else {}
    unverified = [s["text"].lower() for s in superlatives if not s["supported"] and s["text"]]
    by_id = {c["claim"]: c for c in contributions}

    def usable(role: str, need: Any) -> dict[str, Any] | None:
        part = _part(raw.get(role), index, 400)
        if not part["ids"]:
            return None
        low = part["text"].lower()
        hit = next((u for u in unverified if u in low), None)
        if hit or _VAGUE.search(part["text"]):
            dropped.append(f"the overall '{role}' sentence " + (f"restates the unverified “{hit}”" if hit else "was vague"))
            return None
        cid = str((raw.get(role) or {}).get("claim") or "")
        if need is not None and not need(by_id.get(cid)):
            dropped.append(f"the overall '{role}' sentence names {cid or 'no claim'}, which does not fit that role")
            return None
        return part

    rank = {"new": 0, "incremental": 1, "overlapping": 2}
    strength = {"causal": 0, "correlational": 1, "descriptive": 2}
    with_delta = sorted([c for c in contributions if c["delta"]["text"]],
                        key=lambda c: (rank[c["status"]], not c["main"], strength[c["support"]]))
    order = ("same_finding", "reuses_data_from", "builds_on")
    overlapping = sorted([(c, p) for c in contributions for p in c["prior"] if p["relation"] in order],
                         key=lambda cp: (not cp[0]["main"], cp[1]["by_scope"], order.index(cp[1]["relation"])))
    out = []
    new = usable("strongest_new", lambda c: bool(c and c["delta"]["text"]))
    if new is None and with_delta:
        c = with_delta[0]
        new = {"text": f"Strongest new claim: {c['claim']} — {c['delta']['text'].rstrip('.')} "
                       f"({_KIND_TEXT.get(c['evidence_kind'], 'no new evidence')}, {c['support']}; {_status_label(c)})",
               "ids": c["delta"]["ids"]}
    out.append({"role": "strongest_new", **(new or {"text": "No claim shows anything specific as new.", "ids": []})})
    over = usable("strongest_overlap", lambda c: bool(c and any(p["relation"] in order for p in c["prior"])))
    if over is None and overlapping:
        c, p = overlapping[0]
        over = {"text": f"Strongest overlap with cited work: {c['claim']} — {_RELATION_TEXT[p['relation']].lower()}: "
                        f"{_join(p['citations'])}", "ids": p["ids"]}
    out.append({"role": "strongest_overlap",
                **(over or {"text": "No claim is attributed to, built on or reuses cited work.", "ids": []})})
    why = usable("why_verdict", None) if str(raw.get("verdict") or verdict).lower() == verdict else None
    out.append({"role": "why_verdict", **(why or reason)})
    return out


def check(raw: dict[str, Any], index: PaperIndex, claims: list[dict[str, Any]], ctx: AnalysisContext) -> dict[str, Any]:
    """The model's assessment with everything the paper does not support removed, and the verdict by code."""
    by_id = {c["id"]: c for c in claims}
    dropped: list[str] = []
    prior_by_claim = _prior_work(raw, index, by_id, dropped)
    design = _design_checks(_subsets(_repeat_passage([], index, ctx), index), index, ctx)
    used_places: dict[str, str] = {}  # figure/table -> the first claim whose new experiment it shows
    contributions: list[dict[str, Any]] = []
    items = [i for i in raw.get("claims") or raw.get("contributions") or [] if isinstance(i, dict)]
    items.sort(key=lambda i: [c["id"] for c in claims].index(str(i.get("claim"))) if str(i.get("claim")) in by_id else 99)
    seen: set[str] = set()
    for item in items[:MAX_CLAIMS]:
        cid = str(item.get("claim"))
        if cid not in by_id or cid in seen:
            continue
        seen.add(cid)
        claim = by_id[cid]
        scope = _part(item.get("scope"), index)
        ctype = str(item.get("contribution_type") or "").lower()
        ctype = ctype if ctype in CONTRIBUTION_TYPES else ""
        theory = _theory(item.get("theory"), index)
        if isinstance(item.get("theory"), dict) and item["theory"].get("ids") and theory is None:
            dropped.append(f"the theory for {cid} has no citation in its sentences")

        delta_raw = item.get("delta") if isinstance(item.get("delta"), dict) else {}
        delta: dict[str, Any] = {"text": _clean(delta_raw.get("text"), 360), "ids": _ids(delta_raw.get("ids"), index)}
        if alnum(delta["text"]) in ("", "none", "na", "nothing", "nothingnew"):
            delta = {"text": "", "ids": []}
        if delta["text"] and _VAGUE.search(delta["text"]):
            dropped.append(f"the delta for {cid} was vague")
            delta = {"text": "", "ids": []}
        if delta["text"] and not delta["ids"]:
            dropped.append(f"the delta for {cid} has no sentence ids")
            delta = {"text": "", "ids": []}
        strength_raw = item.get("strength") if isinstance(item.get("strength"), dict) else {}
        control = _part(strength_raw.get("control"), index)
        claim_ids = [i for i in claim.get("evidence_ids") or [] if i in index.sentences]
        places = _places(item, claim_ids + delta["ids"] + control["ids"], index, ctx, dropped)
        # what the delta itself rests on: the figures named for it and those its own sentences cite
        delta["shown_in"] = _places(item, delta["ids"], index, ctx, [], whole_paragraphs=False) if delta["text"] else []

        kind = str(item.get("evidence_kind") or "none").lower()
        kind = kind if kind in EVIDENCE_KINDS else "none"
        if not delta["text"]:
            kind = "none"
        notes: list[str] = []
        keys = [place_key(p["label"]) for p in delta["shown_in"]]
        if kind == "new_experiment" and keys and all(k in used_places for k in keys):
            kind = "reanalysis_of_existing_results"
            notes.append(", ".join(sorted({f"{p['label']} is already the evidence for {used_places[place_key(p['label'])]}"
                                           for p in delta["shown_in"]})))
        elif kind == "new_experiment":
            for k in keys:
                used_places.setdefault(k, cid)

        counts = _counts(strength_raw.get("counts"), places, index, cid, dropped)
        context_ids = _paragraph_ids(claim_ids + delta["ids"] + control["ids"], index)
        variance = _variance(strength_raw, places, context_ids, index, ctx)

        support = str(item.get("support") or "descriptive").lower()
        support = support if support in SUPPORT else "descriptive"
        if support == "causal" and not control["ids"]:
            support = "correlational"
            notes.append("called causal, but no control, ablation or counter-test isolates the cause")
        if kind == "reanalysis_of_existing_results" and support == "causal":
            support = "correlational"
            notes.append("re-reading results already shown cannot isolate a cause")
        if kind == "new_explanation" and support == "descriptive" and not control["ids"]:
            support = "correlational"
        intervention = _intervention(item.get("intervention"), control["ids"], delta["ids"], claims, index) \
            if (control["ids"] or isinstance(item.get("intervention"), dict)) else None

        prior = prior_by_claim.get(cid, [])
        relations = {p["relation"] for p in prior}
        if "same_finding" in relations and kind == "none":
            status = "overlapping"
        elif (kind in ("new_experiment", "new_explanation") and theory is None
              and not relations & {"same_finding", "builds_on", "reuses_data_from"}):
            status = "new"  # a new experiment or explanation that no earlier work the paper cites already supports
        else:
            status = "incremental"  # includes re-reading an existing table, new scope, and testing a cited theory
        contributions.append({
            "claim": cid, "statement": claim["statement"], "main": bool(claim.get("main")),
            "scope": scope, "contribution_type": ctype, "prior": prior, "theory": theory, "delta": delta,
            "places": places, "evidence_kind": kind, "support": support, "intervention": intervention,
            "strength": {"counts": counts, "control": control, **variance},
            "limits": _depth(item, "limits", index, dropped, cid, claim),
            "skeptic": _depth(item, "skeptic", index, dropped, cid, claim),
            "strengthen": _depth(item, "strengthen", index, dropped, cid, claim),
            "notes": notes, "status": status,
        })
        c = contributions[-1]
        comparative = str(claim.get("type") or "").lower() == "comparative" or bool(_COMPARES.search(claim["statement"]))
        checks = [d for d in design if comparative or d["check"] not in ("unequal_budget",)]
        mixed = _mix(places, index)
        c["checks"] = checks + ([mixed] if mixed else [])
        if not c["skeptic"]["ids"]:  # the systematic checks give the objection, tied to its sentence
            hit = next((d for name in ("unequal_budget", "exclusion", "unequal_repeats", "split") for d in checks
                        if d["check"] == name and d["ids"]), None)
            if hit:
                c["skeptic"] = {"text": _OBJECTION[hit["check"]].format(hit["text"]), "ids": hit["ids"], "from_check": True}
        cause = _cause(claim)
        if not c["strengthen"]["ids"] and cause and claim_ids:
            c["strengthen"] = {"text": f"a control that varies {cause} while holding everything else in the "
                                       f"experiment fixed", "ids": claim_ids[:2], "from_code": True}
        c["status_label"] = _status_label(c)

    # a claim resting on the same figure as a claim with a possible confound shares that intervention
    for c in contributions:
        if c["intervention"] is not None:
            continue
        figures = {place_key(p["label"]) for p in c["places"] if p["label"].lower().startswith("fig")}
        for other in contributions:
            i = other["intervention"]
            shared = [p["label"] for p in other["places"] if place_key(p["label"]) in figures]
            if other is not c and i and i["kind"] == "confound" and shared:
                c["intervention"] = {"kind": "same_as", "claim": other["claim"], "figure": shared[0],
                                     "note": _intervention_sentence(i)}
                break

    superlatives = _superlatives(raw, index, ctx)
    main = [c for c in contributions if c["main"]] or contributions
    reported = any(p["relation"] == "same_finding" for c in main for p in c["prior"])
    if main and all(c["status"] == "overlapping" for c in main):
        verdict = "overlapping"
    elif any(c["status"] == "new" for c in main) and not reported:
        verdict = "new"  # a paper whose own main claim it says was reported before is at most incremental
    else:
        verdict = "incremental"
    new_main = [c for c in main if c["status"] == "new"]
    verdict_label = "new (weakly supported)" if verdict == "new" and all(_weak(c) for c in new_main) \
        else VERDICT_LABELS[verdict]
    reason = _clean(raw.get("reason"), 300)
    reason_ids = _ids(raw.get("reason_ids"), index)
    if not reason or not reason_ids or _VAGUE.search(reason) or str(raw.get("verdict") or "").lower() != verdict:
        reason, reason_ids = "", []  # argued for another verdict, cites nothing or is vague: code writes it
    code_reason = {"text": f"Why {verdict_label}: {_REASONS[verdict]}", "ids": _reason_ids(contributions)}
    overall = _overall(raw.get("overall"), contributions, verdict,
                       {"text": reason, "ids": reason_ids} if reason else code_reason, superlatives, index, dropped)
    return {"contributions": contributions, "superlatives": superlatives, "verdict": verdict,
            "verdict_label": verdict_label, "reason": reason, "reason_ids": reason_ids, "overall": overall,
            "design_checks": design, "journal": _journal_note(verdict, ctx), "dropped": dropped}


_REASONS = {
    "new": "no main claim is attributed by the paper to earlier work, and at least one rests on a new experiment "
           "or explanation",
    "incremental": "the paper attributes part of its main claims to earlier work it cites, builds on it, or adds "
                   "only a reanalysis, a new scope or a test of known theory",
    "overlapping": "the paper itself attributes its main claims to earlier studies and shows nothing beyond them",
}


def _reason_ids(contributions: list[dict[str, Any]]) -> list[str]:
    return sorted({i for c in contributions if c["main"] for p in c["prior"] for i in p["ids"]}
                  | {i for c in contributions if c["main"] for i in c["delta"]["ids"]}, key=sort_key)


_RELATION_TEXT = {
    "same_finding": "Already reported by earlier work, by the paper's own account",
    "builds_on": "Builds on earlier work the paper cites",
    "challenges": "Challenges earlier work the paper cites",
    "reuses_data_from": "Reuses data from earlier work",
}
_KIND_TEXT = {
    "new_experiment": "a new experiment",
    "reanalysis_of_existing_results": "a reanalysis of results already shown",
    "new_explanation": "a new explanation",
    "new_scope": "a new scope",
}


def _join(items: list[str]) -> str:
    return "; ".join(dict.fromkeys(items))


def _strength_sentences(c: dict[str, Any]) -> list[str]:
    s = c["strength"]
    out: list[str] = []
    if s["counts"]:
        parts = []
        for x in s["counts"]:
            where = " (from the caption" + (f" of {x['source']}: {', '.join(x['names'])})" if x.get("names") else ")") \
                if x["from_caption"] else (f" ({x['source']})" if x["source"] else "")
            parts.append(f"{x['n']} {x['what']}{where}")
        out.append("Supported by " + ", ".join(parts) + "."
                   + _ref(sorted({i for x in s["counts"] for i in x["ids"]}, key=sort_key)))
    else:
        out.append("Number of datasets, targets or compounds: not given in the text or captions.")
    out.append(f"Control or counter-test: {s['control']['text'].rstrip('.')}.{_ref(s['control']['ids'])}"
               if s["control"]["ids"] else "No control, ablation or counter-test is reported.")
    r = s["repeats"]
    subsets = r.get("subsets") or []
    if r["ids"]:
        differ = len({x["n"] for x in subsets if x["count"] and x["kind"] == "repeats"}) > 1 or any(
            x["exception"] and x["kind"] == "repeats" for x in subsets)
        covers = ("; the count differs by subset" if differ else f"; it covers {', '.join(r['covers'])}" if r["covers"]
                  else "; which experiments it covers is not stated")
        out.append(f"Repeats: {r['text'].rstrip('.')}{covers}.{_ref(r['ids'])}")
    else:
        out.append("Repeats (runs, seeds, replicates): not stated.")
    if len(subsets) > 1:
        out.append("By subset: " + "; ".join(
            f"{x['count'] or 'a different count, not stated'}{', tuning' if x['kind'] == 'tuning' else ''} "
            f"({x['applies_to']})" for x in subsets) + "." + _ref(sorted({i for x in subsets for i in x["ids"]}, key=sort_key)))
    for a in r.get("applies") or []:
        out.append(f"Repeats for {a['label']}: {a['text']}.")
    for e in s["error_bars"]:
        out.append(f"Error bars: the caption of {e['label']} defines them (“{e['definition']}”)." if e["caption_defines"]
                   else f"Error bars: the caption of {e['label']} does not define any; whether they are drawn cannot be "
                        f"read from the text.")
    sp = s["spread"]
    if sp["status"] == "shown":
        out.append(f"Spread values: {sp['text'].rstrip('.')}.{_ref(sp['ids'])}")
    elif sp["status"] == "referred_not_shown":
        where = f" in {sp['text']}" if sp["text"] else ""
        out.append(f"Spread values: referred to{where}, not shown — the appendix it points to is not in the file or "
                   f"does not show them.{_ref(sp['ids'])}")
    else:
        out.append("Spread values (standard deviations, errors, intervals): not shown.")
    return out


def _intervention_sentence(i: dict[str, Any]) -> str:
    if i["kind"] == "same_as":
        return f"Same intervention as {i['claim']} (both rest on {i['figure']}): {i['note']}"
    rep = i["representation"]
    if i["kind"] == "inputs":
        side = i["side_effects"]
        tail = (f" It also changes {side['text'].rstrip('.')}.{_ref(side['ids'])}" if side["ids"] else
                " Whether it also changes the sparsity, scale, type or dimensionality of the data is not stated.")
        return (f"The intervention is applied to the models' own inputs ({rep['text'] or i['family'] or 'as defined'}), so "
                f"it is not itself a confound.{_ref(rep['ids'])}{tail}")
    if i["kind"] == "same_as":
        return f"Same intervention as {i['claim']} (both rest on {i['figure']}): {i['note']}"
    how = (f"the same representation ({', '.join(i['shared']).upper()})" if i["same"]
           else f"a related representation of the same family ({i['family']}), not the same one")
    computed = f", computed from {i['family']}" + (f" ({', '.join(i['terms'])})" if i.get("terms") else "")
    return (f"Possible confound: the intervention is defined with {rep['text'].rstrip('.')[:1].lower()}"
            f"{rep['text'].rstrip('.')[1:]}{computed}{_ref(rep['ids'])}; {', '.join(i['models']['names'])} take "
            f"{i['family']} as input{_ref(i['models']['ids'])} — {how}. The evidence is not causal without ruling "
            f"this out.")


def render(result: dict[str, Any]) -> str:
    """The novelty assessment as the reader sees it: one sentence per checked item, each with its ids or place."""
    out: list[str] = []
    for c in result["contributions"]:
        scope = f"; scope: {c['scope']['text'].rstrip('.')}" if c["scope"]["text"] else ""
        ctype = f" Contribution type: {c['contribution_type'].replace('_', ' ')}." if c["contribution_type"] else ""
        out.append(f"{c['claim']} ({c.get('status_label') or c['status']}): “{c['statement'].rstrip('.')}”{scope}."
                   f"{_ref(c['scope']['ids'])}{ctype}")
        for relation in RELATIONS:
            stated = [p for p in c["prior"] if p["relation"] == relation and not p["by_scope"]]
            if stated:
                out.append(f"{_RELATION_TEXT[relation]}: {_join([x for p in stated for x in p['citations']])}."
                           f"{_ref(sorted({i for p in stated for i in p['ids']}, key=sort_key))}")
        for p in [p for p in c["prior"] if p["by_scope"]]:
            out.append(f"Within the scope of earlier work the paper cites on {p['covers']}, which this claim falls "
                       f"under: {_join(p['citations'])}.{_ref(p['ids'])}")
        if c["theory"]:
            out.append(f"Its explanation rests on {_join(c['theory']['citations'])}: the novelty is the application or "
                       f"test, not the theory.{_ref(c['theory']['ids'])}")
        d = c["delta"]
        if d["text"]:
            kind = _KIND_TEXT.get(c["evidence_kind"], "")
            out.append(f"New: {d['text'].rstrip('.')} ({kind}, {c['support']}).{_ref(d['ids'])}")
        else:
            out.append("Nothing specific is shown as new for this claim.")
        if c["places"]:
            out.append("Evidence: " + "; ".join(place_text(p) for p in c["places"]) + ".")
        if c["intervention"]:
            out.append(_intervention_sentence(c["intervention"]))
        out += _strength_sentences(c)
        for key, head in (("limits", "Limits"), ("skeptic", "A skeptic would object"), ("strengthen", "Would be stronger with")):
            if c[key]["ids"]:
                out.append(f"{head}: {c[key]['text'].rstrip('.')}.{_ref(c[key]['ids'])}")
        for check in c.get("checks") or []:
            if check["check"] == "task_metric_mix":
                out.append(f"Design check: {check['text']}.")
        for note in c["notes"]:
            out.append(f"Note: {note}.")
    for s in result["superlatives"]:
        if s["supported"]:
            missing = f"; it does not cover {', '.join(s['axes_not_covered'])}" if s["axes_not_covered"] else ""
            out.append(f"The paper's “{s['text']}” is backed on {', '.join(s['axes_backed'])} by its comparison with "
                       f"cited work{missing}.{_ref(s['ids'] + s['evidence_ids'])}")
        else:
            out.append(f"The paper's “{s['text']}” is unverified: it gives no comparison that shows it.{_ref(s['ids'])}")
    for d in result.get("design_checks") or []:
        out.append(f"Design check: {d['text']}.{_ref(d['ids'])}")
    for s in result.get("overall") or []:
        out.append(f"{s['text'].rstrip('.')}.{_ref(s['ids'])}")
    reason = result["reason"].rstrip(".") if result["reason"] else _REASONS[result["verdict"]]
    ids = result["reason_ids"] or _reason_ids(result["contributions"])
    out.append(f"Verdict: {result['verdict_label']} — {reason}.{_ref(ids)}")
    if result.get("journal"):
        out.append(f"Against the journal: {result['journal']['text']}.")
    out.append("Judged against the earlier work this paper itself cites; work it does not cite was not searched.")
    return " ".join(out)


async def assess(analysis: AnalysisResult, ctx: AnalysisContext, provider: Any) -> AnalysisResult:
    """The report with its novelty assessment written under the rules above. Never raises; without the claim
    ledger, or when the call fails, the main analysis's text stays."""
    from services.absence_audit import _paper_index

    ledger = analysis.evidence_ledger
    if ledger is None or not ledger.claims or not hasattr(provider, "generate"):
        return analysis
    payload = analysis.model_dump(mode="json")
    index = _paper_index(payload, ctx)
    if not index.sentences:
        return analysis
    claims = [c.model_dump() for c in ledger.claims]
    listed = "\n".join(f"- {c['id']}{' (main)' if c['main'] else ''} [{c['type']}] {c['statement']} — stated in "
                       f"{', '.join(c['evidence_ids'][:3])}" for c in claims)
    places = "\n".join(f"- {place_text(_place(f['label'], index, ctx))}: {f.get('caption', '')}"
                       for f in ctx.figures or [] if f.get("label"))
    prompt = (f"{_RULES}\n\nFIGURES AND TABLES IN THE FILE (printed location: caption start):\n{places or 'none found'}"
              f"\n\nCLAIMS:\n{listed}\n\n"
              f"THE PAPER, one sentence per line with its id:\n\n{_numbered(index)}")
    try:
        try:
            raw_text = await provider.generate(_SYSTEM, prompt, temperature=0.0, max_tokens=RESPONSE_MAX_TOKENS)
        except TypeError:  # a provider without max_tokens
            raw_text = await provider.generate(_SYSTEM, prompt, temperature=0.0)
        raw = provider.parse_json_response(raw_text) if hasattr(provider, "parse_json_response") else json.loads(raw_text)
    except Exception:  # noqa: BLE001
        logger.warning("Novelty assessment failed; keeping the main analysis's text", exc_info=True)
        return analysis
    if not isinstance(raw, dict):
        return analysis
    result = check(raw, index, claims, ctx)
    if not result["contributions"]:
        return analysis
    payload["novelty_assessment"] = render(result)
    payload["novelty"] = {**result, "version": NOVELTY_PROMPT_VERSION}
    if result["dropped"]:
        notes = list(payload.get("grounding_notes") or [])
        notes.append(f"Novelty assessment: {len(result['dropped'])} statement(s) left out because the paper does not "
                     f"support them (e.g. {result['dropped'][0]}).")
        payload["grounding_notes"] = notes
    return AnalysisResult.model_validate(payload)
