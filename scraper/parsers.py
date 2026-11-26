"""HTML parsing helpers for Moodle courses, activities, resources, grades, and announcements."""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from config import LMS_BASE_URL
from scraper.client import _ARABIC_DIACRITICS_RE, clean_url
from scraper.dates import looks_like_date, normalize_due_date_year

logger = logging.getLogger(__name__)

_NEGATIVE_SUBMISSION_RE = re.compile(
    r"not\s+submitted|no\s+submissions?\s+have\s+been\s+made(?:\s+yet)?|"
    r"no\s+submissions?|no\s+attempts?(?:\s+have\s+been\s+made)?|"
    r"not\s+(?:yet\s+)?graded|not\s+completed|not\s+finished|never\s+submitted|"
    r"\bdraft\b|in\s+progress|"
    r"لم\s+تسلم|لم\s+يتم\s+التسليم|لم\s+يتم\s+تقديم|غير\s+مسلم[ةه]?|"
    r"لا\s+توجد\s+محاول(?:ة|ات)|لا\s+يوجد\s+تسليم|لم\s+تقم\s+بأي\s+محاولة|"
    r"مسود[ةه]|لم\s+يتم\s+التقييم|غير\s+مقيم[ةه]?|لم\s+يقيم|"
    r"قيد\s+التنفيذ|قيد\s+التقدم|غير\s+مكتمل[ةه]?",
    re.I,
)
_POSITIVE_SUBMISSION_RE = re.compile(
    r"submitted\s+for\s+grading|assignment\s+was\s+submitted|"
    r"\bsubmitted\b|\bgraded\b|\bfinished\b|\bcompleted\b|"
    r"تم\s+تسليم(?:ها|ه)?\s+للتقييم|تم\s+تسليم\s+الواجب|تم\s+التسليم|"
    r"سلم\s+قبل|مسلم(?:ة|ه)?|تم\s+التقييم|مقيم(?:ة|ه)?|"
    r"منتهي(?:ة|ه)?|مكتمل(?:ة|ه)?|تم\s+الحل",
    re.I,
)

_VIEW_ONLY_COMPLETION_RE = re.compile(
    r"^(?:done|تم)?\s*:?\s*(?:view(?:\s+the\s+activity)?|عرض(?:\s+النشاط)?|معاينة|مشاهدة)$",
    re.I,
)
_PENDING_COMPLETION_CLASS_RE = re.compile(
    r"alert-secondary|badge-secondary|bg-secondary|text-bg-secondary|"
    r"btn-subtle-body|alert-warning|alert-danger|badge-danger|text-danger|incomplete",
    re.I,
)
_SUCCESS_COMPLETION_CLASS_RE = re.compile(
    r"btn-success|bg-success|badge-success|alert-success|text-bg-success",
    re.I,
)


def parse_submission_state(text: str) -> bool | None:
    """Parse English/Arabic Moodle submission or grading text (negative patterns first)."""
    if not text:
        return None
    cleaned = _ARABIC_DIACRITICS_RE.sub("", str(text))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned or cleaned in ("-", "—", "–"):
        return None
    if _NEGATIVE_SUBMISSION_RE.search(cleaned):
        return False
    if _POSITIVE_SUBMISSION_RE.search(cleaned):
        return True
    return None


def is_completion_region_complete(comp_region: Any) -> bool:
    """Return True only when a Moodle completion region is fully completed.

    Rejects any region containing 'To do', mark-done toggles, secondary/pending states,
    or only a green 'Done: View' partial completion badge.
    """
    if comp_region is None:
        return False

    # 1. Explicit undone manual toggle in Moodle 4.x/5.x
    if comp_region.find(attrs={"data-toggletype": re.compile(r"mark-done", re.I)}):
        return False

    # 2. Any pending / secondary / incomplete badge or button remaining in the region
    if comp_region.find(class_=_PENDING_COMPLETION_CLASS_RE):
        return False

    raw_txt = _ARABIC_DIACRITICS_RE.sub("", comp_region.get_text(" ", strip=True)).lower()
    if any(
        neg in raw_txt
        for neg in (
            "to do",
            "mark as done",
            "not done",
            "not completed",
            "incomplete",
            "failed",
            "للقيام به",
            "المطلوب",
            "وضع علامة",
            "غير مكتمل",
            "لم يكتمل",
            "غير منجز",
            "مطلوب",
        )
    ):
        return False

    # 3. Manual completion toggled to Done (data-toggletype="manual:undo")
    if comp_region.find(attrs={"data-toggletype": re.compile(r"undo", re.I)}):
        return True

    # 4. Success badges/buttons: require at least one non-View-only badge
    success_els = comp_region.find_all(class_=_SUCCESS_COMPLETION_CLASS_RE)
    if success_els:
        for el in success_els:
            el_txt = _ARABIC_DIACRITICS_RE.sub("", el.get_text(" ", strip=True))
            el_txt = re.sub(r"\s+", " ", el_txt).strip()
            if not el_txt:
                continue
            if _VIEW_ONLY_COMPLETION_RE.match(el_txt):
                continue
            return True
        # Every green badge was View-only -> NOT submitted/complete
        return False

    # 5. Text-only fallback (excluding View-only text)
    if _VIEW_ONLY_COMPLETION_RE.match(raw_txt):
        return False
    if any(k in raw_txt for k in ("done:", "done", "تم:", "مكتمل", "منجز")):
        return True
    return False


def extract_moodle_cfg(html_text: str) -> dict[str, Any]:
    """Extract Moodle's embedded M.cfg JSON object (Moodle 4.x/5.x)."""
    if not html_text:
        return {}
    m = re.search(r"M\.cfg\s*=\s*(\{.*?\});", html_text, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def is_login_soup(soup: BeautifulSoup | None) -> bool:
    """Return True if a parsed BeautifulSoup tree represents a Moodle login page."""
    if soup is None:
        return False
    body = soup.find("body")
    if body and "notloggedin" in body.get("class", []):
        return True
    login_form = soup.find("form", id="login") or soup.find(
        "form", action=re.compile(r"/login/index\.php", re.I)
    )
    return bool(login_form and login_form.find("input", {"name": "username"}))


def is_login_html(html_text: str, soup_factory: Any = BeautifulSoup) -> bool:
    """Return True if raw HTML represents a Moodle login page."""
    if not html_text:
        return False
    return is_login_soup(soup_factory(html_text, "lxml"))


def clean_link_text(el: Any) -> str:
    """Extract activity/course link name without Moodle hidden accessibility labels."""
    if el is None:
        return ""
    inst = el.find(class_="instancename")
    target = inst if inst else el
    parts: list[str] = []
    for child in target.descendants:
        if getattr(child, "name", None) and "accesshide" in (child.get("class") or []):
            continue
        if isinstance(child, str):
            parent_classes = getattr(child.parent, "get", lambda k, d=None: [])("class", []) or []
            if "accesshide" not in parent_classes:
                parts.append(child)
    text = re.sub(r"\s+", " ", "".join(parts)).strip()
    if not text:
        text = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
    return text


def extract_course_name(link_el: Any) -> str:
    """Extract a readable course title from a link element and its nearby card/container."""
    parent = link_el.parent
    for _ in range(4):
        if parent is None:
            break
        title = (
            parent.find(class_=re.compile(r"course-title|coursename|fullname"))
            or parent.find("h3")
            or parent.find("h4")
        )
        if title:
            txt = clean_link_text(title)
            if len(txt) > 3:
                return txt
        parent = parent.parent

    name = clean_link_text(link_el)
    return name if len(name) > 2 else ""


def extract_courses_from_soup(soup: BeautifulSoup, courses: list[dict[str, Any]], seen_ids: set[int]) -> None:
    """Extract enrolled course records from dashboard/my-courses/profile HTML."""
    for a in soup.find_all("a", href=re.compile(r"(?:/course/view\.php\?id=\d+|/user/view\.php\?[^\"']*course=\d+)")):
        href = a.get("href", "")
        match = re.search(r"(?:course/view\.php\?id=|course=)(\d+)", href)
        if not match:
            continue
        cid = int(match.group(1))
        if cid <= 1 or cid in seen_ids:
            continue

        name = extract_course_name(a)
        if not name or name in ("الصفحة الرئيسية", "Home", "الموقع الرئيسي", "المقررات", "عرض المزيد", "View more"):
            continue

        seen_ids.add(cid)
        course_url = f"{LMS_BASE_URL}/course/view.php?id={cid}"
        courses.append({
            "id": cid,
            "name": name or f"مادة {cid}",
            "url": course_url,
        })


def extract_course_page_completions(soup: BeautifulSoup | None) -> set[str]:
    """Detect completed activity module IDs (cmids) from Moodle 3.x/4.x/5.x course page."""
    completed: set[str] = set()
    if soup is None:
        return completed
    try:
        for act in soup.find_all(["li", "div"], class_=re.compile(r"\bactivity\b|\bactivity-item\b")):
            cmid = act.get("data-id") or ""
            if not cmid:
                act_id_attr = act.get("id") or ""
                m = re.search(r"module-(\d+)", act_id_attr)
                if m:
                    cmid = m.group(1)
            if not cmid:
                link = act.find("a", href=re.compile(r"/mod/\w+/view\.php\?id=(\d+)"))
                if link:
                    m = re.search(r"id=(\d+)", link.get("href", ""))
                    if m:
                        cmid = m.group(1)
            if not cmid:
                continue

            # 1. Moodle 4.x/5.x completion badge/button (Bootstrap 4 & 5)
            comp_region = act.find(attrs={"data-region": "completion-info"}) or act.find(
                class_=re.compile(r"activity-completion")
            )
            if comp_region:
                if is_completion_region_complete(comp_region):
                    completed.add(str(cmid))
                continue

            # 2. Moodle 3.x completion icon (completion-auto-y / completion-manual-y / completion-auto-pass)
            for img in act.find_all("img"):
                src = (img.get("src") or "").lower()
                title = (img.get("title") or img.get("alt") or "").lower()
                if any(k in src for k in ["completion-auto-y", "completion-manual-y", "completion-auto-pass"]):
                    completed.add(str(cmid))
                    break
                if any(k in title for k in ["completed:", "مكتمل:", "تم إنجاز"]):
                    if "not completed" not in title and "غير مكتمل" not in title:
                        completed.add(str(cmid))
                        break
    except Exception as exc:
        logger.debug("Course completion badge parse error: %s", exc)
    return completed


def parse_activities_index_soup(
    soup: BeautifulSoup,
    course_id: int,
    mod_type: str,
    parse_quiz_grade_fn: Any,
) -> list[dict[str, Any]]:
    """Parse `/mod/{assign,quiz}/index.php` overview tables (Moodle 3.x/4.x/5.x)."""
    activities: list[dict[str, Any]] = []
    table = soup.find("table", class_=re.compile(r"course-overview-table|generaltable"))
    if table:
        all_rows = table.find_all("tr")
        due_col_idx = None
        sub_col_idx = None
        grade_col_idx = None
        name_col_idx = None
        excluded_date_col_idxs: set[int] = set()

        if all_rows:
            header_cells = all_rows[0].find_all(["th", "td"])
            for idx, hc in enumerate(header_cells):
                col_key = (hc.get("data-mdl-overview-column") or "").lower()
                htxt = hc.get_text(" ", strip=True).lower()
                # Never treat allowsubmissionsfromdate / open date columns as a due date!
                if col_key in ("allowsubmissionsfromdate", "timeopen", "opendate", "opened", "startdate") or any(
                    k in htxt
                    for k in (
                        "allow submissions from",
                        "allowsubmissionsfrom",
                        "open date",
                        "opened",
                        "opens",
                        "تاريخ الفتح",
                        "يفتح",
                        "السماح بالتسليم",
                        "بداية التسليم",
                        "وقت الفتح",
                    )
                ):
                    excluded_date_col_idxs.add(idx)
                    continue
                if col_key == "name" or any(k in htxt for k in ["name", "الاسم", "النشاط"]):
                    name_col_idx = idx
                elif col_key in ("duedate", "closedate", "timeclose") or any(
                    k in htxt
                    for k in ["due", "deadline", "استحقاق", "موعد التسليم", "تاريخ التسليم", "close", "إغلاق", "انتهاء"]
                ):
                    due_col_idx = idx
                elif col_key in ("submissionstatus", "status", "attempts") or any(
                    k in htxt for k in ["status", "submission", "تسليم", "محاولة"]
                ):
                    sub_col_idx = idx
                elif col_key == "grade" or any(k in htxt for k in ["grade", "درجة", "علامة", "تقييم"]):
                    grade_col_idx = idx

        for row in all_rows[1:]:
            cells = row.find_all(["td", "th"])
            if not cells:
                continue

            link = row.find("a", class_=re.compile(r"activityname")) or row.find(
                "a", href=re.compile(rf"/mod/{mod_type}/view\.php\?id=\d+")
            )
            if not link:
                continue

            href = clean_url(link.get("href", ""))
            if f"/mod/{mod_type}/" not in href:
                continue

            id_match = re.search(r"id=(\d+)", href)
            activity_id = row.get("data-mdl-overview-cmid") or (id_match.group(1) if id_match else href)

            name_cell = row.find(["td", "th"], attrs={"data-mdl-overview-item": "name"})
            name = clean_link_text(link)
            if not name and name_cell and name_cell.get("data-mdl-overview-value"):
                name = str(name_cell.get("data-mdl-overview-value")).strip()

            due_date = ""
            is_submitted = False
            submission_text = ""
            sub_decided = False

            # 1. Moodle 5.x structured data-mdl-overview-item cells
            due_cell = row.find(["td", "th"], attrs={"data-mdl-overview-item": re.compile(r"duedate|close", re.I)})
            if due_cell:
                ov_val = (due_cell.get("data-mdl-overview-value") or "").strip()
                d_txt = due_cell.get_text(" ", strip=True)
                if ov_val and re.fullmatch(r"\d{9,11}", ov_val) and int(ov_val) > 1_000_000_000:
                    due_date = normalize_due_date_year(ov_val)
                elif d_txt and d_txt not in ("-", "—"):
                    due_date = normalize_due_date_year(d_txt)
            elif due_col_idx is not None and due_col_idx < len(cells):
                d_txt = cells[due_col_idx].get_text(" ", strip=True)
                if d_txt and d_txt not in ("-", "—"):
                    due_date = normalize_due_date_year(d_txt)

            sub_cell = row.find(["td", "th"], attrs={"data-mdl-overview-item": re.compile(r"submission|status", re.I)})
            if not sub_cell and sub_col_idx is not None and sub_col_idx < len(cells):
                sub_cell = cells[sub_col_idx]
            if sub_cell:
                s_txt = sub_cell.get_text(" ", strip=True)
                if s_txt and s_txt not in ("-", "—"):
                    submission_text = s_txt
                    parsed_sub = parse_submission_state(s_txt)
                    if parsed_sub is not None:
                        is_submitted = parsed_sub
                        sub_decided = True

            grade_val = ""
            grade_max = ""
            percentage = ""
            grade_cell = row.find(["td", "th"], attrs={"data-mdl-overview-item": "grade"})
            if not grade_cell and grade_col_idx is not None and grade_col_idx < len(cells):
                grade_cell = cells[grade_col_idx]
            if grade_cell:
                g_txt = grade_cell.get_text(" ", strip=True)
                parsed_g = parse_quiz_grade_fn(g_txt)
                if parsed_g.get("grade_val"):
                    grade_val = parsed_g["grade_val"]
                    grade_max = parsed_g.get("grade_max", "")
                    percentage = parsed_g.get("percentage", "")
                    is_submitted = True
                    sub_decided = True
                    if not submission_text:
                        submission_text = f"تم التقييم ({g_txt})"

            # 2. Fallback cell scan (strictly excluding name, grade, sub, and open/allow-from date columns)
            for idx, cell in enumerate(cells):
                item_attr = (cell.get("data-mdl-overview-item") or "").lower()
                if item_attr in (
                    "name",
                    "grade",
                    "allowsubmissionsfromdate",
                    "timeopen",
                    "opendate",
                    "opened",
                    "startdate",
                ):
                    continue
                if idx in excluded_date_col_idxs:
                    continue
                if name_col_idx is not None and idx == name_col_idx:
                    continue
                if link in cell.descendants or cell == link.parent:
                    continue
                if grade_col_idx is not None and idx == grade_col_idx:
                    continue
                if sub_cell is not None and cell is sub_cell:
                    continue

                cell_txt = cell.get_text(" ", strip=True)
                if not cell_txt or cell_txt in ("-", "—"):
                    continue

                parsed_cell = parse_submission_state(cell_txt)
                if parsed_cell is not None and not sub_decided:
                    is_submitted = parsed_cell
                    submission_text = cell_txt
                    sub_decided = True
                elif not due_date and looks_like_date(cell_txt):
                    due_date = normalize_due_date_year(cell_txt)

            activities.append({
                "id": f"{mod_type}_{activity_id}",
                "name": name or f"{mod_type} #{activity_id}",
                "url": href,
                "type": mod_type,
                "course_id": course_id,
                "due_date": due_date,
                "is_submitted": is_submitted,
                "submission_text": submission_text,
                "grade_val": grade_val,
                "grade_max": grade_max,
                "percentage": percentage,
            })

    if not activities:
        seen_ids: set[str] = set()
        for a in soup.find_all("a", href=re.compile(rf"/mod/{mod_type}/view\.php\?id=\d+")):
            href = clean_url(a.get("href", ""))
            id_match = re.search(r"id=(\d+)", href)
            activity_id = id_match.group(1) if id_match else href
            full_id = f"{mod_type}_{activity_id}"
            if full_id in seen_ids:
                continue
            seen_ids.add(full_id)
            name = clean_link_text(a)
            activities.append({
                "id": full_id,
                "name": name or f"{mod_type} #{activity_id}",
                "url": href,
                "type": mod_type,
                "course_id": course_id,
                "due_date": "",
                "is_submitted": False,
                "submission_text": "",
            })

    return activities


def parse_resources_from_course_soup(soup: BeautifulSoup, course_id: int) -> list[dict[str, Any]]:
    """Extract course resources (files, folders, URLs, pages, forums, scorm, books) from course HTML."""
    resources: list[dict[str, Any]] = []
    resource_patterns = [
        r"/mod/resource/view\.php\?id=\d+",
        r"/mod/folder/view\.php\?id=\d+",
        r"/mod/url/view\.php\?id=\d+",
        r"/mod/page/view\.php\?id=\d+",
        r"/mod/forum/view\.php\?id=\d+",
        r"/mod/scorm/view\.php\?id=\d+",
        r"/mod/book/view\.php\?id=\d+",
    ]
    combined = "|".join(resource_patterns)

    seen: set[str] = set()
    for a in soup.find_all("a", href=re.compile(combined)):
        raw_href = a.get("href", "")
        if not raw_href.startswith("http"):
            raw_href = urljoin(LMS_BASE_URL + "/", raw_href)
        clean_href = clean_url(raw_href)

        id_match = re.search(r"id=(\d+)", clean_href)
        if not id_match:
            continue

        type_match = re.search(r"/mod/(\w+)/", clean_href)
        mod_type = type_match.group(1) if type_match else "resource"

        activity_id = f"{mod_type}_{id_match.group(1)}"
        if activity_id in seen:
            continue
        seen.add(activity_id)

        name = clean_link_text(a)
        resources.append({
            "id": activity_id,
            "name": name or f"مورد #{id_match.group(1)}",
            "url": clean_href,
            "raw_url": raw_href,
            "type": mod_type,
            "course_id": course_id,
            "due_date": "",
        })
    return resources


def parse_assignment_status_soup(soup: BeautifulSoup) -> dict[str, Any]:
    """Parse detailed submission status for an individual Moodle assignment page."""
    result: dict[str, Any] = {
        "is_submitted": False,
        "status_text": "غير معروف",
        "due_date": "",
        "grading_status": "",
        "time_remaining": "",
    }

    for tr in soup.find_all("tr"):
        th = tr.find(["th", "td"])
        cells = tr.find_all("td")
        if not th or not cells:
            continue
        header = th.get_text(strip=True).lower()
        val = cells[-1].get_text(strip=True)

        if any(k in header for k in ["submission status", "حالة التسليم", "حالة المحاولة"]):
            result["status_text"] = val
            st = parse_submission_state(val)
            if st is not None:
                result["is_submitted"] = st

        elif any(k in header for k in ["due date", "تاريخ الاستحقاق", "موعد التسليم", "يُسلَّم"]):
            result["due_date"] = val

        elif any(k in header for k in ["grading status", "حالة التقييم"]):
            result["grading_status"] = val
            # Only promote to submitted when graded; 'Not graded' must NOT overwrite a submitted assignment
            if parse_submission_state(val) is True:
                result["is_submitted"] = True

        elif any(k in header for k in ["file submissions", "تسليمات الملفات", "online text", "نص عبر الإنترنت"]):
            if val and val not in ("-", "—"):
                result["is_submitted"] = True

        elif any(k in header for k in ["time remaining", "الوقت المتبقي"]):
            result["time_remaining"] = val
            if parse_submission_state(val) is True:
                result["is_submitted"] = True

    # Moodle 4.x/5.x activity dates header (Opened / Due)
    if not result["due_date"]:
        act_dates = soup.find(attrs={"data-region": "activity-dates"}) or soup.find(
            class_=re.compile(r"activity-dates")
        )
        if act_dates:
            dates_txt = act_dates.get_text(" ", strip=True)
            m_due = re.search(
                r"(?:Due|Closes|Closed|تاريخ الاستحقاق|موعد التسليم|يغلق|أغلق)\s*:\s*(.+?)"
                r"(?=\s*(?:Opened|Opens|Due|Closes|Closed|تاريخ الفتح|يفتح|$))",
                dates_txt,
                re.I,
            )
            if m_due:
                result["due_date"] = m_due.group(1).strip()

    # Check Moodle completion region safely (ignores View-only / partial completion badges)
    if not result["is_submitted"]:
        comp_region = soup.find(attrs={"data-region": "completion-info"}) or soup.find(
            class_=re.compile(r"activity-completion")
        )
        if comp_region and is_completion_region_complete(comp_region):
            result["is_submitted"] = True

    # Explicit submission/grading status CSS classes (exclude generic completion badge-success)
    if not result["is_submitted"]:
        badge = soup.find(
            class_=re.compile(r"submissionstatussubmitted|earlysubmission|gradingstatusgraded|status-submitted")
        )
        if badge:
            btxt = badge.get_text(" ", strip=True)
            if parse_submission_state(btxt) is True:
                result["is_submitted"] = True

    return result


def parse_grades_soup(soup: BeautifulSoup, course_id: int, fallback_url: str = "") -> list[dict[str, Any]]:
    """Parse Moodle User Grade Report table HTML into grade item dicts."""
    grades: list[dict[str, Any]] = []
    table = soup.find("table", class_=re.compile(r"user-grade|generaltable")) or soup.find("table")
    if not table:
        return grades

    all_rows = table.find_all("tr")
    name_col, grade_col, range_col, pct_col, fb_col = None, None, None, None, None
    if all_rows:
        for idx, hc in enumerate(all_rows[0].find_all(["th", "td"])):
            htxt = hc.get_text(strip=True).lower()
            if any(k in htxt for k in ["grade item", "البند", "عنصر التقييم", "التقييم", "النشاط"]):
                name_col = idx
            elif any(k in htxt for k in ["grade", "الدرجة", "العلامة"]) and "item" not in htxt:
                grade_col = idx
            elif any(k in htxt for k in ["range", "المدى", "النطاق"]):
                range_col = idx
            elif any(k in htxt for k in ["percentage", "النسبة", "%"]):
                pct_col = idx
            elif any(k in htxt for k in ["feedback", "ملاحظات", "التغذية", "إفادة"]):
                fb_col = idx

    seen_items: set[str] = set()
    for row in all_rows[1:]:
        name_cell = row.find(["th", "td"], class_=re.compile(r"column-itemname")) or row.find(
            ["th", "td"], attrs={"headers": re.compile(r"itemname")}
        )
        grade_cell = row.find("td", class_=re.compile(r"column-grade")) or row.find(
            "td", attrs={"headers": re.compile(r"\bgrade\b")}
        )
        range_cell = row.find("td", class_=re.compile(r"column-range")) or row.find(
            "td", attrs={"headers": re.compile(r"\brange\b")}
        )
        pct_cell = row.find("td", class_=re.compile(r"column-percentage")) or row.find(
            "td", attrs={"headers": re.compile(r"percentage")}
        )
        fb_cell = row.find("td", class_=re.compile(r"column-feedback")) or row.find(
            "td", attrs={"headers": re.compile(r"feedback")}
        )

        cells = row.find_all(["th", "td"])
        if not name_cell and name_col is not None and name_col < len(cells):
            name_cell = cells[name_col]
        if not grade_cell and grade_col is not None and grade_col < len(cells):
            grade_cell = cells[grade_col]
        if not range_cell and range_col is not None and range_col < len(cells):
            range_cell = cells[range_col]
        if not pct_cell and pct_col is not None and pct_col < len(cells):
            pct_cell = cells[pct_col]
        if not fb_cell and fb_col is not None and fb_col < len(cells):
            fb_cell = cells[fb_col]

        if not name_cell or not grade_cell:
            continue

        link = name_cell.find("a", href=True)
        item_url = ""
        item_id = ""
        if link:
            href = clean_url(link.get("href", ""))
            item_url = href
            m = re.search(r"id=(\d+)", href)
            if m:
                item_id = f"mod_{m.group(1)}"

        title_el = name_cell.find(class_="gradeitemheader") or link or name_cell
        raw_name = clean_link_text(title_el)
        raw_name = re.sub(
            r"^(?:البند|عنصر التقييم|الفئة|العلامة المحسوبة|Calculated grade|Category|Grade item)\s*:?\s*",
            "",
            raw_name,
            flags=re.I,
        ).strip()
        if not raw_name or len(raw_name) < 2:
            continue

        # Skip Attendance items in Grades (tracked in dedicated Attendance section)
        if "/mod/attendance/" in item_url or raw_name.strip().lower() in (
            "attendance",
            "الحضور والغياب",
            "الحضور",
            "سجل الحضور",
        ):
            continue

        for menu_el in grade_cell.find_all(class_=re.compile(r"action-menu|dropdown")):
            menu_el.decompose()
        grade_val = grade_cell.get_text(" ", strip=True)
        grade_val = re.sub(
            r"\b(?:Actions|Grade analysis|الإجراءات|تحليل الدرجة)\b",
            "",
            grade_val,
            flags=re.I,
        )
        grade_val = re.sub(r"\s+", " ", grade_val).strip()
        if grade_val in ("-", "—", "Empty", "فارغ") or not any(ch.isalnum() for ch in grade_val):
            grade_val = ""

        grade_max = range_cell.get_text(" ", strip=True) if range_cell else ""
        percentage = pct_cell.get_text(" ", strip=True) if pct_cell else ""
        if percentage in ("-", "—"):
            percentage = ""
        feedback = fb_cell.get_text(" ", strip=True) if fb_cell else ""
        if feedback in ("-", "—"):
            feedback = ""

        if not item_url and not grade_val:
            continue

        if not item_id:
            slug = re.sub(r"\W+", "_", raw_name.lower()).strip("_")[:40]
            item_id = f"g_{course_id}_{slug}"

        if item_id in seen_items:
            continue
        seen_items.add(item_id)

        grades.append({
            "item_id": item_id,
            "item_name": raw_name,
            "grade_val": grade_val,
            "grade_max": grade_max,
            "percentage": percentage,
            "feedback": feedback,
            "url": item_url or fallback_url,
            "course_id": course_id,
        })

    return grades


def extract_forum_links(soup: BeautifulSoup, seen_forums: set[str] | None = None) -> list[tuple[str, str]]:
    """Extract unique `/mod/forum/view.php?id=<cmid>` links from course or forum index HTML."""
    seen = seen_forums if seen_forums is not None else set()
    forum_links: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=re.compile(r"/mod/forum/view\.php\?id=\d+")):
        href = a.get("href", "")
        if not href.startswith("http"):
            href = urljoin(LMS_BASE_URL + "/", href)
        m = re.search(r"id=(\d+)", href)
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        fname = clean_link_text(a) or "منتدى الإعلانات"
        forum_links.append((href, fname))
    return forum_links


def parse_forum_announcements_soup(
    f_soup: BeautifulSoup,
    course_id: int,
    forum_name: str,
    seen_discuss: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Parse forum discussion threads from a Moodle forum view page soup."""
    seen = seen_discuss if seen_discuss is not None else set()
    announcements: list[dict[str, Any]] = []

    # Check discussion table rows first
    for tr in f_soup.find_all("tr", class_=re.compile(r"discussion")):
        d_link = tr.find("a", href=re.compile(r"/mod/forum/discuss\.php\?d=\d+"))
        if not d_link:
            continue
        d_href = clean_url(d_link.get("href", ""))
        dm = re.search(r"d=(\d+)", d_href)
        if not dm:
            continue
        discuss_id = f"discuss_{dm.group(1)}"
        if discuss_id in seen:
            continue
        seen.add(discuss_id)

        title = clean_link_text(d_link)
        author_el = tr.find(class_=re.compile(r"author"))
        author = author_el.get_text(" ", strip=True) if author_el else ""
        time_el = tr.find("time")
        posted_date = time_el.get_text(strip=True) if time_el else ""

        announcements.append({
            "discuss_id": discuss_id,
            "forum_name": forum_name,
            "title": title or f"إعلان #{dm.group(1)}",
            "author": author,
            "posted_date": posted_date,
            "url": d_href,
            "course_id": course_id,
        })

    # Fallback for any discuss.php links outside tr.discussion
    for d_link in f_soup.find_all("a", href=re.compile(r"/mod/forum/discuss\.php\?d=\d+")):
        raw_d = d_link.get("href", "")
        if "#" in raw_d:
            continue
        d_href = clean_url(raw_d)
        dm = re.search(r"d=(\d+)", d_href)
        if not dm:
            continue
        discuss_id = f"discuss_{dm.group(1)}"
        if discuss_id in seen:
            continue
        title = clean_link_text(d_link)
        if not title or len(title) < 2:
            continue
        seen.add(discuss_id)
        announcements.append({
            "discuss_id": discuss_id,
            "forum_name": forum_name,
            "title": title,
            "author": "",
            "posted_date": "",
            "url": d_href,
            "course_id": course_id,
        })

    return announcements


def parse_announcement_details_soup(soup: BeautifulSoup) -> dict[str, str]:
    """Parse body text, author, and date from an individual forum discussion page."""
    info = {"body": "", "author": "", "date": ""}
    post_root = (
        soup.find(attrs={"data-region": "post"})
        or soup.find(class_=re.compile(r"forumpost"))
        or soup
    )
    post_body = (
        post_root.find(class_=re.compile(r"post-content-container|posting|fullpost"))
        or post_root.find(attrs={"data-region-content": "forum-post-core"})
    )
    if post_body:
        text = post_body.get_text("\n", strip=True)
        info["body"] = re.sub(r"\n{3,}", "\n\n", text).strip()[:900]

    author_el = post_root.find("a", href=re.compile(r"/user/view\.php\?"))
    if author_el:
        info["author"] = author_el.get_text(strip=True)

    time_el = post_root.find("time") or soup.find("time")
    if time_el:
        info["date"] = time_el.get_text(strip=True)

    return info
