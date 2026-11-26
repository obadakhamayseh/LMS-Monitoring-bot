"""Dynamic Moodle Attendance table and status parsing helpers."""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup

from scraper.client import _ARABIC_DIACRITICS_RE, clean_url
from scraper.parsers import resolve_lms_url


def parse_attendance_status_category(status_text: str) -> str | None:
    """Classify attendance status (full words, Arabic equivalents, and single-letter P/A/L/E)."""
    if not status_text:
        return None
    cleaned = _ARABIC_DIACRITICS_RE.sub("", str(status_text))
    cleaned = re.sub(r"\s+", " ", cleaned).strip().lower()
    if not cleaned or cleaned in ("?", "-", "—") or cleaned.startswith("?"):
        return None
    first_tok = re.split(r"[\s(\[/\-–—]+", cleaned)[0]
    if first_tok == "p" or any(k in cleaned for k in ("present", "حاضر", "موجود")):
        return "present"
    if first_tok == "a" or any(k in cleaned for k in ("absent", "غائب", "غياب")):
        return "absent"
    if first_tok == "l" or any(k in cleaned for k in ("late", "متأخر", "تأخير")):
        return "late"
    if first_tok == "e" or any(k in cleaned for k in ("excused", "معذور", "عذر")):
        return "excused"
    return None


def build_attendance_col_map(tbl: Any) -> tuple[Any | None, dict[str, int]]:
    """Build a dynamic header column index map for a Moodle Attendance session table."""
    rows = tbl.find_all("tr")
    for tr in rows:
        cells = tr.find_all(["th", "td"])
        if len(cells) < 2:
            continue
        col_map: dict[str, int] = {}
        for idx, cell in enumerate(cells):
            htxt = _ARABIC_DIACRITICS_RE.sub("", cell.get_text(" ", strip=True)).lower()
            if "date" not in col_map and any(k in htxt for k in ("date", "التاريخ", "تاريخ")):
                col_map["date"] = idx
            elif "group" not in col_map and any(k in htxt for k in ("group", "المجموعة", "مجموعة", "الشعبة", "شعبة")):
                col_map["group"] = idx
            elif "description" not in col_map and any(
                k in htxt for k in ("description", "الوصف", "وصف", "المحاضرة", "session")
            ):
                col_map["description"] = idx
            elif "status" not in col_map and any(k in htxt for k in ("status", "الحالة", "الوضع")):
                col_map["status"] = idx
            elif "points" not in col_map and any(k in htxt for k in ("points", "النقاط", "نقاط", "grade", "الدرجة")):
                col_map["points"] = idx
            elif "remarks" not in col_map and any(k in htxt for k in ("remarks", "ملاحظات", "تعليق", "comment")):
                col_map["remarks"] = idx
        if "date" in col_map and "status" in col_map:
            return tr, col_map
    return None, {}


def extract_attendance_links(soup: BeautifulSoup, clean_link_text_fn: Any) -> list[tuple[str, str, str]]:
    """Discover unique `/mod/attendance/view.php?id=<cmid>` links from a course page soup."""
    att_links: list[tuple[str, str, str]] = []
    seen_cmids: set[str] = set()
    for a in soup.find_all("a", href=re.compile(r"(?:^|/)mod/attendance/view\.php\?id=\d+", re.I)):
        clean_href = clean_url(resolve_lms_url(a.get("href", "")))
        m = re.search(r"id=(\d+)", clean_href)
        if not m:
            continue
        cmid = m.group(1)
        if cmid in seen_cmids:
            continue
        seen_cmids.add(cmid)
        title = clean_link_text_fn(a) or "Attendance"
        att_links.append((cmid, title, clean_href))
    return att_links


def parse_attendance_page_soup(
    att_soup: BeautifulSoup,
    course_id: int,
    cmid: str,
    title: str,
    base_url: str,
) -> dict[str, Any]:
    """Parse a Moodle Attendance view page into session records and summary statistics."""
    tables = att_soup.find_all("table")

    sessions: list[dict[str, str]] = []
    present_count = 0
    absent_count = 0
    late_count = 0
    excused_count = 0

    taken_sessions = 0
    total_sessions = 0
    points_str = ""
    percentage_str = ""

    for tbl in tables:
        header_tr, col_map = build_attendance_col_map(tbl)
        if header_tr is not None and col_map:
            date_idx = col_map["date"]
            status_idx = col_map["status"]
            desc_idx = col_map.get("description")
            points_idx = col_map.get("points")
            remarks_idx = col_map.get("remarks")
            group_idx = col_map.get("group")
            min_required = max(date_idx, status_idx) + 1

            for tr in tbl.find_all("tr"):
                if tr is header_tr:
                    continue
                raw_cells = tr.find_all(["td", "th"])
                if not raw_cells or all(c.name == "th" for c in raw_cells):
                    continue
                cells = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)).strip() for c in raw_cells]
                if len(cells) < min_required:
                    continue

                s_date = cells[date_idx]
                s_status = cells[status_idx]
                s_desc = cells[desc_idx] if (desc_idx is not None and desc_idx < len(cells)) else ""
                s_points = cells[points_idx] if (points_idx is not None and points_idx < len(cells)) else ""
                s_remarks = cells[remarks_idx] if (remarks_idx is not None and remarks_idx < len(cells)) else ""
                s_group = cells[group_idx] if (group_idx is not None and group_idx < len(cells)) else ""

                # Skip unrecorded future sessions (where status is '?' or empty)
                if not s_status or s_status in ("?", "-", "—") or s_status.startswith("?"):
                    continue
                # Skip empty-table placeholder message rows
                if "no " in s_date.lower() and len(cells) < 4:
                    continue

                cat = parse_attendance_status_category(s_status)
                if cat == "present":
                    present_count += 1
                elif cat == "absent":
                    absent_count += 1
                elif cat == "late":
                    late_count += 1
                elif cat == "excused":
                    excused_count += 1

                sess_rec: dict[str, str] = {
                    "date": s_date,
                    "description": s_desc,
                    "status": s_status,
                    "points": s_points,
                    "remarks": s_remarks,
                }
                if s_group:
                    sess_rec["group"] = s_group
                sessions.append(sess_rec)
        else:
            # Summary key-value table
            for tr in tbl.find_all("tr"):
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
                if len(cells) < 2:
                    continue
                k = cells[0].lower()
                v = cells[1].strip()
                if "percentage over taken" in k or "النسبة المئوية" in k or "نسبة الحضور" in k:
                    percentage_str = v
                elif "points over taken" in k or "النقاط على" in k:
                    points_str = v
                elif "taken sessions" in k or "الجلسات المأخوذة" in k or "المحاضرات المنعقدة" in k:
                    m_num = re.search(r"(\d+)", v)
                    if m_num:
                        taken_sessions = int(m_num.group(1))
                elif "total number of sessions" in k or "مجموع الجلسات" in k or "إجمالي الجلسات" in k:
                    m_num = re.search(r"(\d+)", v)
                    if m_num:
                        total_sessions = int(m_num.group(1))

    if taken_sessions == 0 and sessions:
        taken_sessions = len(sessions)
    if total_sessions < taken_sessions:
        total_sessions = taken_sessions

    return {
        "att_id": f"att_{cmid}",
        "title": title,
        "taken_sessions": taken_sessions,
        "total_sessions": total_sessions,
        "present_count": present_count,
        "absent_count": absent_count,
        "late_count": late_count,
        "excused_count": excused_count,
        "points_str": points_str,
        "percentage_str": percentage_str,
        "sessions": sessions,
        "url": base_url,
        "course_id": course_id,
    }
