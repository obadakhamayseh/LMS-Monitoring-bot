"""Date normalization and extraction helpers for Moodle 4.x/5.x pages."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)
JORDAN_TZ = ZoneInfo("Asia/Amman")

_MONTH_NAMES_RE = (
    r"January|February|March|April|May|June|July|August|September|October|November|December|"
    r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec|"
    r"يناير|فبراير|مارس|أبريل|إبريل|ابريل|مايو|يونيو|يوليو|أغسطس|اغسطس|سبتمبر|أكتوبر|اكتوبر|نوفمبر|ديسمبر|"
    r"كانون الثاني|كانون ثاني|شباط|آذار|أذار|اذار|نيسان|أيار|ايار|حزيران|تموز|آب|أب|اب|"
    r"أيلول|إيلول|ايلول|تشرين الأول|تشرين الاول|تشرين الثاني|كانون الأول|كانون الاول"
)

_MONTH_TOKENS_LOOKS_LIKE = (
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
    "يناير", "فبراير", "مارس", "أبريل", "إبريل", "ابريل", "مايو", "يونيو", "يوليو", "أغسطس", "اغسطس",
    "سبتمبر", "أكتوبر", "اكتوبر", "نوفمبر", "ديسمبر",
    "كانون", "شباط", "آذار", "اذار", "نيسان", "أيار", "ايار", "حزيران", "تموز", "آب", "أيلول", "تشرين",
)


def _now_amman() -> datetime:
    """Return current time in Asia/Amman (isolated for deterministic testing)."""
    return datetime.now(JORDAN_TZ)


def normalize_due_date_year(due_str: str) -> str:
    """Ensure Moodle 5.x yearless overview dates include a 4-digit year."""
    if not due_str:
        return ""
    s = re.sub(r"\s+", " ", str(due_str)).strip()
    if not s or s in ("-", "—"):
        return ""
    # Pure unix timestamp (e.g. from Moodle 5 data-mdl-overview-value)
    if re.fullmatch(r"\d{9,11}", s):
        try:
            ts = int(s)
            if ts > 1_000_000_000:
                dt = datetime.fromtimestamp(ts, tz=JORDAN_TZ)
                hour12 = dt.strftime("%I").lstrip("0") or "12"
                return f"{dt.strftime('%A')}, {dt.day} {dt.strftime('%B %Y')}, {hour12}:{dt.strftime('%M %p')}"
        except Exception:
            pass
    # Already has a 4-digit year
    if re.search(r"\b20\d{2}\b", s):
        return s
    # Insert inferred year after month name (e.g. 'Saturday, 28 November, 11:59 PM' -> '... 2026, ...')
    m = re.search(rf"(\b\d{{1,2}}\s+(?:{_MONTH_NAMES_RE})\b)", s, re.I)
    if m:
        now = _now_amman()
        year = now.year
        month_low = m.group(1).lower()
        if now.month >= 10 and any(k in month_low for k in ("jan", "feb", "يناير", "فبراير", "كانون الثاني", "شباط")):
            year += 1
        elif now.month <= 2 and any(
            k in month_low for k in ("nov", "dec", "نوفمبر", "ديسمبر", "تشرين الثاني", "كانون الأول", "كانون الاول")
        ):
            year -= 1
        s = s[: m.end(1)] + f" {year}" + s[m.end(1) :]
    return s


def looks_like_date(text: str) -> bool:
    """Return True if a table cell text plausibly represents a date."""
    if not text or len(text) < 6 or len(text) > 90:
        return False
    t = text.lower()
    if any(m in t for m in _MONTH_TOKENS_LOOKS_LIKE) and any(ch.isdigit() for ch in t):
        return True
    if re.search(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b", t):
        return True
    if re.search(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", t):
        return True
    return False


def extract_course_page_activity_dates(soup: BeautifulSoup | None) -> dict[str, str]:
    """Extract full-year Due/Closes dates per cmid directly from Moodle 4.x/5.x course page."""
    dates: dict[str, str] = {}
    if soup is None:
        return dates
    try:
        for act in soup.find_all(["li", "div"], class_=re.compile(r"\bactivity\b|\bactivity-item\b")):
            cmid = act.get("data-id") or ""
            if not cmid:
                m = re.search(r"module-(\d+)", act.get("id") or "")
                if m:
                    cmid = m.group(1)
            if not cmid:
                continue
            act_dates = act.find(attrs={"data-region": "activity-dates"}) or act.find(
                class_=re.compile(r"activity-dates")
            )
            if not act_dates:
                continue
            dates_txt = act_dates.get_text(" ", strip=True)
            m_due = re.search(
                r"(?:Due|Closes|Closed|تاريخ الاستحقاق|موعد التسليم|تاريخ الإغلاق|يغلق|أغلق)\s*:\s*(.+?)"
                r"(?=\s*(?:Opened|Opens|Due|Closes|Closed|تاريخ الفتح|يفتح|Done|To do|$))",
                dates_txt,
                re.I,
            )
            if m_due:
                val = m_due.group(1).strip(" |·-")
                if val and len(val) < 90:
                    dates[str(cmid)] = val
    except Exception as exc:
        logger.debug("Course activity-dates parse error: %s", exc)
    return dates
