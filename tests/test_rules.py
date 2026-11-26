import datetime as dt

from lpc.rules.engine import compute, entries_for, load_rulesets, validate
from lpc.rules.holidays import federal_holidays, holiday_name, illinois_extra_holidays

D = dt.date


def test_rulesets_are_valid():
    assert validate() == []
    sets = load_rulesets()
    for rid in ("frcp", "frap", "ndil", "ca7", "illinois", "cook", "aaa_employment", "jams_employment"):
        assert rid in sets
    ids = {e.id for e in entries_for("ndil")}
    assert {"frcp.answer", "ndil.affiliates_notice", "usc.removal"} <= ids


def test_federal_holiday_observance():
    h2026 = federal_holidays(2026)
    assert D(2026, 7, 3) in h2026            # July 4, 2026 is a Saturday
    assert D(2026, 11, 26) in h2026          # Thanksgiving
    assert D(2027, 12, 31) in federal_holidays(2027)  # Jan 1, 2028 is a Saturday
    assert D(2026, 11, 3) in illinois_extra_holidays(2026)      # general election (even year)
    assert not any(n == "General Election Day" for n in illinois_extra_holidays(2027).values())


def test_answer_rolls_past_thanksgiving():
    assert compute("frcp.answer", D(2026, 11, 5), profile="ndil").due == D(2026, 11, 27)


def test_illinois_holiday_applies_only_with_illinois_profile():
    # 21 days after Jan 22, 2027 is Fri Feb 12 (Lincoln's Birthday); Feb 15 is Washington's Birthday.
    assert compute("frcp.answer", D(2027, 1, 22), profile="ndil").due == D(2027, 2, 16)
    assert compute("frcp.answer", D(2027, 1, 22)).due == D(2027, 2, 12)


def test_mail_service_adds_three_days_after_period():
    c = compute("frcp.interrogatory_responses", D(2026, 11, 2), "mail", profile="ndil")
    assert c.due == D(2026, 12, 7)           # Dec 2 + 3 = Sat Dec 5 -> Mon Dec 7
    assert compute("frcp.interrogatory_responses", D(2026, 11, 2), "electronic", profile="ndil").due == D(2026, 12, 2)


def test_backward_period_skips_to_earlier_day():
    # 7 days before Fri Jan 8, 2027 is New Year's Day -> Thu Dec 31, 2026
    assert compute("frcp.opposing_affidavit", D(2027, 1, 8), profile="ndil").due == D(2026, 12, 31)


def test_illinois_mail_service_completes_after_four_days():
    assert compute("il.interrogatory_answers", D(2026, 11, 2), "mail", profile="cook").due == D(2026, 12, 4)
    assert compute("il.appearance_answer", D(2026, 11, 2), profile="cook").due == D(2026, 12, 2)


def test_aaa_warns_instead_of_rolling():
    c = compute("aaa.answer", D(2026, 11, 14), profile="aaa_employment")
    assert c.due == D(2026, 11, 28)
    assert any("Saturday" in w for w in c.warnings)


def test_jams_mail_and_rollover():
    assert compute("jams.answer", D(2026, 11, 13), "mail", profile="jams_employment").due == D(2026, 11, 30)


def test_month_periods_clamp_and_roll():
    # 3 months after Nov 30, 2026 -> Feb 28, 2027 (Sunday) -> Mon Mar 1
    assert compute("faa.vacate_modify", D(2026, 11, 30), profile="aaa_employment").due == D(2027, 3, 1)


def test_unverified_entries_warn():
    c = compute("frcp.answer", D(2026, 11, 2), profile="ndil")
    assert any("not been verified" in w for w in c.warnings)
    assert c.explanation


def test_holiday_calendars_are_cached():
    federal_holidays.cache_clear()
    illinois_extra_holidays.cache_clear()
    for _ in range(50):
        holiday_name(D(2026, 11, 26), ["federal", "illinois"])
        holiday_name(D(2026, 11, 3), ["federal", "illinois"])
    assert federal_holidays.cache_info().misses == 1
    assert illinois_extra_holidays.cache_info().misses == 1
