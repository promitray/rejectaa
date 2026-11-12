"""Scientific consistency check: the paper's own values, compared with each other by code.

The model only READS: it lists every protocol value (dose, concentration, duration, molecular weight, cell
density, temperature, CO2, group size, cell-cycle phase), every percent-of claim, every compound used as a
treatment and every cell line, each with the exact words it was read from. It judges nothing.

Code then:
1. keeps only items whose quotation is really in the paper (a value the model made up is dropped);
2. works out the section of each quotation itself;
3. compares: the same parameter of the same experiment in different sections ("48 h" in Methods, "24 h" in
   Results), percent-of claims against the numbers they name ("50 mg/kg is 10-20% of an MTD of 0.5 mg/kg"),
   units that do not fit the quantity, values far outside what the set-up allows (3 x 10^6 cells per well of
   a 96-well plate), and a compound used as a treatment that is not the one the paper studies;
4. looks each cell line up in Cellosaurus, which flags lines known to be contaminated or misidentified.

Each finding carries its quotations, so a reader can check it in seconds. Nothing here raises: a failure gives
status "not_checked" and the analysis goes on.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Any

import httpx

from models import CellLineCheck, ConsistencyFinding, ConsistencyQuote, ConsistencyReport, ParsedDocument
from services import paper_profile as pp
from services.citation_http import NOT_FOUND, OK, limiter
from services.textfold import GREEK_TABLE

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT_VERSION = "2026-11-13.2"
EXTRACTION_BUDGET_CHARS = 120_000
TOTAL_BUDGET_SECONDS = 120.0
MAX_FACTS = 80
MAX_FINDINGS = 8
MAX_CELL_LINES = 12
CACHE_TTL_SECONDS = 7 * 24 * 3600.0
MAX_CACHE_ENTRIES = 300
CELLOSAURUS_URL = "https://api.cellosaurus.org/search/cell-line"
CELLOSAURUS_ENTRY_URL = "https://api.cellosaurus.org/cell-line/{accession}"
CELLOSAURUS_ROWS = 300  # derived lines (HepG2-CD81, ...) rank above the parent line, so read many and match exactly
# A "Problematic cell line" comment that starts like this means the cells are not what the paper says they are.
_SERIOUS_PROBLEM = re.compile(r"(?i)^\s*(?:contaminated|misidentified)")
USER_AGENT = "Rejecta/1.0 (mailto:hello@rejecta.ai)"
REQUEST_TIMEOUT = 8.0

RELATIVE_TOLERANCE = 0.02  # 2 %: rounding between Abstract and Results is not a contradiction
PERCENT_CLAIM_SLACK = 1.25  # a percent-of claim may be this far outside its stated range before it is flagged
MAX_CELLS_PER_96_WELL = 2e5  # usual seeding is 10^3 to 10^5 cells per well

PARAMETERS = (
    "dose", "concentration", "duration", "molecular_weight", "cell_density", "temperature", "co2",
    "sample_size", "cell_cycle_phase",
)
SINGLE_VALUED = frozenset({"molecular_weight"})  # one compound has one molecular weight
CATEGORICAL = frozenset({"cell_cycle_phase"})
TREATMENT_ROLES_OK = frozenset({"studied", "positive_control", "negative_control", "vehicle", "reagent", "stain", "standard"})

_SYSTEM = (
    "You read scientific manuscripts and extract facts exactly as written. You never judge, correct or explain. "
    "Return ONLY valid JSON."
)

_INSTRUCTIONS = """Extract from the manuscript below, as JSON with exactly these keys:

{
  "studied_compounds": ["<the compound(s) or material(s) this paper is about>"],
  "facts": [
    {"experiment": "<short name of the assay or procedure, e.g. 'MTT assay', 'cell cycle analysis', 'xenograft dosing', 'compound characterisation'. Use EXACTLY the same name every time the same procedure is mentioned, in any section>",
     "parameter": "<one of: PARAMS>",
     "values": [<numbers as written, e.g. 5, 25, 50; for 3 x 10^6 write 3000000>],
     "value_text": "<for cell_cycle_phase only: the phase as written, e.g. 'G0/G1' or 'sub-G1'; else empty>",
     "unit": "<the unit exactly as written, e.g. 'mg/kg', 'µM', 'h', 'cells/well', '%', 'µg/ml'; empty if none>",
     "quote": "<5-30 words copied EXACTLY from the manuscript that contain this value>"}
  ],
  "percent_claims": [
    {"quote": "<exact words that say a value is some percent of another, e.g. '50 and 100 mg/kg/day, which is 10-20% of the MTD'>",
     "value": <the value said to be a percent of the other>, "unit": "<its unit>",
     "percent_low": <lower percent>, "percent_high": <upper percent>,
     "of_value": <the value it is a percent of, as written elsewhere>, "of_unit": "<its unit>",
     "of_quote": "<5-30 exact words where that other value is written>"}
  ],
  "treatments": [
    {"compound": "<a compound the cells or animals were treated with>",
     "role": "<one of: studied, positive_control, negative_control, vehicle, reagent, stain, standard, unexplained>",
     "quote": "<5-30 exact words>"}
  ],
  "cell_lines": [{"name": "<cell line name as written, e.g. HepG2, MGC-803>", "quote": "<5-30 exact words>"}]
}

Rules:
- Read the Abstract, Methods, Results, Discussion and figure captions. Take every value of the listed parameters,
  in EVERY section where it appears (the same value in the Abstract and in the Methods is two facts).
- Protocol values only: what was given, used or set (doses, concentrations, durations, densities, temperatures,
  CO2, group sizes), molecular weights, and the cell-cycle phase the paper says cells were arrested in. Not
  measured outcomes such as percentages of viable cells or IC50 values.
- "unexplained" is for a compound named as a treatment that is neither the studied compound nor a control,
  vehicle, reagent, stain or standard.
- Include values given in passing in the Results and in figure captions ("after 24 h of treatment",
  "cells treated with 50 µM"), with the SAME experiment name as in the Methods: a Results value that differs
  from the Methods is exactly what this is for.
- Quotes must be copied character for character. Do not paraphrase. At most MAXFACTS facts.
- Empty lists are fine. Do not invent anything that is not in the text.

MANUSCRIPT:
"""


# ---------------------------------------------------------------- text matching

def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower().translate(GREEK_TABLE)  # µ -> u, ϕ -> phi
    text = text.replace("×", "x")
    return re.sub(r"[^a-z0-9]+", "", text)


@dataclass
class _Located:
    section: str  # role or name of the section, "abstract", or ""
    found: bool


class _Paper:
    """The paper's text in sections, normalised once, for finding where a quotation sits."""

    def __init__(self, parsed: ParsedDocument) -> None:
        self.parts: list[tuple[str, str]] = [("abstract", _norm(parsed.abstract))]
        self.raw_parts: list[tuple[str, str]] = [("abstract", parsed.abstract)]
        for name, text in parsed.sections.items():
            role = parsed.section_roles.get(name, "")
            label = role if role in pp.CANONICAL_ROLES else (name if role else "")
            self.parts.append((label, _norm(text)))
            self.raw_parts.append((label, text))
        self.whole = _norm(parsed.full_text)
        self.title_and_abstract = _norm(f"{parsed.title} {parsed.abstract}")

    def locate(self, quote: str) -> _Located:
        key = _norm(quote)
        if len(key) < 12:
            return _Located("", False)
        for label, text in self.parts:
            if key in text:
                return _Located(label, True)
        return _Located("", key in self.whole)

    def count(self, words: str) -> int:
        key = _norm(words)
        return self.whole.count(key) if key else 0


# ---------------------------------------------------------------- units

_UNITS: dict[str, tuple[str, float]] = {}


def _add_units(family: str, table: dict[str, float]) -> None:
    for unit, factor in table.items():
        _UNITS[unit] = (family, factor)


_add_units("molar", {"m": 1, "mm": 1e-3, "um": 1e-6, "nm": 1e-9, "pm": 1e-12, "mol/l": 1, "mmol/l": 1e-3,
                     "umol/l": 1e-6, "nmol/l": 1e-9, "umoll1": 1e-6, "mmoll1": 1e-3})
_add_units("mass_conc", {"g/l": 1, "mg/ml": 1, "ug/ml": 1e-3, "ng/ml": 1e-6, "mg/l": 1e-3, "ug/l": 1e-6,
                         "g/ml": 1e3, "mgml1": 1, "ugml1": 1e-3, "ngml1": 1e-6})
_add_units("dose_per_kg", {"mg/kg": 1, "ug/kg": 1e-3, "g/kg": 1e3, "mg/kg/day": 1, "mg/kg/d": 1, "mgkg1": 1,
                           "mg/kgbodyweight": 1, "mg/kgbw": 1, "mg/kg/dayip": 1, "mgkg1day1": 1, "mgkg1d1": 1})
_add_units("dose_per_animal", {"mg/mouse": 1, "mg/animal": 1, "mg/rat": 1, "ug/mouse": 1e-3, "mg/mice": 1})
_add_units("mass", {"mg": 1, "ug": 1e-3, "g": 1e3, "ng": 1e-6})
_add_units("time", {"h": 1, "hr": 1, "hrs": 1, "hour": 1, "hours": 1, "min": 1 / 60, "mins": 1 / 60,
                    "minute": 1 / 60, "minutes": 1 / 60, "s": 1 / 3600, "sec": 1 / 3600, "d": 24, "day": 24,
                    "days": 24, "week": 168, "weeks": 168, "wk": 168})
_add_units("molar_mass", {"g/mol": 1, "gmol1": 1, "da": 1, "kda": 1e3, "g/mole": 1, "amu": 1})
_add_units("cells_per_well", {"cells/well": 1, "cellsperwell": 1, "cells/well96wellplate": 1})
_add_units("cells_per_ml", {"cells/ml": 1, "cellsml1": 1, "cellspermilliliter": 1, "cellsperml": 1})
_add_units("cells_per_dish", {"cells/dish": 1, "cells/plate": 1, "cells/flask": 1})
_add_units("temperature", {"c": 1, "degc": 1, "oc": 1, "celsius": 1})
_add_units("percent", {"%": 1, "percent": 1, "%v/v": 1, "%w/v": 1})
_add_units("count", {"": 1, "mice": 1, "rats": 1, "animals": 1, "pergroup": 1, "mice/group": 1, "animals/group": 1})

ALLOWED_FAMILIES: dict[str, frozenset[str]] = {
    "dose": frozenset({"dose_per_kg", "dose_per_animal", "mass", "molar", "mass_conc"}),
    "concentration": frozenset({"molar", "mass_conc", "percent"}),
    "duration": frozenset({"time"}),
    "molecular_weight": frozenset({"molar_mass", "count"}),
    "cell_density": frozenset({"cells_per_well", "cells_per_ml", "cells_per_dish", "count"}),
    "temperature": frozenset({"temperature"}),
    "co2": frozenset({"percent"}),
    "sample_size": frozenset({"count"}),
}


def unit_family(unit: str) -> tuple[str, float] | None:
    text = unicodedata.normalize("NFKC", unit or "").lower().replace("µ", "u").replace("μ", "u")
    text = text.replace("°", "").replace("·", "").replace("−", "-").replace("⁻", "-").replace("¹", "1")
    text = re.sub(r"\s+", "", text)
    if text in _UNITS:
        return _UNITS[text]
    compact = re.sub(r"[^a-z0-9/%]", "", text)
    if compact in _UNITS:
        return _UNITS[compact]
    flat = re.sub(r"[^a-z0-9%]", "", text)
    return _UNITS.get(flat)


# ---------------------------------------------------------------- reading the model's answer

@dataclass
class _Fact:
    experiment: str
    parameter: str
    values: list[float]
    value_text: str
    unit: str
    quote: str
    section: str


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").strip())
        except ValueError:
            return None
    return None


def _clean_text(value: Any, limit: int = 400) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _facts(raw: Any, paper: _Paper) -> list[_Fact]:
    out: list[_Fact] = []
    for item in (raw if isinstance(raw, list) else [])[:MAX_FACTS]:
        if not isinstance(item, dict):
            continue
        parameter = _clean_text(item.get("parameter"), 40).lower()
        quote = _clean_text(item.get("quote"))
        where = paper.locate(quote)
        if parameter not in PARAMETERS or not where.found:
            continue
        values = [v for v in (_number(x) for x in (item.get("values") or [])) if v is not None]
        value_text = _clean_text(item.get("value_text"), 60)
        if parameter in CATEGORICAL:
            if not value_text or _norm(value_text) not in _norm(quote):
                continue
        elif not values or not re.search(r"\d", quote):
            continue
        out.append(
            _Fact(
                experiment=_clean_text(item.get("experiment"), 80),
                parameter=parameter, values=values, value_text=value_text,
                unit=_clean_text(item.get("unit"), 30), quote=quote, section=where.section,
            )
        )
    return out


# ---------------------------------------------------------------- a second reading, by code

# Words that name no particular experiment ("analysis", "assay"), left out when an experiment's name is looked for.
_GENERIC_WORDS = frozenset({
    "analysis", "assay", "assays", "experiment", "experiments", "measurement", "measurements", "treatment", "study",
    "test", "tests", "procedure", "protocol", "the", "and", "of", "for", "with", "in", "dosing", "determination",
})
_DURATION = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(h|hr|hrs|hours?|min|mins|minutes?|days?|weeks?)\b", re.IGNORECASE)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")


def _durations_by_name(facts: list[_Fact], paper: _Paper) -> list[_Fact]:
    """Treatment times outside the Methods that the model did not list, read by code.

    For each experiment whose treatment time the Methods give, every sentence of the Abstract and the Results
    that names that experiment ("... after 24 h of treatment showed ... cell cycle arrest") is searched for a
    time. A single reading pass misses values given in passing; this one cannot, and it quotes the sentence.
    """
    # one name per experiment, however the model capitalised it
    named = {
        f.experiment.lower(): f.experiment
        for f in sorted(facts, key=lambda f: f.experiment)
        if f.parameter == "duration" and f.section == pp.ROLE_METHODS and f.experiment
    }
    known = [_norm(f.quote) for f in facts if f.parameter == "duration"]
    found: list[_Fact] = []
    for experiment in sorted(named.values(), key=str.lower):
        words = [w for w in re.findall(r"[a-z0-9]+", experiment.lower()) if w not in _GENERIC_WORDS and len(w) >= 3]
        if not words:
            continue
        for section, text in paper.raw_parts:
            if section in ("", pp.ROLE_METHODS):
                continue
            for sentence in _SENTENCE_END.split(re.sub(r"\s+", " ", text)):
                lowered = sentence.lower()
                if not all(re.search(rf"\b{re.escape(w)}", lowered) for w in words):
                    continue
                if any(quote and quote in _norm(sentence) for quote in known):  # the model already read it
                    continue
                times = _DURATION.findall(sentence)
                if len(times) != 1 or len(sentence) > 400:  # one time, so it is clearly the treatment time
                    continue
                value, unit = times[0]
                found.append(_Fact(experiment, "duration", [float(value)], "", unit, sentence.strip(), section))
    return found


# The phase cells are said to be arrested in, read by code from every sentence about an arrest. The single most
# telling inconsistency (an Abstract that says sub-G1 while the Results say G0/G1) must not depend on the model.
_PHASE = re.compile(r"(?<![\w/-])(sub-?G1|G0/G1|G1/S|G2/M|G0|G1|G2)(?![\w/-])", re.IGNORECASE)
CODE_PHASE_EXPERIMENT = "cell cycle arrest"


def _phases_by_code(paper: _Paper) -> list[_Fact]:
    found: list[_Fact] = []
    for section, text in paper.raw_parts:
        if section in ("", pp.ROLE_METHODS, pp.ROLE_INTRODUCTION):
            continue
        for sentence in _SENTENCE_END.split(re.sub(r"\s+", " ", text)):
            if not re.search(r"\barrest", sentence, re.IGNORECASE) or len(sentence) > 400:
                continue
            phases = {re.sub(r"(?i)sub-?g1", "sub-G1", p).upper().replace("SUB-G1", "sub-G1") for p in _PHASE.findall(sentence)}
            if len(phases) == 1:  # one phase named, so it is clearly the phase of the arrest
                phase = phases.pop()
                found.append(_Fact(CODE_PHASE_EXPERIMENT, "cell_cycle_phase", [], phase, "", sentence.strip(), section))
    return found


# ---------------------------------------------------------------- the checks

def _fmt(value: float) -> str:
    """5, 0.5, 543.8, and 3 x 10^6 rather than 3e+06."""
    if value and abs(value) >= 1e4:
        mantissa, exponent = f"{value:e}".split("e")
        return f"{float(mantissa):g} × 10^{int(exponent)}"
    return f"{value:g}"


def _same(a: float, b: float) -> bool:
    return abs(a - b) <= RELATIVE_TOLERANCE * max(abs(a), abs(b), 1e-12)


def _section_name(section: str) -> str:
    return {"": "the text", "abstract": "the Abstract", "methods": "the Methods", "results": "the Results",
            "discussion": "the Discussion", "introduction": "the Introduction"}.get(section, f'"{section}"')


_PARAMETER_WORDS = {
    "dose": "dose", "concentration": "concentration", "duration": "treatment time", "molecular_weight": "molecular weight",
    "cell_density": "cell density", "temperature": "temperature", "co2": "CO2", "sample_size": "group size",
    "cell_cycle_phase": "cell-cycle phase",
}


def _quote(fact: _Fact) -> ConsistencyQuote:
    values = fact.value_text or ", ".join(_fmt(v) for v in fact.values)
    return ConsistencyQuote(quote=fact.quote, section=fact.section, values=f"{values} {fact.unit}".strip())


def _compare_groups(facts: list[_Fact]) -> list[ConsistencyFinding]:
    findings: list[ConsistencyFinding] = []
    groups: dict[tuple[str, str], list[_Fact]] = {}
    for fact in facts:
        if fact.experiment:
            groups.setdefault((fact.experiment.lower(), fact.parameter), []).append(fact)

    for (_key, parameter), group in sorted(groups.items()):
        experiment = group[0].experiment  # as the model wrote it, for the message
        sections = {f.section for f in group}
        label = _PARAMETER_WORDS[parameter]
        if parameter in CATEGORICAL:
            by_section: dict[str, set[str]] = {}
            for f in group:
                by_section.setdefault(f.section, set()).add(_norm(f.value_text))
            names = sorted(by_section)
            for i, a in enumerate(names):
                for b in names[i + 1:]:
                    if not (by_section[a] & by_section[b]):
                        fa = next(f for f in group if f.section == a)
                        fb = next(f for f in group if f.section == b)
                        findings.append(ConsistencyFinding(
                            kind="category_mismatch", severity="major",
                            message=(f"{experiment[:1].upper() + experiment[1:]}: {_section_name(a)} gives the {label} as "
                                     f"{fa.value_text}, {_section_name(b)} as {fb.value_text}. These are different."),
                            quotes=[_quote(fa), _quote(fb)],
                        ))
            continue

        # units: the same experiment's doses per kilogram and per animal cannot both be right
        typed = [(f, unit_family(f.unit)) for f in group]
        families = {fam[0] for _, fam in typed if fam}
        if parameter == "dose" and {"dose_per_kg", "dose_per_animal"} <= families:
            a = next(f for f, fam in typed if fam and fam[0] == "dose_per_kg")
            b = next(f for f, fam in typed if fam and fam[0] == "dose_per_animal")
            findings.append(ConsistencyFinding(
                kind="unit_mismatch", severity="minor",
                message=f"{experiment[:1].upper() + experiment[1:]}: doses are given both per kilogram ({a.unit}) and per animal ({b.unit}).",
                quotes=[_quote(a), _quote(b)],
            ))

        if len(sections) < 2:
            continue
        by_family: dict[str, list[tuple[_Fact, float]]] = {}
        for f, fam in typed:
            if fam is None:
                continue
            for v in f.values:
                by_family.setdefault(fam[0], []).append((f, v * fam[1]))
        for family, items in sorted(by_family.items()):
            if parameter in SINGLE_VALUED:
                first_f, first_v = items[0]
                other = next(((f, v) for f, v in items if f.section != first_f.section and not _same(v, first_v)), None)
                if other is not None:
                    findings.append(ConsistencyFinding(
                        kind="value_mismatch", severity="major",
                        message=(f"{experiment[:1].upper() + experiment[1:]}: the {label} is {_fmt(first_f.values[0])} {first_f.unit} in "
                                 f"{_section_name(first_f.section)} but {_fmt(other[0].values[0])} {other[0].unit} in "
                                 f"{_section_name(other[0].section)}.").replace("  ", " "),
                        quotes=[_quote(first_f), _quote(other[0])],
                    ))
                continue
            # several values are normal (a dose range); a value used elsewhere must be one the Methods give
            reference = [(f, v) for f, v in items if f.section == pp.ROLE_METHODS]
            if not reference:
                continue
            allowed = [v for _, v in reference]
            for f, v in items:
                if f.section == pp.ROLE_METHODS or any(_same(v, r) for r in allowed):
                    continue
                shown = ", ".join(sorted({_fmt(x) for x in reference[0][0].values}, key=float))
                raw_value = next((x for x in f.values if _same(x * (unit_family(f.unit) or ("", 1))[1], v)), v)
                findings.append(ConsistencyFinding(
                    kind="value_mismatch", severity="major",
                    message=(f"{experiment[:1].upper() + experiment[1:]}: {_section_name(f.section)} gives a {label} of "
                             f"{_fmt(raw_value)} {f.unit}, which is not among the values in the Methods "
                             f"({shown} {reference[0][0].unit}).").replace("  ", " "),
                    quotes=[_quote(reference[0][0]), _quote(f)],
                ))
                break  # one finding per experiment and parameter is enough to send the reader to it
    return findings


def _plausibility(facts: list[_Fact]) -> list[ConsistencyFinding]:
    findings: list[ConsistencyFinding] = []
    for fact in facts:
        family = unit_family(fact.unit)
        allowed = ALLOWED_FAMILIES.get(fact.parameter)
        if fact.parameter == "co2" and (family is None or family[0] != "percent") and fact.unit:
            findings.append(ConsistencyFinding(
                kind="unit_mismatch", severity="minor",
                message=f'The CO2 atmosphere is given as "{_fmt(fact.values[0])} {fact.unit}", not as a percentage.',
                quotes=[_quote(fact)],
            ))
        elif allowed and family is not None and family[0] not in allowed and fact.parameter != "co2":
            findings.append(ConsistencyFinding(
                kind="unit_mismatch", severity="minor",
                message=f'The {_PARAMETER_WORDS[fact.parameter]} is given in "{fact.unit}", which is not a unit of {_PARAMETER_WORDS[fact.parameter]}.',
                quotes=[_quote(fact)],
            ))
        if (
            fact.parameter == "cell_density" and family is not None and family[0] in ("cells_per_well", "count")
            and re.search(r"96[\s\-]*well", fact.quote, re.IGNORECASE)
            and any(v * family[1] > MAX_CELLS_PER_96_WELL for v in fact.values)
        ):
            top = max(fact.values)
            findings.append(ConsistencyFinding(
                kind="implausible_value", severity="major",
                message=(f"{_fmt(top)} cells per well of a 96-well plate is far above the usual 10^3 to 10^5. "
                         "Check the number: it changes the viability and IC50 results."),
                quotes=[_quote(fact)],
            ))
    return findings


def _percent_claims(raw: Any, paper: _Paper) -> list[ConsistencyFinding]:
    findings: list[ConsistencyFinding] = []
    flagged: set[str] = set()  # "50 and 100 mg/kg ... 10-20% of the MTD" is one claim, not two findings
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        quote, of_quote = _clean_text(item.get("quote")), _clean_text(item.get("of_quote"))
        here, there = paper.locate(quote), paper.locate(of_quote)
        value, of_value = _number(item.get("value")), _number(item.get("of_value"))
        low, high = _number(item.get("percent_low")), _number(item.get("percent_high"))
        unit, of_unit = _clean_text(item.get("unit"), 30), _clean_text(item.get("of_unit"), 30)
        if not (here.found and there.found) or None in (value, of_value, low) or not of_value:
            continue
        high = high if high is not None else low
        a, b = unit_family(unit), unit_family(of_unit)
        if a is None or b is None or a[0] != b[0]:
            continue
        percent = 100 * value * a[1] / (of_value * b[1])  # type: ignore[operator]
        if low / PERCENT_CLAIM_SLACK <= percent <= high * PERCENT_CLAIM_SLACK:  # type: ignore[operator]
            continue
        if _norm(quote) in flagged:
            continue
        flagged.add(_norm(quote))
        stated = f"{_fmt(low)}-{_fmt(high)}%" if high != low else f"{_fmt(low)}%"  # type: ignore[arg-type]
        findings.append(ConsistencyFinding(
            kind="percent_claim", severity="major",
            message=(f"{_fmt(value)} {unit} is described as {stated} of {_fmt(of_value)} {of_unit}, but it is "  # type: ignore[arg-type]
                     f"{percent:,.0f}% of it. The doses, the reference value or the percentage is wrong."),
            quotes=[
                ConsistencyQuote(quote=quote, section=here.section, values=f"{_fmt(value)} {unit}"),  # type: ignore[arg-type]
                ConsistencyQuote(quote=of_quote, section=there.section, values=f"{_fmt(of_value)} {of_unit}"),  # type: ignore[arg-type]
            ],
        ))
    return findings


def _foreign_compounds(raw: Any, studied: list[str], paper: _Paper) -> list[ConsistencyFinding]:
    findings: list[ConsistencyFinding] = []
    studied_keys = [_norm(s) for s in studied if _norm(s)]
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        compound, role, quote = _clean_text(item.get("compound"), 80), _clean_text(item.get("role"), 30).lower(), _clean_text(item.get("quote"))
        key = _norm(compound)
        where = paper.locate(quote)
        if not key or len(key) < 4 or not where.found or key not in _norm(quote):
            continue
        if role in TREATMENT_ROLES_OK and role != "studied":
            continue
        if any(key in s or s in key for s in studied_keys) or key in paper.title_and_abstract:
            continue
        if paper.count(compound) > 3:  # named throughout: a real part of this study
            continue
        findings.append(ConsistencyFinding(
            kind="foreign_compound", severity="major",
            message=(f'"{compound}" is named as a treatment in {_section_name(where.section)}, but this paper studies '
                     f"{', '.join(studied) or 'another compound'} and does not mention it elsewhere. This passage may "
                     "come from another protocol; it is unclear what was done."),
            quotes=[ConsistencyQuote(quote=quote, section=where.section, values=compound)],
        ))
    return findings


# ---------------------------------------------------------------- cell lines (Cellosaurus)

_cell_cache: dict[str, tuple[float, CellLineCheck]] = {}


async def _cellosaurus(client: httpx.AsyncClient, name: str) -> CellLineCheck:
    key = _norm(name)
    hit = _cell_cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL_SECONDS:
        return hit[1].model_copy()
    search = await limiter("Cellosaurus").get(
        client, CELLOSAURUS_URL,
        params={"q": name.replace('"', ""), "fields": "id,ac,sy", "format": "json", "rows": str(CELLOSAURUS_ROWS)},
        headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT,
    )
    if search.kind == NOT_FOUND:
        result = CellLineCheck(name=name, status="not_found")
    elif search.kind != OK or search.response is None:
        return CellLineCheck(name=name, status="not_checked")
    else:
        accession = _exact_accession(name, search.response.json())
        if accession is None:
            result = CellLineCheck(name=name, status="not_found")
        else:
            record = await limiter("Cellosaurus").get(
                client, CELLOSAURUS_ENTRY_URL.format(accession=accession),
                params={"fields": "id,ac,cc", "format": "json"}, headers={"User-Agent": USER_AGENT},
                timeout=REQUEST_TIMEOUT,
            )
            if record.kind != OK or record.response is None:
                return CellLineCheck(name=name, status="not_checked", accession=accession)
            result = _cell_line_record(name, accession, record.response.json())
    _cell_cache[key] = (time.monotonic(), result)
    return result.model_copy()


def _exact_accession(name: str, payload: dict[str, Any]) -> str | None:
    """The accession whose identifier (or else a synonym) is exactly this name. A near match is another line."""
    key = _norm(name)
    entries = ((payload.get("Cellosaurus") or {}).get("cell-line-list")) or []
    by_synonym: str | None = None
    for entry in entries:
        names = entry.get("name-list") or []
        accession = next((a.get("value") for a in entry.get("accession-list") or [] if a.get("type") == "primary"), None)
        if not accession:
            continue
        if any(_norm(n.get("value", "")) == key for n in names if n.get("type") == "identifier"):
            return accession
        if by_synonym is None and any(_norm(n.get("value", "")) == key for n in names if n.get("type") == "synonym"):
            by_synonym = accession
    return by_synonym


def _cell_line_record(name: str, accession: str, payload: dict[str, Any]) -> CellLineCheck:
    entries = ((payload.get("Cellosaurus") or {}).get("cell-line-list")) or []
    problems: list[str] = []
    for comment in (entries[0].get("comment-list") if entries else None) or []:
        if comment.get("category") == "Problematic cell line" and comment.get("value") not in problems:
            problems.append(str(comment.get("value")))
    if problems:
        return CellLineCheck(name=name, status="problematic", accession=accession, note=" ".join(problems)[:500])
    return CellLineCheck(name=name, status="no_known_problem", accession=accession)


async def _cell_lines(raw: Any, paper: _Paper, client: httpx.AsyncClient) -> list[CellLineCheck]:
    named: dict[str, tuple[str, str]] = {}
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        name, quote = _clean_text(item.get("name"), 40), _clean_text(item.get("quote"))
        key = _norm(name)
        if len(key) < 3 or key in named or key not in _norm(quote) or not paper.locate(quote).found:
            continue
        named[key] = (name, quote)
        if len(named) >= MAX_CELL_LINES:
            break
    checks = await asyncio.gather(*(_cellosaurus(client, name) for name, _ in named.values()), return_exceptions=True)
    out: list[CellLineCheck] = []
    for (name, quote), check in zip(named.values(), checks):
        result = check if isinstance(check, CellLineCheck) else CellLineCheck(name=name, status="not_checked")
        result.quote = quote
        out.append(result)
    return out


# ---------------------------------------------------------------- the check

def _paper_text(parsed: ParsedDocument) -> str:
    parts = [f"## Abstract\n{parsed.abstract}"]
    if parsed.sections and parsed.section_roles:
        parts += [f"## {name}\n{text}" for name, text in parsed.sections.items()]
    else:
        parts.append(parsed.body or parsed.full_text)
    text = "\n\n".join(parts)
    if len(text) > EXTRACTION_BUDGET_CHARS:
        head = int(EXTRACTION_BUDGET_CHARS * 0.7)
        text = text[:head] + "\n[...]\n" + text[-(EXTRACTION_BUDGET_CHARS - head):]
    return text


_extraction_cache: dict[str, tuple[float, dict[str, Any]]] = {}


async def _extract(parsed: ParsedDocument, provider: Any) -> dict[str, Any]:
    text = _paper_text(parsed)
    model = f"{type(provider).__name__}:{getattr(provider, 'model', '') or getattr(provider, 'model_name', '')}"
    key = hashlib.sha256(f"{EXTRACTION_PROMPT_VERSION}\x1f{model}\x1f{text}".encode("utf-8")).hexdigest()
    hit = _extraction_cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]
    prompt = _INSTRUCTIONS.replace("PARAMS", ", ".join(PARAMETERS)).replace("MAXFACTS", str(MAX_FACTS)) + text
    raw = await provider.generate(_SYSTEM, prompt, temperature=0.0)
    data = provider.parse_json_response(raw) if hasattr(provider, "parse_json_response") else json.loads(raw)
    if len(_extraction_cache) >= MAX_CACHE_ENTRIES:
        _extraction_cache.pop(next(iter(_extraction_cache)))
    _extraction_cache[key] = (time.monotonic(), data)
    return data


def applies_to(parsed: ParsedDocument) -> bool:
    """Reviews, perspectives and editorials quote other people's numbers: comparing them says nothing."""
    return parsed.article_type not in (*pp.THEMATIC_TYPES, *pp.NON_RESEARCH_TYPES)


def findings_from(data: dict[str, Any], paper: _Paper) -> tuple[list[ConsistencyFinding], int]:
    facts = _facts(data.get("facts"), paper)
    facts += _durations_by_name(facts, paper)
    facts += _phases_by_code(paper)
    studied = [_clean_text(s, 80) for s in (data.get("studied_compounds") or []) if _clean_text(s, 80)]
    findings = (
        _percent_claims(data.get("percent_claims"), paper)
        + _compare_groups(facts)
        + _foreign_compounds(data.get("treatments"), studied, paper)
        + _plausibility(facts)
    )
    # the same finding twice (the model listed a fact twice, or the model and code both read it) is one finding
    unique: dict[str, ConsistencyFinding] = {}
    for finding in findings:
        if finding.kind == "category_mismatch":
            key = "phase:" + "|".join(sorted(_norm(q.values) for q in finding.quotes))
        else:
            key = finding.message
        unique.setdefault(key, finding)
    ordered = sorted(unique.values(), key=lambda f: (f.severity != "major",))
    return ordered[:MAX_FINDINGS], len(facts)


async def _check(parsed: ParsedDocument, provider: Any, client: httpx.AsyncClient) -> ConsistencyReport:
    paper = _Paper(parsed)
    data = await _extract(parsed, provider)
    findings, facts_checked = findings_from(data, paper)
    cell_lines = await _cell_lines(data.get("cell_lines"), paper, client)
    for line in cell_lines:
        if line.status != "problematic":
            continue
        serious = bool(_SERIOUS_PROBLEM.match(line.note))
        message = (
            f"Cellosaurus lists {line.name} ({line.accession}) as a problematic cell line: {line.note} "
            + ("Results in this line may not apply to the cancer type the paper names." if serious
               else "Check that the paper describes the line as what it is.")
        )
        findings.append(ConsistencyFinding(
            kind="problematic_cell_line", severity="major" if serious else "minor", message=message,
            quotes=[ConsistencyQuote(quote=line.quote, section=paper.locate(line.quote).section, values=line.name)],
        ))
    return ConsistencyReport(status="checked", findings=findings, facts_checked=facts_checked, cell_lines=cell_lines)


async def check_consistency(parsed: ParsedDocument, provider: Any, client: httpx.AsyncClient | None = None) -> ConsistencyReport:
    """Compare the paper's own values. Never raises: a failure gives status "not_checked"."""
    if not applies_to(parsed):
        return ConsistencyReport(status="not_applicable", note="Not done for reviews, perspectives and editorials.")
    own_client = client is None
    http = client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True)
    try:
        return await asyncio.wait_for(_check(parsed, provider, http), TOTAL_BUDGET_SECONDS)
    except Exception:  # noqa: BLE001 - the analysis goes on without this check
        logger.warning("Consistency check failed", exc_info=True)
        return ConsistencyReport(status="not_checked", note="The consistency check could not be completed this time.")
    finally:
        if own_client:
            await http.aclose()


def clear_cache() -> None:
    _extraction_cache.clear()
    _cell_cache.clear()
