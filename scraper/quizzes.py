"""Quiz attempt status and quiz grade parsing helpers for Moodle 4.x/5.x."""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup

from scraper.parsers import is_completion_region_complete, parse_submission_state


def parse_quiz_grade_text(raw_text: str) -> dict[str, str]:
    """Parse Moodle quiz grade text such as '15.00 out of 15.00 ( 100 %)', '14.5 / 15', or '15.00'."""
    out = {"grade_val": "", "grade_max": "", "percentage": ""}
    if not raw_text:
        return out
    s = re.sub(r"\s+", " ", str(raw_text)).strip()
    s = s.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
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

    # Percentage in parentheses or trailing: e.g. "( 100 %)" or "93.33%"
    m_pct = re.search(r"(\d+(?:\.\d+)?)\s*%", s)
    if m_pct:
        out["percentage"] = f"{m_pct.group(1)}%"

    # Pattern 1: "15.00 out of 15.00" / "15.00 من 15.00" / "15.00 من أصل 15.00"
    m_out = re.search(r"(\d+(?:\.\d+)?)\s*(?:out\s+of|من\s+أصل|من)\s*(\d+(?:\.\d+)?)", s, re.I)
    if m_out:
        out["grade_val"] = m_out.group(1)
        out["grade_max"] = f"0–{m_out.group(2)}"
        return out

    # Pattern 2: "15.00 / 15.00" or "14/15"
    m_slash = re.search(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", s)
    if m_slash:
        out["grade_val"] = m_slash.group(1)
        out["grade_max"] = f"0–{m_slash.group(2)}"
        return out

    # Pattern 3: Pure number (e.g. "15.00" from quiz/index.php Grade column)
    m_num = re.fullmatch(r"\d+(?:\.\d+)?", s)
    if m_num:
        out["grade_val"] = s
        return out

    return out


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

    # 1. Check all tables on the quiz page (handles quizreviewsummary cards & quizattemptsummary tables)
    # Note: Moodle shows BOTH 'Marks' (raw points, e.g. 5/5) AND 'Grade' (scaled grade, e.g. 10/10).
    # Always prefer 'Grade' over 'Marks'.
    has_final_grade_row = False
    for tr in soup.find_all("tr"):
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
            elif any(k in hdr for k in ["grade", "الدرجة", "العلامة", "النتيجة"]):
                pg = parse_quiz_grade_text(val_raw)
                if pg.get("grade_val"):
                    result["grade_val"] = pg["grade_val"]
                    result["grade_max"] = pg.get("grade_max", "")
                    result["percentage"] = pg.get("percentage", "")
                    result["is_submitted"] = True
                    result["status_text"] = result["status_text"] or "تم التقييم"
                    has_final_grade_row = True
            elif (
                any(k in hdr for k in ["mark", "marks", "نقاط"])
                and not has_final_grade_row
                and not result["grade_val"]
            ):
                pg = parse_quiz_grade_text(val_raw)
                if pg.get("grade_val"):
                    result["grade_val"] = pg["grade_val"]
                    result["grade_max"] = pg.get("grade_max", "")
                    result["percentage"] = pg.get("percentage", "")
                    result["is_submitted"] = True
                    result["status_text"] = result["status_text"] or "تم التقييم"
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
