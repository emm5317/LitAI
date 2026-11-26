"""Deadline engine.

Rules live in data/rulesets/*.yaml as data, not code. A ruleset names its counting
method, holiday calendars and service-day rules, may inherit entries from base
rulesets, and lists deadline entries (trigger, period, direction, citation).

Every computation returns its arithmetic step by step so the user can check it,
and nothing here writes to the database: the app stores results as *proposed*
deadlines that the user must confirm.

Counting methods
  frcp6a            FRCP 6(a) / FRAP 26(a): exclude the trigger day, count every day,
                    include the last day; if it is a weekend or legal holiday, keep going
                    (forward: to the next day; backward: to the day before).
  illinois          5 ILCS 70/1.11: exclude the first day, include the last unless it is a
                    Saturday, Sunday or holiday, in which case it is excluded.
  calendar_rollover calendar days; a weekend/holiday end date rolls to the next day
                    (JAMS Employment Rule 8(f)).
  calendar          calendar days with no rollover stated (AAA Employment/Workplace Rules);
                    the engine warns when the date lands on a weekend or holiday.
"""

from __future__ import annotations

import calendar as _cal
import datetime as dt
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from .holidays import holiday_name

RULESETS_DIR = Path(__file__).resolve().parent / "data" / "rulesets"
COUNTING_METHODS = {"frcp6a", "illinois", "calendar_rollover", "calendar"}
SERVICE_METHODS = ("electronic", "mail", "hand", "leave_with_clerk", "other_consented")


@dataclass
class Entry:
    id: str
    title: str
    cite: str
    trigger: str
    direction: str
    days: int | None = None
    months: int | None = None
    service_applies: bool = False
    hard: bool = True
    group: str | None = None
    notes: str | None = None
    verified: bool = False
    ruleset_id: str = ""


@dataclass
class Ruleset:
    id: str
    name: str
    jurisdiction: str
    counting: str
    holidays_all: list[str]
    holidays_forward_only: list[str]
    service_add_days: dict[str, int]
    service_completes_after: dict[str, int]
    sources: list[str]
    base: list[str]
    entries: list[Entry]
    notes: str | None = None
    verified: bool = False


@dataclass
class Computation:
    entry: Entry
    trigger_date: dt.date
    due: dt.date
    steps: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def explanation(self) -> str:
        return " ".join(self.steps)


def _parse(path: Path) -> Ruleset:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    hol = raw.get("holidays") or {}
    svc = raw.get("service") or {}
    rid = raw["id"]
    entries = []
    for e in raw.get("entries") or []:
        period = e.get("period") or {}
        entries.append(Entry(
            id=e["id"], title=e["title"], cite=e["cite"], trigger=e["trigger"],
            direction=e.get("direction", "after"), days=period.get("days"), months=period.get("months"),
            service_applies=bool(e.get("service_applies", False)), hard=bool(e.get("hard", True)),
            group=e.get("group"), notes=e.get("notes"), verified=bool(e.get("verified", False)),
            ruleset_id=rid,
        ))
    return Ruleset(
        id=rid, name=raw["name"], jurisdiction=raw.get("jurisdiction", ""), counting=raw["counting"],
        holidays_all=list(hol.get("all") or []), holidays_forward_only=list(hol.get("forward_only") or []),
        service_add_days=dict(svc.get("add_days") or {}),
        service_completes_after=dict(svc.get("completes_after") or {}),
        sources=list(raw.get("sources") or []), base=list(raw.get("base") or []),
        entries=entries, notes=raw.get("notes"), verified=bool(raw.get("verified", False)),
    )


@lru_cache
def load_rulesets() -> dict[str, Ruleset]:
    return {rs.id: rs for rs in (_parse(p) for p in sorted(RULESETS_DIR.glob("*.yaml")))}


def entries_for(ruleset_id: str) -> list[Entry]:
    """Entries of a ruleset plus everything it inherits, own entries first, no duplicates."""
    sets = load_rulesets()
    seen: set[str] = set()
    out: list[Entry] = []

    def walk(rid: str) -> None:
        rs = sets[rid]
        for e in rs.entries:
            if e.id not in seen:
                seen.add(e.id)
                out.append(e)
        for b in rs.base:
            walk(b)

    walk(ruleset_id)
    return out


def find_entry(entry_id: str) -> Entry:
    for rs in load_rulesets().values():
        for e in rs.entries:
            if e.id == entry_id:
                return e
    raise KeyError(entry_id)


def _add_months(day: dt.date, months: int, sign: int) -> dt.date:
    m = day.month - 1 + sign * months
    year, month = day.year + m // 12, m % 12 + 1
    return dt.date(year, month, min(day.day, _cal.monthrange(year, month)[1]))


def _closed(day: dt.date, calendars: list[str]) -> str | None:
    if day.weekday() == 5:
        return "Saturday"
    if day.weekday() == 6:
        return "Sunday"
    return holiday_name(day, calendars)


def _roll(day: dt.date, calendars: list[str], step: int, steps: list[str]) -> dt.date:
    skipped = []
    while reason := _closed(day, calendars):
        skipped.append(f"{day:%a %b %d} ({reason})" if reason not in ("Saturday", "Sunday") else f"{day:%a %b %d}")
        day += dt.timedelta(days=step)
    if skipped:
        word = "forward" if step > 0 else "back"
        steps.append(f"Skipping {', '.join(skipped)}, the period runs {word} to {day:%a %b %d, %Y}.")
    return day


def compute(entry_id: str, trigger_date: dt.date, service_method: str | None = None,
            profile: str | None = None) -> Computation:
    """Compute one deadline.

    `profile` is the matter's ruleset (e.g. 'ndil'). Counting and service rules come from
    the ruleset that defines the entry (FRCP entries use FRCP counting), but holiday
    calendars come from the profile when it declares any, because state holidays depend
    on where the court sits (FRCP 6(a)(6)(C)).
    """
    entry = find_entry(entry_id)
    sets = load_rulesets()
    rs = sets[entry.ruleset_id]
    hol_rs = sets[profile] if profile and (sets[profile].holidays_all or sets[profile].holidays_forward_only) else rs
    if rs.counting not in COUNTING_METHODS:
        raise ValueError(f"{rs.id}: unknown counting method {rs.counting}")
    forward = entry.direction == "after"
    sign = 1 if forward else -1
    calendars = hol_rs.holidays_all + (hol_rs.holidays_forward_only if forward else [])
    calendars = calendars + (["closures"] if "closures" not in calendars else [])
    steps: list[str] = []
    warnings: list[str] = []

    start = trigger_date
    if entry.service_applies and service_method:
        shift = rs.service_completes_after.get(service_method, 0)
        if shift:
            start = trigger_date + dt.timedelta(days=shift)
            steps.append(f"Service by {service_method.replace('_', ' ')} is complete {shift} days after it is made: "
                         f"{trigger_date:%b %d, %Y} becomes {start:%b %d, %Y}.")

    if entry.days is not None:
        due = start + dt.timedelta(days=sign * entry.days)
        span = f"{entry.days} days"
    else:
        due = _add_months(start, entry.months or 0, sign)
        span = f"{entry.months} months"
    steps.append(f"{span} {'after' if forward else 'before'} {start:%a %b %d, %Y} ({entry.trigger}) "
                 f"is {due:%a %b %d, %Y}.")

    if rs.counting == "calendar":
        if reason := _closed(due, calendars):
            warnings.append(f"{due:%b %d, %Y} is {reason}. These rules count calendar days and do not say "
                            "whether a weekend or holiday deadline rolls; confirm with the case manager.")
    else:
        due = _roll(due, calendars, sign, steps)

    if entry.service_applies and service_method:
        extra = rs.service_add_days.get(service_method, 0)
        if extra:
            before = due
            due = due + dt.timedelta(days=extra)
            steps.append(f"Service by {service_method.replace('_', ' ')} adds {extra} days: "
                         f"{before:%b %d, %Y} becomes {due:%b %d, %Y}.")
            if rs.counting != "calendar":
                due = _roll(due, calendars, 1, steps)

    if not entry.verified:
        warnings.append("This rule entry has not been verified against the source text yet.")
    return Computation(entry=entry, trigger_date=trigger_date, due=due, steps=steps, warnings=warnings)


def validate() -> list[str]:
    """Structural checks on every ruleset; returns a list of problems (empty = OK)."""
    problems = []
    sets = load_rulesets()
    ids: dict[str, str] = {}
    for rs in sets.values():
        if rs.counting not in COUNTING_METHODS:
            problems.append(f"{rs.id}: unknown counting method {rs.counting}")
        for b in rs.base:
            if b not in sets:
                problems.append(f"{rs.id}: base {b} not found")
        for m in list(rs.service_add_days) + list(rs.service_completes_after):
            if m not in SERVICE_METHODS:
                problems.append(f"{rs.id}: unknown service method {m}")
        for e in rs.entries:
            if e.id in ids:
                problems.append(f"duplicate entry id {e.id} in {rs.id} and {ids[e.id]}")
            ids[e.id] = rs.id
            if e.direction not in ("after", "before"):
                problems.append(f"{e.id}: direction must be after or before")
            if (e.days is None) == (e.months is None):
                problems.append(f"{e.id}: period needs exactly one of days or months")
            if not e.cite or not e.title or not e.trigger:
                problems.append(f"{e.id}: missing cite, title or trigger")
    return problems
