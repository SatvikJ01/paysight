"""Resolve normalized time expressions against ``AS_OF_DATE`` (SPEC §7.7).

The LLM never computes dates. It emits one of these expressions, and this module resolves it:

=================================  ===================================================
``last_n_days:7``                  the last 7 complete days ending on AS_OF_DATE
``last_n_weeks:2``                 the last 14 complete days ("last week" = last 7 days)
``this_month`` / ``last_month``    the AS_OF month / the previous calendar month
``month:2026-09``                  a calendar month
``day:2026-09-17``                 a single day
``explicit:2026-10-18..2026-10-31`` an explicit inclusive range
``relative_to_named:festive_season`` a named business period (``:before`` = the
                                    equal-length window just before it)
``previous_period``                (compare only) the equal-length window just before
=================================  ===================================================

Windows are clipped to the data range and every clip is reported as a note, never silently.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

from pydantic import BaseModel, ConfigDict

from paysight.semantic.models import TimeWindow

# Business calendar used by ``relative_to_named``.
NAMED_PERIODS: dict[str, tuple[date, date]] = {
    "festive_season": (date(2026, 10, 18), date(2026, 10, 31)),
}


class DateResolutionError(ValueError):
    """A time expression is malformed or falls entirely outside the data."""


class ResolvedWindow(BaseModel):
    """A resolved window plus the expression it came from and any notes (e.g. clipping)."""

    model_config = ConfigDict(frozen=True)

    expression: str
    window: TimeWindow
    notes: list[str] = []


def _month(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def _parse_date(text: str, expression: str) -> date:
    try:
        return date.fromisoformat(text.strip())
    except ValueError as exc:
        raise DateResolutionError(f"bad date {text!r} in {expression!r}") from exc


def _preceding(window: TimeWindow) -> tuple[date, date]:
    end = window.start - timedelta(days=1)
    return end - timedelta(days=window.days - 1), end


def _raw_range(expression: str, as_of: date) -> tuple[date, date]:
    kind, _, arg = expression.strip().partition(":")
    if kind in ("last_n_days", "last_n_weeks"):
        if not re.fullmatch(r"[1-9]\d{0,3}", arg):
            raise DateResolutionError(f"{kind} needs a positive integer, got {arg!r}")
        days = int(arg) * (7 if kind == "last_n_weeks" else 1)
        return as_of - timedelta(days=days - 1), as_of
    if kind == "this_month" and not arg:
        return _month(as_of.year, as_of.month)
    if kind == "last_month" and not arg:
        prev = as_of.replace(day=1) - timedelta(days=1)
        return _month(prev.year, prev.month)
    if kind == "month":
        m = re.fullmatch(r"(\d{4})-(\d{2})", arg)
        if not m or not 1 <= int(m.group(2)) <= 12:
            raise DateResolutionError(f"month needs YYYY-MM, got {arg!r}")
        return _month(int(m.group(1)), int(m.group(2)))
    if kind == "day":
        d = _parse_date(arg, expression)
        return d, d
    if kind == "explicit":
        start, sep, end = arg.partition("..")
        first = _parse_date(start, expression)
        return (first, _parse_date(end, expression)) if sep else (first, first)
    if kind == "relative_to_named":
        name, _, modifier = arg.partition(":")
        if name not in NAMED_PERIODS:
            raise DateResolutionError(
                f"unknown named period {name!r}; known: {list(NAMED_PERIODS)}"
            )
        start, end = NAMED_PERIODS[name]
        if modifier == "before":
            return _preceding(TimeWindow(start=start, end=end))
        if modifier:
            raise DateResolutionError(f"unknown modifier {modifier!r} (only 'before')")
        return start, end
    raise DateResolutionError(f"unknown time expression {expression!r}")


def _clip(expression: str, start: date, end: date, data_start: date, as_of: date) -> ResolvedWindow:
    if start > end:
        raise DateResolutionError(f"{expression!r} resolves to an empty window")
    if end < data_start or start > as_of:
        raise DateResolutionError(
            f"{expression!r} ({start}..{end}) is outside the available data ({data_start}..{as_of})"
        )
    notes = []
    if start < data_start:
        notes.append(f"Data starts on {data_start}; the window was clipped from {start}.")
        start = data_start
    if end > as_of:
        notes.append(f"Data is complete only up to {as_of}; the window was clipped from {end}.")
        end = as_of
    return ResolvedWindow(
        expression=expression, window=TimeWindow(start=start, end=end), notes=notes
    )


def resolve(expression: str, *, as_of: date, data_start: date) -> ResolvedWindow:
    """Resolve one time expression to an inclusive, clipped window."""
    start, end = _raw_range(expression, as_of)
    return _clip(expression, start, end, data_start, as_of)


def resolve_compare(
    current: ResolvedWindow, expression: str | None, *, as_of: date, data_start: date
) -> ResolvedWindow:
    """Resolve the baseline window for a comparison.

    With no expression (or ``previous_period``) the baseline is the immediately preceding window
    of equal length (SPEC §7.7).
    """
    if expression is None or expression.strip() == "previous_period":
        start, end = _preceding(current.window)
        return _clip("previous_period", start, end, data_start, as_of)
    return resolve(expression, as_of=as_of, data_start=data_start)
