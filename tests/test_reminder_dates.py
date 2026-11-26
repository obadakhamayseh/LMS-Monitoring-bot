import os
from datetime import datetime
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet
import pytest

# Must be set BEFORE importing project modules (never touch live DB or real .env)
os.environ["DATABASE_URL"] = ""
os.environ["DIRECT_URL"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:TEST"
os.environ["OWNER_TELEGRAM_ID"] = "1"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()

import reminder  # noqa: E402
from reminder import _parse_due_date  # noqa: E402

AMMAN_TZ = ZoneInfo("Asia/Amman")


def _amman_ts(year: int, month: int, day: int, hour: int, minute: int) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=AMMAN_TZ).timestamp())


@pytest.mark.parametrize(
    "due_str,expected_ymdhm",
    [
        # All 12 Arabic Levantine months (including compound multi-word months)
        ("15 كانون الثاني 2026، 11:59 مساءً", (2026, 1, 15, 23, 59)),
        ("14 شباط 2026، 10:30 ص", (2026, 2, 14, 10, 30)),
        ("21 آذار 2026، 08:15 صباحاً", (2026, 3, 21, 8, 15)),
        ("21 اذار 2026، 08:15 صباحا", (2026, 3, 21, 8, 15)),
        ("10 نيسان 2026، 09:00 م", (2026, 4, 10, 21, 0)),
        ("5 أيار 2026، 14:20", (2026, 5, 5, 14, 20)),
        ("5 ايار 2026، 14:20", (2026, 5, 5, 14, 20)),
        ("18 حزيران 2026، 11:00 صباحاً", (2026, 6, 18, 11, 0)),
        ("7 تموز 2026، 16:45", (2026, 7, 7, 16, 45)),
        ("12 آب 2026، 12:30 م", (2026, 8, 12, 12, 30)),
        ("12 اب 2026، 12:30 مساء", (2026, 8, 12, 12, 30)),
        ("3 أيلول 2026، 12:00 ص", (2026, 9, 3, 0, 0)),
        ("3 ايلول 2026، 12:00 صباحا", (2026, 9, 3, 0, 0)),
        ("20 تشرين الأول 2026، 11:59 م", (2026, 10, 20, 23, 59)),
        ("20 تشرين الاول 2026، 11:59 مساءً", (2026, 10, 20, 23, 59)),
        ("25 تشرين الثاني 2026، 07:30 م", (2026, 11, 25, 19, 30)),
        ("31 كانون الأول 2026، 11:59 م", (2026, 12, 31, 23, 59)),
        ("31 كانون الاول 2026، 11:59 مساء", (2026, 12, 31, 23, 59)),
        # All 12 Arabic Gregorian months (including إبريل / أبريل / ابريل)
        ("10 يناير 2026، 09:30 ص", (2026, 1, 10, 9, 30)),
        ("11 فبراير 2026، 10:00 م", (2026, 2, 11, 22, 0)),
        ("12 مارس 2026، 13:45", (2026, 3, 12, 13, 45)),
        ("13 أبريل 2026، 06:00 م", (2026, 4, 13, 18, 0)),
        ("13 إبريل 2026، 06:00 م", (2026, 4, 13, 18, 0)),
        ("13 ابريل 2026، 06:00 م", (2026, 4, 13, 18, 0)),
        ("14 مايو 2026، 08:00 ص", (2026, 5, 14, 8, 0)),
        ("15 يونيو 2026، 11:15 م", (2026, 6, 15, 23, 15)),
        ("16 يوليو 2026، 05:05 م", (2026, 7, 16, 17, 5)),
        ("17 أغسطس 2026، 04:00 م", (2026, 8, 17, 16, 0)),
        ("17 اغسطس 2026، 04:00 م", (2026, 8, 17, 16, 0)),
        ("18 سبتمبر 2026، 09:45 ص", (2026, 9, 18, 9, 45)),
        ("19 أكتوبر 2026، 11:59 م", (2026, 10, 19, 23, 59)),
        ("19 اكتوبر 2026، 11:59 م", (2026, 10, 19, 23, 59)),
        ("20 نوفمبر 2026، 12:00 ص", (2026, 11, 20, 0, 0)),
        ("21 ديسمبر 2026، 23:55", (2026, 12, 21, 23, 55)),
        # Arabic-Indic digits with compound month and day-of-week prefix
        ("الخميس، ١٥ كانون الثاني ٢٠٢٦، ١١:٥٩ مساءً", (2026, 1, 15, 23, 59)),
    ],
)
def test_parse_due_date_arabic_months(due_str, expected_ymdhm):
    assert _parse_due_date(due_str) == _amman_ts(*expected_ymdhm)


@pytest.mark.parametrize(
    "due_str,expected_ymdhm",
    [
        # Full English months
        ("15 January 2026, 11:59 p.m.", (2026, 1, 15, 23, 59)),
        ("14 February 2026, 12:00 a.m.", (2026, 2, 14, 0, 0)),
        ("10 March 2026, 12:30 p.m.", (2026, 3, 10, 12, 30)),
        ("5 April 2026, 1:05 A.M.", (2026, 4, 5, 1, 5)),
        ("6 May 2026, 1:05 P.M.", (2026, 5, 6, 13, 5)),
        ("7 June 2026, 11:59 PM", (2026, 6, 7, 23, 59)),
        ("8 July 2026, 12:00 AM", (2026, 7, 8, 0, 0)),
        ("9 August 2026, 12:30 PM", (2026, 8, 9, 12, 30)),
        ("11 September 2026, 9:00 am", (2026, 9, 11, 9, 0)),
        ("12 October 2026, 9:00 pm", (2026, 10, 12, 21, 0)),
        ("Sunday, 30 November 2025, 11:59 p.m.", (2025, 11, 30, 23, 59)),
        ("24 December 2026, 23:59", (2026, 12, 24, 23, 59)),
        # Short English months
        ("2 Jan 2026, 11:59 p.m.", (2026, 1, 2, 23, 59)),
        ("3 Feb 2026, 12:00 a.m.", (2026, 2, 3, 0, 0)),
        ("4 Mar 2026, 12:30 p.m.", (2026, 3, 4, 12, 30)),
        ("5 Apr 2026, 8:15 a.m.", (2026, 4, 5, 8, 15)),
        ("6 May 2026, 8:15 p.m.", (2026, 5, 6, 20, 15)),
        ("7 Jun 2026, 10:00 AM", (2026, 6, 7, 10, 0)),
        ("8 Jul 2026, 10:00 PM", (2026, 7, 8, 22, 0)),
        ("9 Aug 2026, 14:00", (2026, 8, 9, 14, 0)),
        ("10 Sep 2026, 11:59 p.m.", (2026, 9, 10, 23, 59)),
        ("11 Oct 2026, 12:15 a.m.", (2026, 10, 11, 0, 15)),
        ("12 Nov 2026, 12:45 p.m.", (2026, 11, 12, 12, 45)),
        ("13 Dec 2026, 6:30 p.m.", (2026, 12, 13, 18, 30)),
        # Date-only defaults to 23:59
        ("15 كانون الثاني 2026", (2026, 1, 15, 23, 59)),
        ("15/03/2026", (2026, 3, 15, 23, 59)),
        ("2026-03-15 14:30", (2026, 3, 15, 14, 30)),
    ],
)
def test_parse_due_date_english_and_ampm_edge_cases(due_str, expected_ymdhm):
    assert _parse_due_date(due_str) == _amman_ts(*expected_ymdhm)


def test_parse_due_date_uses_zoneinfo_amman_not_fixed_offset():
    # Before Oct 2022 Jordan observed winter DST (UTC+2 in January).
    # ZoneInfo("Asia/Amman") produces UTC+2, whereas fixed +3 is off by 3600s.
    due_str = "15 January 2021, 10:00 a.m."
    expected = _amman_ts(2021, 1, 15, 10, 0)
    assert _parse_due_date(due_str) == expected


@pytest.mark.parametrize(
    "fake_now,due_str,expected_ymdhm",
    [
        # Same year
        (datetime(2026, 5, 10, 12, 0, tzinfo=AMMAN_TZ), "Saturday, 28 November, 11:59 p.m.", (2026, 11, 28, 23, 59)),
        # Late-year -> Jan/Feb wraps to next year
        (datetime(2026, 11, 20, 12, 0, tzinfo=AMMAN_TZ), "15 كانون الثاني، 11:59 م", (2027, 1, 15, 23, 59)),
        # Early-year -> Nov/Dec wraps to previous year
        (datetime(2026, 1, 15, 12, 0, tzinfo=AMMAN_TZ), "20 كانون الأول، 10:00 ص", (2025, 12, 20, 10, 0)),
    ],
)
def test_parse_due_date_yearless_boundary_rule(monkeypatch, fake_now, due_str, expected_ymdhm):
    monkeypatch.setattr(reminder, "_now_jordan", lambda: fake_now)
    assert _parse_due_date(due_str) == _amman_ts(*expected_ymdhm)


@pytest.mark.parametrize("bad", ["", "   ", "not a date", "32 January 2026, 10:00 AM"])
def test_parse_due_date_invalid_returns_none(bad):
    assert _parse_due_date(bad) is None
