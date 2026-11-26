"""Holiday calendars used by the deadline engine.

federal  - the legal public holidays in 5 U.S.C. 6103(a), with the Friday/Monday
           observance shift for fixed-date holidays that fall on a weekend
           (FRCP 6(a)(6)(A)-(B); FRAP 26(a)(6)).
illinois - federal holidays plus the extra Illinois holidays listed in
           data/holidays_illinois.yaml. VERIFY that list against 205 ILCS 630/17 and
           the Circuit Court of Cook County's "Legal Court Holidays" page each year.

Ad hoc closings (a day the President declares a holiday, a weather closure, a
court's own closure order) go in data/closures.yaml.
"""

from __future__ import annotations

import datetime as dt
from functools import lru_cache
from pathlib import Path

import yaml

DATA_DIR = Path(__file__).resolve().parent / "data"


def nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    """n-th weekday (Mon=0) of a month; n=-1 means the last one."""
    if n > 0:
        first = dt.date(year, month, 1)
        offset = (weekday - first.weekday()) % 7
        return first + dt.timedelta(days=offset + 7 * (n - 1))
    nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - dt.timedelta(days=1)
    return last - dt.timedelta(days=(last.weekday() - weekday) % 7)


def observed(day: dt.date) -> dt.date:
    """Saturday holidays are observed Friday; Sunday holidays are observed Monday."""
    if day.weekday() == 5:
        return day - dt.timedelta(days=1)
    if day.weekday() == 6:
        return day + dt.timedelta(days=1)
    return day


def federal_holidays(year: int) -> dict[dt.date, str]:
    days = {
        observed(dt.date(year, 1, 1)): "New Year's Day",
        nth_weekday(year, 1, 0, 3): "Birthday of Martin Luther King, Jr.",
        nth_weekday(year, 2, 0, 3): "Washington's Birthday",
        nth_weekday(year, 5, 0, -1): "Memorial Day",
        observed(dt.date(year, 6, 19)): "Juneteenth National Independence Day",
        observed(dt.date(year, 7, 4)): "Independence Day",
        nth_weekday(year, 9, 0, 1): "Labor Day",
        nth_weekday(year, 10, 0, 2): "Columbus Day",
        observed(dt.date(year, 11, 11)): "Veterans Day",
        nth_weekday(year, 11, 3, 4): "Thanksgiving Day",
        observed(dt.date(year, 12, 25)): "Christmas Day",
    }
    # New Year's Day of the following year can be observed on Dec 31 of this year.
    nxt = observed(dt.date(year + 1, 1, 1))
    if nxt.year == year:
        days[nxt] = "New Year's Day (observed)"
    return days


@lru_cache
def _illinois_spec() -> dict:
    return yaml.safe_load((DATA_DIR / "holidays_illinois.yaml").read_text(encoding="utf-8"))


def _resolve(spec: dict, year: int) -> dt.date | None:
    kind = spec["rule"]
    if kind == "fixed":
        day = dt.date(year, spec["month"], spec["day"])
        return observed(day) if spec.get("observe_weekend", False) else day
    if kind == "nth_weekday":
        return nth_weekday(year, spec["month"], spec["weekday"], spec["n"])
    if kind == "general_election":
        if year % 2:
            return None
        # first Tuesday after the first Monday in November
        return nth_weekday(year, 11, 0, 1) + dt.timedelta(days=1)
    raise ValueError(f"unknown holiday rule {kind}")


def illinois_extra_holidays(year: int) -> dict[dt.date, str]:
    out = {}
    for spec in _illinois_spec()["holidays"]:
        day = _resolve(spec, year)
        if day:
            out[day] = spec["name"]
    return out


@lru_cache
def closures() -> dict[dt.date, str]:
    path = DATA_DIR / "closures.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {dt.date.fromisoformat(str(c["date"])): c["reason"] for c in data.get("closures") or []}


def holiday_name(day: dt.date, calendars: list[str]) -> str | None:
    """Return the holiday's name if `day` is a holiday on any of the named calendars."""
    for cal in calendars:
        if cal == "federal":
            name = federal_holidays(day.year).get(day)
        elif cal == "illinois":
            name = illinois_extra_holidays(day.year).get(day)
        elif cal == "closures":
            name = closures().get(day)
        else:
            raise ValueError(f"unknown holiday calendar {cal}")
        if name:
            return name
    return None
