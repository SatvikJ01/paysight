"""Grounding checker (SPEC §11.3): every number in the headline and facts must match evidence.

Claims extracted:

* percentages (``88.5%``) and percentage points (``-1.47pp``, ``1.5 percentage points``);
* rupee amounts (``₹11,199.34``, ``₹36.67 crore``, ``Rs 4.4 lakh``, ``₹43.7M``);
* counts and other bare numbers (anything with a decimal point or thousands separator, and
  whole numbers of 100 or more);
* dates (``17 September``, ``17 Sep 2026``, ``2026-09-17``, ranges such as ``18–31 Oct``).

A numeric claim matches when some number in the cited evidence (key numbers, result cells, or
numbers inside code-generated text cells such as driver reasons) equals it within half a unit
of its last displayed digit. Fractions are also compared as percentages (0.885 matches 88.5%),
and signs are ignored ("fell 1.47pp" matches -1.47). A date
matches when it falls inside a cited evidence item's time window or appears in its results.
Small bare whole numbers (under 100, e.g. "top 5", "2 weeks"), clock times and unit phrases such as
"per 100 attempts" or "95% confidence interval" are not treated as claims.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from paysight.orchestrator.models import AnswerDraft, Evidence, RemovedClaim

ClaimKind = Literal["percent", "pp", "inr", "count", "date"]

_MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}
_MONTH_RE = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_DATE_ISO = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")
_DATE_DM = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?(?:\s*(?:–|-|to|and)\s*(\d{{1,2}})(?:st|nd|rd|th)?)?\s+"
    rf"{_MONTH_RE}(?:\s+(20\d\d))?",
    re.IGNORECASE,
)
_DATE_MD = re.compile(
    rf"\b{_MONTH_RE}\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(20\d\d))?", re.IGNORECASE
)
_TIME = re.compile(r"\b\d{1,2}:\d\d\b")
_UNIT_PHRASE = re.compile(
    r"\b(?:per|out of|every)\s+(?:100|1,?000|hundred|thousand)\b"
    r"|\b(?:90|95|99)\s?%\s+(?:confidence|CI\b|intervals?|Wilson)",
    re.IGNORECASE,
)
_NUM = r"(\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
_INR = re.compile(
    rf"(?:₹|\bRs\.?\s?|\bINR\s?)\s?{_NUM}\s*(crores?|cr\b|lakhs?|lacs?|k\b|m\b|mn\b|b\b|bn\b|"
    rf"thousand|million|billion)?",
    re.IGNORECASE,
)
_PP = re.compile(rf"[-+−]?{_NUM}\s*(?:pp\b|percentage points?|ppts?\b)", re.IGNORECASE)
_PCT = re.compile(rf"[-+−]?{_NUM}\s*(?:%|per ?cent\b)", re.IGNORECASE)
_COUNT = re.compile(rf"(?<![\w.]){_NUM}(?![\w%.])")
_MULT = {
    "crore": 1e7,
    "crores": 1e7,
    "cr": 1e7,
    "lakh": 1e5,
    "lakhs": 1e5,
    "lac": 1e5,
    "lacs": 1e5,
    "k": 1e3,
    "thousand": 1e3,
    "m": 1e6,
    "mn": 1e6,
    "million": 1e6,
    "b": 1e9,
    "bn": 1e9,
    "billion": 1e9,
}


@dataclass(frozen=True)
class Claim:
    """One number or date found in the text."""

    kind: ClaimKind
    text: str
    value: float | None = None
    tolerance: float = 0.0
    dates: tuple[date, ...] = ()
    direction: int = 0  # for pp changes: +1 rose, -1 fell, 0 not stated


_UP = {
    "rose",
    "rise",
    "rises",
    "risen",
    "increased",
    "increase",
    "increases",
    "up",
    "higher",
    "gained",
    "grew",
    "improved",
    "improvement",
    "gain",
    "climbed",
}
_DOWN = {
    "fell",
    "fall",
    "falls",
    "fallen",
    "dropped",
    "drop",
    "drops",
    "declined",
    "decline",
    "declines",
    "decrease",
    "decreased",
    "down",
    "lower",
    "lost",
    "worsened",
    "slipped",
    "dip",
    "dipped",
}


def _direction(text: str, start: int, end: int, signed: str) -> int:
    """+1/-1 when a pp change states its direction (sign, or the nearest direction word within
    five words before or two words after the number), else 0."""
    if signed[:1] in "-−":
        return -1
    if signed[:1] == "+":
        return 1
    before = re.findall(r"[a-z]+", text[:start].lower())[-5:]
    after = re.findall(r"[a-z]+", text[end:].lower())[:2]
    for word in [*after[:1], *reversed(before), *after[1:]]:
        if word in _UP:
            return 1
        if word in _DOWN:
            return -1
    return 0


def _parse_num(text: str) -> tuple[float, int]:
    clean = text.replace(",", "")
    decimals = len(clean.split(".")[1]) if "." in clean else 0
    return float(clean), decimals


def _half_unit(decimals: int) -> float:
    return 0.5 * 10 ** (-decimals) + 1e-9


def extract_claims(text: str, *, year: int) -> list[Claim]:
    """Find every checkable claim in ``text``. Matched spans are removed as they are found, so a
    number is claimed once (a date's day is not also a count)."""
    claims: list[Claim] = []
    rest = text

    def blank(match: re.Match[str]) -> None:
        nonlocal rest
        rest = rest[: match.start()] + " " * (match.end() - match.start()) + rest[match.end() :]

    for m in list(_DATE_ISO.finditer(rest)):
        claims.append(Claim("date", m.group(0), dates=(date(int(m[1]), int(m[2]), int(m[3])),)))
        blank(m)
    for m in list(_DATE_DM.finditer(rest)):
        month = _MONTHS[m.group(3)[:3].lower()]
        yr = int(m.group(4)) if m.group(4) else year
        days = [int(m.group(1))] + ([int(m.group(2))] if m.group(2) else [])
        try:
            claims.append(Claim("date", m.group(0), dates=tuple(date(yr, month, d) for d in days)))
        except ValueError:
            claims.append(Claim("date", m.group(0)))  # impossible date: will not match
        blank(m)
    for m in list(_DATE_MD.finditer(rest)):
        yr = int(m.group(3)) if m.group(3) else year
        try:
            claims.append(
                Claim(
                    "date",
                    m.group(0),
                    dates=(date(yr, _MONTHS[m.group(1)[:3].lower()], int(m.group(2))),),
                )
            )
        except ValueError:
            claims.append(Claim("date", m.group(0)))
        blank(m)
    for m in [*_TIME.finditer(rest), *_UNIT_PHRASE.finditer(rest)]:
        blank(m)  # clock times (19:00) and units ("per 100 attempts") are not claims
    for m in list(_INR.finditer(rest)):
        value, decimals = _parse_num(m.group(1))
        mult = _MULT.get((m.group(2) or "").lower(), 1.0)
        claims.append(Claim("inr", m.group(0).strip(), value * mult, _half_unit(decimals) * mult))
        blank(m)
    for pattern, kind in ((_PP, "pp"), (_PCT, "percent")):
        for m in list(pattern.finditer(rest)):
            value, decimals = _parse_num(m.group(1))
            raw = m.group(0).strip()
            way = _direction(text, m.start(), m.end(), raw) if kind == "pp" else 0
            claims.append(Claim(kind, raw, value, _half_unit(decimals), direction=way))  # type: ignore[arg-type]
            blank(m)
    for m in list(_COUNT.finditer(rest)):
        raw = m.group(1)
        value, decimals = _parse_num(raw)
        if 1900 <= value <= 2100 and "," not in raw and decimals == 0:
            continue  # a bare year
        if value < 100 and "," not in raw and decimals == 0:
            continue  # small bare whole numbers ("top 5", "2 weeks") are not claims
        claims.append(Claim("count", raw, value, _half_unit(decimals)))
    return claims


def _numbers(value: Any) -> list[float]:
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, int | float | Decimal):
        return [float(value)]
    if isinstance(value, str):
        # Code-generated text in evidence (driver reasons, data-quality details) carries computed
        # numbers too, e.g. "share 39.9% -> 55.4%" or "+39%".
        return [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", value)]
    return []


def _evidence_numbers(ev: Evidence) -> list[tuple[float, bool]]:
    """(value, sign known) for every number in the evidence; numbers read out of text carry
    no reliable sign."""
    values = [*ev.key_numbers.values(), *(v for row in ev.result for v in row.values())]
    return [(n, not isinstance(v, str)) for v in values for n in _numbers(v)]


def _evidence_dates(ev: Evidence) -> tuple[list[tuple[date, date]], set[date]]:
    windows = []
    for part in re.findall(r"(\d{4}-\d\d-\d\d)\.\.(\d{4}-\d\d-\d\d)", ev.time_window):
        windows.append((date.fromisoformat(part[0]), date.fromisoformat(part[1])))
    seen: set[date] = set()
    for value in [*ev.key_numbers.values(), *(v for row in ev.result for v in row.values())]:
        if isinstance(value, str):
            for iso in re.findall(r"\d{4}-\d\d-\d\d", value):
                seen.add(date.fromisoformat(iso))
    return windows, seen


def claim_supported(claim: Claim, cited: list[Evidence]) -> bool:
    """Whether ``claim`` matches a value in the cited evidence."""
    if claim.kind == "date":
        if not claim.dates:
            return False
        for ev in cited:
            windows, seen = _evidence_dates(ev)
            if all(d in seen or any(a <= d <= b for a, b in windows) for d in claim.dates):
                return True
        return False
    assert claim.value is not None
    target = abs(claim.value)
    for ev in cited:
        for n, signed in _evidence_numbers(ev):
            candidates = [abs(n)]
            if claim.kind in ("percent", "pp"):
                candidates.append(abs(n) * 100)
            if not any(abs(c - target) <= claim.tolerance for c in candidates):
                continue
            # A stated direction must agree with a signed value: -1.47 does not support "rose".
            if claim.direction == 0 or not signed or n == 0 or (n > 0) == (claim.direction > 0):
                return True
    return False


class GroundingReport(BaseModel):
    """Outcome of checking one draft."""

    model_config = ConfigDict(frozen=True)

    claims_total: int
    claims_supported: int
    failures: list[RemovedClaim]
    headline_ok: bool

    @property
    def score(self) -> float:
        """Share of claims supported (1.0 when there are none)."""
        return 1.0 if self.claims_total == 0 else self.claims_supported / self.claims_total


def check_draft(draft: AnswerDraft, evidence: list[Evidence], *, year: int) -> GroundingReport:
    """Check the headline (against all evidence) and each fact (against its cited evidence)."""
    by_id = {e.id: e for e in evidence}
    total = supported = 0
    failures: list[RemovedClaim] = []

    headline_ok = True
    for claim in extract_claims(draft.headline, year=year):
        total += 1
        if claim_supported(claim, evidence):
            supported += 1
        else:
            headline_ok = False
            failures.append(
                RemovedClaim(
                    text=draft.headline,
                    reason=f"headline: '{claim.text}' does not match any evidence",
                )
            )

    for fact in draft.facts:
        unknown = [i for i in fact.evidence_ids if i not in by_id]
        if not fact.evidence_ids or unknown:
            total += 1
            reason = "cites no evidence" if not fact.evidence_ids else f"cites unknown {unknown}"
            failures.append(RemovedClaim(text=fact.text, reason=reason))
            continue
        cited = [by_id[i] for i in fact.evidence_ids]
        for claim in extract_claims(fact.text, year=year):
            total += 1
            if claim_supported(claim, cited):
                supported += 1
            else:
                failures.append(
                    RemovedClaim(
                        text=fact.text,
                        reason=f"'{claim.text}' does not match {', '.join(fact.evidence_ids)}",
                    )
                )
    return GroundingReport(
        claims_total=total, claims_supported=supported, failures=failures, headline_ok=headline_ok
    )


def strip_unsupported(
    draft: AnswerDraft, report: GroundingReport
) -> tuple[AnswerDraft, list[RemovedClaim]]:
    """Remove failing facts; replace an unverifiable headline with the first verified fact."""
    bad_texts = {f.text for f in report.failures}
    facts = [f for f in draft.facts if f.text not in bad_texts]
    removed = list({f.text + f.reason: f for f in report.failures}.values())
    headline = draft.headline
    if not report.headline_ok:
        headline = (
            facts[0].text if facts else "The numbers in the drafted answer could not be verified."
        )
    return draft.model_copy(update={"facts": facts, "headline": headline}), removed


def failure_feedback(report: GroundingReport) -> str:
    """Text for the narrator's single regeneration attempt."""
    lines = [
        "",
        "YOUR PREVIOUS DRAFT FAILED THE GROUNDING CHECK. Fix these and use only numbers "
        "that appear in the cited evidence:",
    ]
    lines += [f"- {f.reason} (in: {f.text[:160]})" for f in report.failures]
    return "\n".join(lines) + "\n"
