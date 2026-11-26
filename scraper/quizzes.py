"""Quiz attempt status and quiz grade parsing helpers for Moodle 4.x/5.x."""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup

from scraper.client import _ARABIC_DIACRITICS_RE
from scraper.parsers import is_completion_region_complete, parse_submission_state

_DIGIT_TRANS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_DECIMAL_SEP_RE = re.compile(r"(?<=\d)[,٫](?=\d)")
_SLASH_DATE_RE = re.compile(
    r"\b(?:\d{4}\s*/\s*\d{1,2}\s*/\s*\d{1,2}|\d{1,2}\s*/\s*\d{1,2}\s*/\s*\d{2,4})\b"
)


def _normalize_grade_numbers(text: str) -> str:
    """Normalize Arabic/Persian digits and decimal separators (',' and '٫') to ASCII."""
    s = str(text).translate(_DIGIT_TRANS)
    return _DECIMAL_SEP_RE.sub(".", s)


def _extract_header_max(header_text: str) -> str:
    """Extract optional max score from a quiz table header like 'Grade / 20.00' or 'العلامة / ١٠'."""
    norm = _normalize_grade_numbers(re.sub(r"\s+", " ", header_text or "").strip())
    norm = _SLASH_DATE_RE.sub(" ", norm)
    m = re.search(r"(?:/|out\s+of|من\s+أصل|من)\s*(\d+(?:\.\d+)?)", norm, re.I)
    return m.group(1) if m else ""


def parse_quiz_grade_text(raw_text: str) -> dict[str, str]:
    """Parse Moodle quiz grade text such as '15.00 out of 15.00 ( 100 %)', '8,5 / 10', '٨٫٥ / ١٠', or '15.00'.

    Never matches date strings like dd/mm/yyyy, d/m/yyyy, yyyy/mm/dd, or dd/mm/yy as a grade.
    """
    out = {"grade_val": "", "grade_max": "", "percentage": ""}
    if not raw_text:
        return out
    s = re.sub(r"\s+", " ", str(raw_text)).strip()
    s = _normalize_grade_numbers(s)
    low = s.lower()
    if not s or s in ("-", "—", "empty", "فارغ") or any(
        neg in low
        for neg in (
            "not yet graded",
            "لم يتم التقييم",
            "لم يُقيَّم",
            "review not permitted",
            "غير مسموح",
            "no grade",
            "بدون علامة",
        )
    ):
        return out

    # Strip slash-formatted dates (dd/mm/yyyy, d/m/yyyy, yyyy/mm/dd, dd/mm/yy) before matching grades
    s_no_dates = re.sub(r"\s+", " ", _SLASH_DATE_RE.sub(" ", s)).strip()
    if not s_no_dates:
        return out

    # Percentage in parentheses or trailing: e.g. "( 100 %)", "93.33%", "71,25%"
    m_pct = re.search(r"(\d+(?:\.\d+)?)\s*%", s_no_dates)
    if m_pct:
        out["percentage"] = f"{m_pct.group(1)}%"

    # Pattern 1: "15.00 out of 15.00" / "15.00 من 15.00" / "15.00 من أصل 15.00"
    m_out = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:out\s+of|من\s+أصل|من)\s*(\d+(?:\.\d+)?)",
        s_no_dates,
        re.I,
    )
    if m_out:
        out["grade_val"] = m_out.group(1)
        out["grade_max"] = f"0–{m_out.group(2)}"
        return out

    # Pattern 2: "15.00 / 15.00" or "8.5 / 10" (never part of a 3-part date a/b/c)
    m_slash = re.search(
        r"(?<![/\d])(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)(?!\s*/)(?!\d)",
        s_no_dates,
    )
    if m_slash:
        out["grade_val"] = m_slash.group(1)
        out["grade_max"] = f"0–{m_slash.group(2)}"
        return out

    # Pattern 3: Pure number (e.g. "15.00", "12,5", "٨٫٧٥" from quiz/index.php or quizattemptsummary cell)
    m_num = re.fullmatch(r"\d+(?:\.\d+)?", s)
    if m_num:
        out["grade_val"] = s
        return out

    return out


def build_quiz_attempt_col_map(tbl: Any) -> tuple[Any | None, dict[str, int], dict[str, str]]:
    """Build a dynamic header column index map for horizontal Moodle `quizattemptsummary` tables.

    Returns `(header_tr, col_map, header_max)` where `col_map` maps canonical keys
    (`attempt`, `state`, `marks`, `grade`, `review`, `feedback`) to column indices,
    and `header_max` stores any max score extracted from column headers (e.g. 'Grade / 20.00').
    Returns `(None, {}, {})` for vertical key-value `quizreviewsummary` tables.
    """
    tbl_classes = " ".join(tbl.get("class") or []).lower()
    is_attempt_table = "quizattemptsummary" in tbl_classes

    candidate_rows: list[Any] = []
    thead = tbl.find("thead")
    if thead:
        candidate_rows.extend(thead.find_all("tr"))
    for tr in tbl.find_all("tr"):
        if tr not in candidate_rows:
            candidate_rows.append(tr)

    for tr in candidate_rows:
        cells = tr.find_all(["th", "td"])
        if len(cells) < 2:
            continue
        all_th = all(c.name == "th" for c in cells)
        if not all_th and not (is_attempt_table and tr is candidate_rows[0]):
            continue

        col_map: dict[str, int] = {}
        header_max: dict[str, str] = {}
        for idx, cell in enumerate(cells):
            raw_hdr = cell.get_text(" ", strip=True)
            htxt = _ARABIC_DIACRITICS_RE.sub("", raw_hdr).lower()
            if "attempt" not in col_map and any(k in htxt for k in ("attempt", "المحاولة", "محاولة")):
                col_map["attempt"] = idx
            elif "state" not in col_map and any(k in htxt for k in ("state", "status", "الحالة", "حالة")):
                col_map["state"] = idx
            elif "grade" not in col_map and any(
                k in htxt for k in ("grade", "الدرجة", "العلامة", "النتيجة", "التقدير")
            ):
                col_map["grade"] = idx
                hmax = _extract_header_max(raw_hdr)
                if hmax:
                    header_max["grade"] = hmax
            elif "marks" not in col_map and any(k in htxt for k in ("marks", "mark", "نقاط", "النقاط")):
                col_map["marks"] = idx
                hmax = _extract_header_max(raw_hdr)
                if hmax:
                    header_max["marks"] = hmax
            elif "review" not in col_map and any(k in htxt for k in ("review", "المراجعة", "مراجعة")):
                col_map["review"] = idx
            elif "feedback" not in col_map and any(k in htxt for k in ("feedback", "ملاحظات", "التغذية", "إفادة")):
                col_map["feedback"] = idx

        if len(col_map) >= 2 or (is_attempt_table and all_th and len(col_map) >= 1):
            return tr, col_map, header_max

    return None, {}, {}


def _try_float(val: str) -> float | None:
    try:
        return float(_normalize_grade_numbers(val))
    except (TypeError, ValueError):
        return None


def parse_quiz_status_soup(soup: BeautifulSoup) -> dict[str, Any]:
    """Parse completion/attempt status, released grade, and close date from a Moodle quiz page soup."""
    result: dict[str, Any] = {
        "is_submitted": False,
        "status_text": "",
        "due_date": "",
        "grade_val": "",
        "grade_max": "",
        "percentage": "",
        "feedback": "",
    }

    # 1. Check all tables on the quiz page (handles horizontal quizattemptsummary & vertical quizreviewsummary)
    # Note: Moodle shows BOTH 'Marks' (raw points, e.g. 5/5) AND 'Grade' (scaled grade, e.g. 10/10).
    # Always prefer 'Grade' over 'Marks'.
    has_final_grade_row = False
    best_grade_num: float | None = None

    def _apply_parsed_grade(pg: dict[str, str], *, is_final: bool) -> None:
        nonlocal has_final_grade_row, best_grade_num
        if not pg.get("grade_val"):
            return
        if not is_final and has_final_grade_row:
            return
        cand_num = _try_float(pg["grade_val"])
        should_update = (
            not result["grade_val"]
            or (is_final and not has_final_grade_row)
            or (
                cand_num is not None
                and (best_grade_num is None or cand_num >= best_grade_num)
            )
        )
        if should_update:
            result["grade_val"] = pg["grade_val"]
            result["grade_max"] = pg.get("grade_max", "")
            result["percentage"] = pg.get("percentage", "")
            best_grade_num = cand_num
        result["is_submitted"] = True
        result["status_text"] = result["status_text"] or "تم التقييم"
        if is_final:
            has_final_grade_row = True

    tables = soup.find_all("table")
    for tbl in tables:
        header_tr, col_map, header_max = build_quiz_attempt_col_map(tbl)
        if header_tr is not None and col_map:
            state_idx = col_map.get("state")
            grade_idx = col_map.get("grade")
            marks_idx = col_map.get("marks")
            fb_idx = col_map.get("feedback")

            for tr in tbl.find_all("tr"):
                if tr is header_tr:
                    continue
                raw_cells = tr.find_all(["td", "th"])
                if not raw_cells or all(c.name == "th" for c in raw_cells):
                    continue
                cells = [c.get_text(" ", strip=True) for c in raw_cells]

                if state_idx is not None and state_idx < len(cells):
                    if parse_submission_state(cells[state_idx]) is True:
                        result["is_submitted"] = True
                        result["status_text"] = result["status_text"] or "تم الحل (Finished)"

                if grade_idx is not None and grade_idx < len(cells):
                    pg = parse_quiz_grade_text(cells[grade_idx])
                    if pg.get("grade_val"):
                        if not pg.get("grade_max") and header_max.get("grade"):
                            pg["grade_max"] = f"0–{header_max['grade']}"
                        _apply_parsed_grade(pg, is_final=True)

                if marks_idx is not None and marks_idx < len(cells) and not has_final_grade_row:
                    pg = parse_quiz_grade_text(cells[marks_idx])
                    if pg.get("grade_val"):
                        if not pg.get("grade_max") and header_max.get("marks"):
                            pg["grade_max"] = f"0–{header_max['marks']}"
                        _apply_parsed_grade(pg, is_final=False)

                if fb_idx is not None and fb_idx < len(cells):
                    fb_txt = cells[fb_idx].strip()
                    if fb_txt and fb_txt not in ("-", "—") and not result["feedback"]:
                        result["feedback"] = fb_txt[:300]
            continue

        # Vertical key-value summary table (e.g. quizreviewsummary)
        for tr in tbl.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            if not cells:
                continue
            if len(cells) >= 2:
                hdr = cells[0].lower()
                val_raw = cells[-1]
                val = val_raw.lower()
                if any(k in hdr for k in ["status", "state", "الحالة", "حالة"]):
                    if parse_submission_state(val_raw) is True:
                        result["is_submitted"] = True
                        result["status_text"] = "تم الحل (Finished)"
                elif (
                    any(k in hdr for k in ["completed", "اكتمل", "تاريخ الإكتمال"])
                    and len(val) > 4
                    and val not in ("-", "—")
                ):
                    if parse_submission_state(val_raw) is not False:
                        result["is_submitted"] = True
                        result["status_text"] = result["status_text"] or "تم الحل (Finished)"
                elif any(k in hdr for k in ["grade", "الدرجة", "العلامة", "النتيجة", "التقدير"]):
                    pg = parse_quiz_grade_text(val_raw)
                    if pg.get("grade_val"):
                        _apply_parsed_grade(pg, is_final=True)
                elif (
                    any(k in hdr for k in ["mark", "marks", "نقاط", "النقاط"])
                    and not has_final_grade_row
                    and not result["grade_val"]
                ):
                    pg = parse_quiz_grade_text(val_raw)
                    if pg.get("grade_val"):
                        _apply_parsed_grade(pg, is_final=False)
            row_txt = " ".join(cells)
            row_txt_low = row_txt.lower()
            if any(k in row_txt_low for k in ["status", "الحالة"]) and parse_submission_state(row_txt) is True:
                result["is_submitted"] = True
                result["status_text"] = result["status_text"] or "تم الحل (Finished)"

    # 2. Check final grade heading or feedback (e.g. "Your final grade for this quiz is 15.00/15.00.")
    grade_blocks = []
    fb_el = soup.find(id="feedback")
    if fb_el:
        grade_blocks.append(fb_el)
    for el in soup.find_all(["div", "h2", "h3", "p"], class_=re.compile(r"quizgradefeedback|quizattemptcounts|grade")):
        grade_blocks.append(el)
    for el in soup.find_all(["h2", "h3", "div"]):
        txt_l = el.get_text(" ", strip=True).lower()
        if len(txt_l) < 160 and any(
            k in txt_l
            for k in [
                "your final grade for this quiz is",
                "final grade",
                "highest grade",
                "علامتك النهائية",
                "الدرجة النهائية",
                "أعلى درجة",
            ]
        ):
            grade_blocks.append(el)

    for gb in grade_blocks:
        gtxt = gb.get_text(" ", strip=True)
        gtxt_l = gtxt.lower()
        if any(k in gtxt_l for k in ["final grade", "علامتك النهائية", "الدرجة النهائية", "أعلى درجة", "highest grade"]):
            result["is_submitted"] = True
            result["status_text"] = result["status_text"] or "تم التقييم"
            if not result["grade_val"]:
                pg = parse_quiz_grade_text(gtxt)
                if pg.get("grade_val"):
                    result["grade_val"] = pg["grade_val"]
                    result["grade_max"] = pg.get("grade_max", "")
                    result["percentage"] = pg.get("percentage", "")
        if gb.get("id") == "feedback" or "quizgradefeedback" in " ".join(gb.get("class") or []):
            if not result["feedback"] and len(gtxt) > 2:
                result["feedback"] = gtxt[:300]

    # 3. Check Moodle 4.x/5.x completion badge on the quiz page safely (ignores View-only / partial)
    if not result["is_submitted"]:
        comp = soup.find(attrs={"data-region": "completion-info"}) or soup.find(
            class_=re.compile(r"activity-completion")
        )
        if comp and is_completion_region_complete(comp):
            result["is_submitted"] = True
            result["status_text"] = "مكتمل"

    # 4. Extract quiz close / due date from activity-dates or quizinfo
    act_dates = soup.find(attrs={"data-region": "activity-dates"}) or soup.find(
        class_=re.compile(r"activity-dates|quizinfo")
    )
    if act_dates:
        dates_txt = act_dates.get_text(" ", strip=True)
        m_close = re.search(
            r"(?:Closes|Closed|Due|تاريخ الإغلاق|يغلق في|أغلق في|يغلق|أغلق|انتهاء)\s*:\s*(.+?)"
            r"(?=\s*(?:Opened|Opens|Closes|Closed|Due|Attempts|Time limit|$))",
            dates_txt,
            re.I,
        )
        if m_close:
            result["due_date"] = m_close.group(1).strip()
        else:
            for el in act_dates.find_all(["div", "p", "span"]):
                txt = el.get_text(" ", strip=True)
                if any(k in txt.lower() for k in ["close", "closes", "closed", "يغلق", "إغلاق", "انتهاء", "due"]):
                    cleaned = re.sub(
                        r"^(?:this quiz (?:will close|closed|closes) (?:at|on)|closes|closed|due|"
                        r"هذا الاختبار (?:يغلق|أغلق) في|تاريخ الإغلاق|يغلق)\s*:?\s*",
                        "",
                        txt,
                        flags=re.I,
                    ).strip()
                    if cleaned and len(cleaned) < 90:
                        result["due_date"] = cleaned
                        break

    return result
