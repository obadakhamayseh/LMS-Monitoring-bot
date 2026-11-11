import requests
import logging
import re
import time
from bs4 import BeautifulSoup
from typing import Optional, List, Dict, Any
from config import LMS_BASE_URL, LMS_LOGIN_URL, LMS_PROXY

logger = logging.getLogger(__name__)


class LMSAuthError(RuntimeError):
    """Raised when an authenticated LMS page still requires a login."""

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

class LMSScraper:

    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        if LMS_PROXY:
            self.session.proxies.update({
                "http": LMS_PROXY,
                "https": LMS_PROXY,
            })
        self.logged_in = False
        self.user_id: Optional[str] = None
        self.full_name: Optional[str] = None
        self.sesskey: Optional[str] = None
        self.error_message: Optional[str] = None
        self.last_request_at = 0.0
        self.last_fetch_ok = True
        self.session_expired = False

    def _is_login_response(self, resp) -> bool:
        if "/login/index.php" in (resp.url or "").lower():
            return True

        soup = BeautifulSoup(resp.text, "lxml")
        body = soup.find("body")
        if body and "notloggedin" in body.get("class", []):
            return True

        login_form = soup.find("form", id="login") or soup.find(
            "form", action=re.compile(r"/login/index\.php", re.I)
        )
        return bool(login_form and login_form.find("input", {"name": "username"}))

    def authenticated_get(self, url: str, timeout: int = 30, **kwargs):
        """Fetch a protected page and report an expired session to the monitor."""
        resp = self.session.get(url, timeout=timeout, **kwargs)
        self.last_request_at = time.monotonic()
        if not self._is_login_response(resp):
            return resp

        logger.info("LMS session expired; waiting for the user to sign in again")
        self.logged_in = False
        self.session_expired = True
        self.last_fetch_ok = False
        raise LMSAuthError("انتهت جلسة LMS؛ يرجى تسجيل الدخول مجدداً من البوت")

    def keep_session_alive(self) -> bool:
        """Make a lightweight authenticated request to keep an active monitor session warm."""
        try:
            resp = self.authenticated_get(f"{LMS_BASE_URL}/my/", timeout=15)
            resp.raise_for_status()
            return self.logged_in
        except Exception as e:
            self.last_fetch_ok = False
            logger.warning(f"LMS keepalive failed: {e}")
            return False

    def close(self):
        self.session.close()

    def login(self) -> bool:
        self.logged_in = False
        self.session_expired = False
        self.last_fetch_ok = True
        self.error_message = None
        try:
            logger.info(f"محاولة تسجيل الدخول: {self.username}")

            resp = self.session.get(LMS_LOGIN_URL, timeout=30)
            self.last_request_at = time.monotonic()
            if resp.status_code != 200:
                self.error_message = f"موقع الجامعة غير متاح (رمز {resp.status_code})"
                logger.error(self.error_message)
                return False

            soup = BeautifulSoup(resp.text, "lxml")

            login_token = ""
            token_input = soup.find("input", {"name": "logintoken"})
            if token_input:
                login_token = token_input.get("value", "")

            payload = {
                "username": self.username,
                "password": self.password,
                "logintoken": login_token,
                "anchor": "",
                "rememberusername": "1",
            }

            resp = self.session.post(
                LMS_LOGIN_URL,
                data=payload,
                timeout=30,
                allow_redirects=True
            )
            self.last_request_at = time.monotonic()

            if resp.status_code != 200:
                self.error_message = f"فشل الاستجابة بعد إرسال البيانات (رمز {resp.status_code})"
                logger.error(self.error_message)
                return False

            soup = BeautifulSoup(resp.text, "lxml")

            if self._check_logged_in(soup):
                self.logged_in = True
                self._extract_user_info(soup)
                logger.info(f"✅ تسجيل الدخول ناجح - {self.full_name}")
                return True

            try:
                my_resp = self.session.get(f"{LMS_BASE_URL}/my/", timeout=15)
                if my_resp.status_code == 200 and "login" not in my_resp.url:
                    my_soup = BeautifulSoup(my_resp.text, "lxml")
                    if self._check_logged_in(my_soup):
                        self.logged_in = True
                        self._extract_user_info(my_soup)
                        logger.info(f"✅ تسجيل الدخول ناجح عبر /my/ - {self.full_name}")
                        return True
            except Exception:
                pass

            err = soup.find(class_=["loginerrors", "alert-danger", "error"]) or soup.find(id="loginerrormessage")
            if err:
                msg = err.get_text(strip=True)
            else:
                title = soup.title.get_text(strip=True) if soup.title else ""
                msg = f"بيانات غير صحيحة أو تم حظر الجلسة (الصفحة: {title})"
            self.error_message = msg
            logger.error(f"❌ فشل تسجيل الدخول: {msg} | URL: {resp.url}")
            return False

        except requests.exceptions.Timeout:
            self.error_message = "انتهت مهلة الاتصال بموقع الجامعة (Timeout)"
            logger.error(self.error_message)
            return False
        except requests.RequestException as e:
            self.error_message = f"خطأ في الاتصال بالشبكة: {e}"
            logger.error(self.error_message)
            return False
        except Exception as e:
            self.error_message = f"خطأ غير متوقع: {e}"
            logger.error(self.error_message)
            return False

    def _check_logged_in(self, soup: BeautifulSoup) -> bool:
        body = soup.find("body")
        if body:
            classes = body.get("class", [])
            if "notloggedin" in classes:
                return False
            uid = body.get("data-userid", "0")
            if uid and uid != "0":
                return True

        if soup.find("a", href=re.compile(r"logout", re.I)):
            return True
        if soup.find("form", action=re.compile(r"logout", re.I)):
            return True
        if soup.find(class_=["userinitials", "userbutton", "usermenu"]):
            return True
        if soup.find(attrs={"data-region": "user-menu"}):
            return True
        return False

    def _extract_sesskey(self, html_or_soup) -> Optional[str]:
        if isinstance(html_or_soup, BeautifulSoup):
            inp = html_or_soup.find("input", {"name": "sesskey"})
            if inp and inp.get("value"):
                self.sesskey = inp["value"]
                return self.sesskey
            logout_a = html_or_soup.find("a", href=re.compile(r"sesskey=([A-Za-z0-9]+)"))
            if logout_a:
                m = re.search(r"sesskey=([A-Za-z0-9]+)", logout_a.get("href", ""))
                if m:
                    self.sesskey = m.group(1)
                    return self.sesskey
            text = str(html_or_soup)
        else:
            text = str(html_or_soup or "")

        m = re.search(r'"sesskey"\s*:\s*"([A-Za-z0-9]+)"', text)
        if m:
            self.sesskey = m.group(1)
            return self.sesskey
        return self.sesskey

    def _extract_user_info(self, soup: BeautifulSoup):
        self._extract_sesskey(soup)
        body = soup.find("body")
        if body:
            self.user_id = body.get("data-userid")

        excluded_labels = {
            "user menu", "language selector", "قائمة المستخدم",
            "الملف الشخصي", "profile", "dashboard", "لوحة التحكم",
            "الصفحة الرئيسية", "home",
        }
        raw_name = None

        # 1. Explicit usertext or logininfo profile link
        for selector in [
            {"class": "usertext"},
            {"class": "username"},
            {"data-region": "user-name"},
        ]:
            el = soup.find(attrs=selector)
            if el:
                val = el.get_text(strip=True)
                if len(val) > 3 and val.lower() not in excluded_labels:
                    raw_name = val
                    break

        if not raw_name:
            login_info = soup.find(class_="logininfo")
            if login_info:
                prof_a = login_info.find("a", href=re.compile(r"/user/profile\.php"))
                if prof_a:
                    val = prof_a.get_text(strip=True)
                    if len(val) > 3 and val.lower() not in excluded_labels:
                        raw_name = val

        # 2. User button aria-label/title (excluding generic menu titles and short 2-char initials)
        if not raw_name:
            user_btn = soup.find(class_="userbutton") or soup.find(class_="usermenu")
            if user_btn:
                for attr_el in user_btn.find_all(attrs={"aria-label": True}):
                    val = attr_el.get("aria-label", "").strip()
                    if len(val) > 4 and val.lower() not in excluded_labels:
                        raw_name = val
                        break
                if not raw_name:
                    for attr_el in user_btn.find_all(attrs={"title": True}):
                        val = attr_el.get("title", "").strip()
                        if len(val) > 4 and val.lower() not in excluded_labels:
                            raw_name = val
                            break

        # 3. Fetch /user/profile.php for authoritative full name
        if not raw_name or len(raw_name.strip()) <= 3:
            try:
                resp = self.session.get(f"{LMS_BASE_URL}/user/profile.php", timeout=15)
                if resp.status_code == 200:
                    prof_soup = BeautifulSoup(resp.text, "lxml")
                    self._extract_sesskey(prof_soup)
                    if not self.user_id:
                        prof_body = prof_soup.find("body")
                        if prof_body:
                            self.user_id = prof_body.get("data-userid")
                    header_el = prof_soup.find(class_="page-header-headings") or prof_soup.find("h1")
                    if header_el:
                        val = header_el.get_text(strip=True)
                        if len(val) > 3 and val.lower() not in excluded_labels:
                            raw_name = val
            except Exception as e:
                logger.debug(f"فشل جلب الملف الشخصي للاسم: {e}")

        if raw_name:
            cleaned = re.sub(r'\s*\d{5,}\s*$', '', raw_name).strip()
            cleaned = re.sub(r'^\s*\d{5,}\s*', '', cleaned).strip()
            if len(cleaned) > 2:
                self.full_name = cleaned
            else:
                self.full_name = raw_name.strip()

    def get_courses(self) -> List[Dict[str, Any]]:
        if not self.logged_in:
            logger.warning("لم يتم تسجيل الدخول")
            return []

        courses = []
        seen_ids: set = set()

        # 1. Try /my/courses.php and extract sesskey
        try:
            resp = self.authenticated_get(f"{LMS_BASE_URL}/my/courses.php", timeout=30)
            soup = BeautifulSoup(resp.text, "lxml")
            self._extract_sesskey(soup)
            self._extract_courses_from_soup(soup, courses, seen_ids)
        except Exception as e:
            logger.error(f"خطأ في صفحة My Courses: {e}")

        # 2. Moodle 4.x AJAX WebService (loads courses rendered dynamically by block_myoverview)
        if self.sesskey:
            for classification in ("all", "inprogress", "All"):
                try:
                    ajax_url = (
                        f"{LMS_BASE_URL}/lib/ajax/service.php"
                        f"?sesskey={self.sesskey}&info=core_course_get_enrolled_courses_by_timeline_classification"
                    )
                    payload = [{
                        "index": 0,
                        "methodname": "core_course_get_enrolled_courses_by_timeline_classification",
                        "args": {
                            "offset": 0,
                            "limit": 0,
                            "classification": classification,
                            "sort": "fullname",
                            "customfieldname": "",
                            "customfieldvalue": "",
                        },
                    }]
                    ajax_resp = self.session.post(ajax_url, json=payload, timeout=20)
                    self.last_request_at = time.monotonic()
                    if ajax_resp.status_code == 200:
                        data = ajax_resp.json()
                        if isinstance(data, list) and data and not data[0].get("error"):
                            course_list = data[0].get("data", {}).get("courses", [])
                            for c_item in course_list:
                                cid = int(c_item.get("id", 0))
                                if cid <= 1 or cid in seen_ids:
                                    continue
                                cname = (c_item.get("fullname") or c_item.get("shortname") or f"مادة {cid}").strip()
                                view_url = c_item.get("viewurl") or f"{LMS_BASE_URL}/course/view.php?id={cid}"
                                seen_ids.add(cid)
                                courses.append({
                                    "id": cid,
                                    "name": cname,
                                    "url": view_url,
                                })
                            if courses:
                                break
                except Exception as e:
                    logger.debug(f"AJAX course fetch ({classification}) failed: {e}")

        # 3. Fallback to /my/ Dashboard
        if not courses:
            try:
                resp = self.authenticated_get(f"{LMS_BASE_URL}/my/", timeout=30)
                soup = BeautifulSoup(resp.text, "lxml")
                self._extract_sesskey(soup)
                self._extract_courses_from_soup(soup, courses, seen_ids)
            except Exception as e:
                logger.error(f"خطأ في Dashboard: {e}")

        # 4. Fallback to User Profile page (supports both /course/view.php?id= and /user/view.php?course=)
        if not courses:
            try:
                prof_url = (
                    f"{LMS_BASE_URL}/user/profile.php?id={self.user_id}&showallcourses=1"
                    if self.user_id else f"{LMS_BASE_URL}/user/profile.php?showallcourses=1"
                )
                resp = self.authenticated_get(prof_url, timeout=30)
                soup = BeautifulSoup(resp.text, "lxml")
                self._extract_courses_from_soup(soup, courses, seen_ids)
            except Exception as e:
                logger.error(f"خطأ في صفحة User Profile Courses: {e}")

        logger.info(f"✅ تم جلب {len(courses)} مادة")
        return courses

    def _extract_courses_from_soup(self, soup: BeautifulSoup,
                                   courses: list, seen_ids: set):
        # Match both standard course view links and profile course links
        for a in soup.find_all("a", href=re.compile(r"(?:/course/view\.php\?id=\d+|/user/view\.php\?[^\"']*course=\d+)")):
            href = a.get("href", "")
            match = re.search(r"(?:course/view\.php\?id=|course=)(\d+)", href)
            if not match:
                continue
            cid = int(match.group(1))
            if cid <= 1 or cid in seen_ids:
                continue

            name = self._extract_course_name(a)
            if not name or name in ("الصفحة الرئيسية", "Home", "الموقع الرئيسي", "المقررات", "عرض المزيد", "View more"):
                continue

            seen_ids.add(cid)
            course_url = f"{LMS_BASE_URL}/course/view.php?id={cid}"

            courses.append({
                "id": cid,
                "name": name or f"مادة {cid}",
                "url": course_url,
            })

    @staticmethod
    def _clean_link_text(el) -> str:
        """Extract activity/course link name without Moodle hidden accessibility labels."""
        if el is None:
            return ""
        inst = el.find(class_="instancename")
        target = inst if inst else el
        parts = []
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

    def _extract_course_name(self, link_el) -> str:
        parent = link_el.parent
        for _ in range(4):
            if parent is None:
                break
            title = (
                parent.find(class_=re.compile(r"course-title|coursename|fullname")) or
                parent.find("h3") or parent.find("h4")
            )
            if title:
                txt = self._clean_link_text(title)
                if len(txt) > 3:
                    return txt
            parent = parent.parent

        name = self._clean_link_text(link_el)
        return name if len(name) > 2 else ""

    def get_course_content(self, course_id: int, course_soup: Optional[BeautifulSoup] = None) -> Dict[str, List[Dict]]:
        self.last_fetch_ok = True
        result = {
            "assignments": [],
            "quizzes": [],
            "resources": [],
            "grades": [],
            "announcements": [],
        }

        result["assignments"] = self._get_activities_from_index(
            course_id, "assign"
        )
        assign_ok = self.last_fetch_ok

        self.last_fetch_ok = True
        result["quizzes"] = self._get_activities_from_index(
            course_id, "quiz"
        )
        quiz_ok = self.last_fetch_ok

        self.last_fetch_ok = True
        resources, page_soup = self._get_resources_from_course_page(
            course_id, soup=course_soup, return_soup=True
        )
        res_ok = self.last_fetch_ok
        result["resources"] = resources

        # Supplement assignments & quizzes from the main course page if any were missed by index.php
        if page_soup is not None:
            seen_assigns = {a["id"] for a in result["assignments"]}
            for a in page_soup.find_all("a", href=re.compile(r"/mod/assign/view\.php\?id=\d+")):
                href = a.get("href", "")
                if not href.startswith("http"):
                    href = LMS_BASE_URL + href
                id_m = re.search(r"id=(\d+)", href)
                if not id_m:
                    continue
                act_id = f"assign_{id_m.group(1)}"
                if act_id in seen_assigns:
                    continue
                seen_assigns.add(act_id)
                name = self._clean_link_text(a)
                result["assignments"].append({
                    "id": act_id,
                    "name": name or f"assign #{id_m.group(1)}",
                    "url": href,
                    "type": "assign",
                    "course_id": course_id,
                    "due_date": "",
                    "is_submitted": False,
                    "submission_text": "",
                })

            seen_quizzes = {q["id"] for q in result["quizzes"]}
            for a in page_soup.find_all("a", href=re.compile(r"/mod/quiz/view\.php\?id=\d+")):
                href = a.get("href", "")
                if not href.startswith("http"):
                    href = LMS_BASE_URL + href
                id_m = re.search(r"id=(\d+)", href)
                if not id_m:
                    continue
                act_id = f"quiz_{id_m.group(1)}"
                if act_id in seen_quizzes:
                    continue
                seen_quizzes.add(act_id)
                name = self._clean_link_text(a)
                result["quizzes"].append({
                    "id": act_id,
                    "name": name or f"quiz #{id_m.group(1)}",
                    "url": href,
                    "type": "quiz",
                    "course_id": course_id,
                    "due_date": "",
                    "is_submitted": False,
                    "submission_text": "",
                })

        # Fetch course grades and announcements
        result["grades"] = self.get_course_grades(course_id)
        result["announcements"] = self.get_course_announcements(course_id, course_soup=page_soup)

        self.last_fetch_ok = (assign_ok and quiz_ok and res_ok) or (res_ok and page_soup is not None)
        return result

    @staticmethod
    def _looks_like_date(text: str) -> bool:
        if not text or len(text) < 6 or len(text) > 90:
            return False
        t = text.lower()
        months = [
            "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
            "يناير", "فبراير", "مارس", "أبريل", "ابريل", "مايو", "يونيو", "يوليو", "أغسطس", "اغسطس",
            "سبتمبر", "أكتوبر", "اكتوبر", "نوفمبر", "ديسمبر",
            "كانون", "شباط", "آذار", "نيسان", "أيار", "حزيران", "تموز", "آب", "أيلول", "تشرين",
        ]
        if any(m in t for m in months) and any(ch.isdigit() for ch in t):
            return True
        if re.search(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b", t):
            return True
        if re.search(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", t):
            return True
        return False

    def _get_activities_from_index(self, course_id: int,
                                   mod_type: str) -> List[Dict]:
        url = f"{LMS_BASE_URL}/mod/{mod_type}/index.php?id={course_id}"
        activities = []

        try:
            resp = self.authenticated_get(url, timeout=30)
            if resp.status_code == 404:
                return activities
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")

            table = soup.find("table", class_=re.compile(r"generaltable"))
            if table:
                all_rows = table.find_all("tr")
                due_col_idx = None
                sub_col_idx = None
                grade_col_idx = None

                if all_rows:
                    header_cells = all_rows[0].find_all(["th", "td"])
                    for idx, hc in enumerate(header_cells):
                        htxt = hc.get_text(strip=True).lower()
                        if any(k in htxt for k in ["due", "استحقاق", "موعد", "تاريخ", "close", "إغلاق", "انتهاء"]):
                            due_col_idx = idx
                        elif any(k in htxt for k in ["status", "submission", "تسليم", "محاولة"]):
                            sub_col_idx = idx
                        elif any(k in htxt for k in ["grade", "درجة", "علامة", "تقييم"]):
                            grade_col_idx = idx

                rows = all_rows[1:]
                for row in rows:
                    cells = row.find_all(["td", "th"])
                    if not cells:
                        continue

                    link = row.find("a", href=re.compile(
                        rf"/mod/{mod_type}/view\.php\?id=\d+"
                    ))
                    if not link:
                        continue

                    href = link.get("href", "")
                    if not href.startswith("http"):
                        href = LMS_BASE_URL + href

                    id_match = re.search(r"id=(\d+)", href)
                    activity_id = id_match.group(1) if id_match else href

                    name = self._clean_link_text(link)

                    due_date = ""
                    is_submitted = False
                    submission_text = ""

                    # Extract from mapped columns first if headers were detected
                    if due_col_idx is not None and due_col_idx < len(cells):
                        d_txt = cells[due_col_idx].get_text(strip=True)
                        if d_txt and d_txt not in ("-", "—"):
                            due_date = d_txt

                    if sub_col_idx is not None and sub_col_idx < len(cells):
                        s_txt = cells[sub_col_idx].get_text(strip=True)
                        if s_txt and s_txt not in ("-", "—"):
                            submission_text = s_txt
                            if re.search(r"submitted for grading|تم تسليمها للتقييم|تم التسليم|submitted|مسلّمة", s_txt, re.I):
                                is_submitted = True

                    for idx, cell in enumerate(cells):
                        # Never mistake the activity name cell or the grade cell for due_date
                        if link in cell.descendants or cell == link.parent:
                            continue
                        if grade_col_idx is not None and idx == grade_col_idx:
                            continue

                        cell_txt = cell.get_text(strip=True)
                        if not cell_txt or cell_txt in ("-", "—"):
                            continue

                        if re.search(r"submitted for grading|تم تسليمها للتقييم|تم التسليم|submitted|مسلّمة", cell_txt, re.I):
                            is_submitted = True
                            submission_text = cell_txt
                        elif re.search(r"no attempt|لا توجد محاولة|لم تسلم|not submitted|draft|مسودة", cell_txt, re.I):
                            is_submitted = False
                            submission_text = cell_txt
                        elif not due_date and self._looks_like_date(cell_txt):
                            due_date = cell_txt

                    activities.append({
                        "id": f"{mod_type}_{activity_id}",
                        "name": name or f"{mod_type} #{activity_id}",
                        "url": href,
                        "type": mod_type,
                        "course_id": course_id,
                        "due_date": due_date,
                        "is_submitted": is_submitted,
                        "submission_text": submission_text,
                    })

            if not activities:
                seen_ids = set()
                for a in soup.find_all("a", href=re.compile(
                    rf"/mod/{mod_type}/view\.php\?id=\d+"
                )):
                    href = a.get("href", "")
                    if not href.startswith("http"):
                        href = LMS_BASE_URL + href
                    id_match = re.search(r"id=(\d+)", href)
                    activity_id = id_match.group(1) if id_match else href
                    full_id = f"{mod_type}_{activity_id}"
                    if full_id in seen_ids:
                        continue
                    seen_ids.add(full_id)
                    name = self._clean_link_text(a)

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

            logger.debug(f"المادة {course_id} | {mod_type}: {len(activities)} نشاط")

        except Exception as e:
            self.last_fetch_ok = False
            logger.error(f"خطأ في جلب {mod_type} للمادة {course_id}: {e}")

        return activities

    def _get_resources_from_course_page(self, course_id: int,
                                        soup: Optional[BeautifulSoup] = None,
                                        return_soup: bool = False):
        resources = []
        url = f"{LMS_BASE_URL}/course/view.php?id={course_id}"

        try:
            if soup is None:
                resp = self.authenticated_get(url, timeout=30)
                resp.raise_for_status()
                soup = BeautifulSoup(resp.text, "lxml")

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

            seen = set()
            for a in soup.find_all("a", href=re.compile(combined)):
                href = a.get("href", "")
                if not href.startswith("http"):
                    href = LMS_BASE_URL + href

                id_match = re.search(r"id=(\d+)", href)
                if not id_match:
                    continue

                type_match = re.search(r"/mod/(\w+)/", href)
                mod_type = type_match.group(1) if type_match else "resource"

                activity_id = f"{mod_type}_{id_match.group(1)}"
                if activity_id in seen:
                    continue
                seen.add(activity_id)

                name = self._clean_link_text(a)

                resources.append({
                    "id": activity_id,
                    "name": name or f"مورد #{id_match.group(1)}",
                    "url": href,
                    "type": mod_type,
                    "course_id": course_id,
                    "due_date": "",
                })

            logger.debug(f"المادة {course_id} | resources: {len(resources)} مورد")

        except Exception as e:
            self.last_fetch_ok = False
            logger.error(f"خطأ في جلب موارد المادة {course_id}: {e}")

        if return_soup:
            return resources, soup
        return resources

    def get_course_info(self, course_id: int):
        course_name = ""
        content = {"assignments": [], "quizzes": [], "resources": []}

        try:
            url = f"{LMS_BASE_URL}/course/view.php?id={course_id}"
            resp = self.authenticated_get(url, timeout=30)

            if resp.status_code == 404 or "login" in resp.url:
                return None, content

            soup = BeautifulSoup(resp.text, "lxml")

            title_el = (
                soup.find("h1") or
                soup.find(class_=re.compile(r"page-header-headings")) or
                soup.find("title")
            )
            if title_el:
                course_name = title_el.get_text(strip=True)
                course_name = course_name.split("|")[0].split(":")[0 if course_name.lower().startswith("course") else 0].strip()
                if ":" in title_el.get_text(strip=True) and len(course_name) < 4:
                    course_name = title_el.get_text(strip=True).split("|")[0].strip()

            if not course_name:
                crumb = soup.find(class_=re.compile(r"breadcrumb"))
                if crumb:
                    items = crumb.find_all("li")
                    if items:
                        course_name = items[-1].get_text(strip=True)

            if not course_name:
                return None, content

            content = self.get_course_content(course_id, course_soup=soup)

        except Exception as e:
            self.last_fetch_ok = False
            logger.error(f"get_course_info error for {course_id}: {e}")
            return None, content

        return course_name, content

    def get_assignment_due_date(self, assignment_url: str) -> str:
        try:
            resp = self.authenticated_get(assignment_url, timeout=20)
            soup = BeautifulSoup(resp.text, "lxml")

            for row in soup.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) >= 2:
                    label = cells[0].get_text(strip=True).lower()
                    if any(k in label for k in ["due", "يُسلَّم", "نهائي", "deadline"]):
                        return cells[1].get_text(strip=True)

        except Exception:
            pass
        return "غير محدد"

    def get_assignment_status(self, url: str) -> Dict[str, Any]:
        """Fetch detailed submission status for an individual assignment."""
        result = {
            "is_submitted": False,
            "status_text": "غير معروف",
            "due_date": "",
            "grading_status": "",
            "time_remaining": "",
        }
        try:
            resp = self.authenticated_get(url, timeout=20)
            if resp.status_code != 200:
                return result
            soup = BeautifulSoup(resp.text, "lxml")

            for tr in soup.find_all("tr"):
                th = tr.find(["th", "td"])
                cells = tr.find_all("td")
                if not th or not cells:
                    continue
                header = th.get_text(strip=True).lower()
                val = cells[-1].get_text(strip=True)
                val_lower = val.lower()

                if any(k in header for k in ["submission status", "حالة التسليم", "حالة المحاولة"]):
                    result["status_text"] = val
                    if any(k in val_lower for k in ["submitted for grading", "تم تسليمها للتقييم", "تم التسليم", "submitted", "مسلّمة"]):
                        result["is_submitted"] = True
                    elif any(k in val_lower for k in ["no attempt", "لا توجد محاولة", "لم تسلم"]):
                        result["is_submitted"] = False

                elif any(k in header for k in ["due date", "تاريخ الاستحقاق", "موعد التسليم"]):
                    result["due_date"] = val

                elif any(k in header for k in ["grading status", "حالة التقييم"]):
                    result["grading_status"] = val

                elif any(k in header for k in ["time remaining", "الوقت المتبقي"]):
                    result["time_remaining"] = val

            badge = soup.find(class_=re.compile(r"submissionstatussubmitted|badge-success|status-submitted"))
            if badge and any(k in badge.get_text().lower() for k in ["submitted", "تسليم"]):
                result["is_submitted"] = True

        except Exception as e:
            logger.warning(f"Error checking assignment status at {url}: {e}")
        return result

    def download_resource(self, url: str, max_mb: int = 40) -> Optional[tuple]:
        """Download a resource file from Moodle safely.
        
        Returns:
            Tuple[str, bytes] -> (filename, file_content_bytes) if successful, None otherwise.
        """
        try:
            resp = self.authenticated_get(url, timeout=30, stream=True, allow_redirects=True)
            if resp.status_code != 200:
                logger.warning(f"Resource download returned {resp.status_code} for {url}")
                return None

            content_type = resp.headers.get("Content-Type", "").lower()
            content_disp = resp.headers.get("Content-Disposition", "")

            # If Moodle returned an HTML page instead of direct file, find the embedded file link
            if "text/html" in content_type and not content_disp:
                soup = BeautifulSoup(resp.text, "lxml")
                file_link = (
                    soup.find("a", href=re.compile(r"/pluginfile\.php/|/mod_resource/content/")) or
                    soup.find("object", data=re.compile(r"/pluginfile\.php/|/mod_resource/content/")) or
                    soup.find("iframe", src=re.compile(r"/pluginfile\.php/|/mod_resource/content/"))
                )
                if not file_link:
                    return None

                target_href = file_link.get("href") or file_link.get("data") or file_link.get("src")
                if not target_href:
                    return None

                if not target_href.startswith("http"):
                    target_href = LMS_BASE_URL + target_href

                resp = self.authenticated_get(target_href, timeout=30, stream=True, allow_redirects=True)
                content_type = resp.headers.get("Content-Type", "").lower()
                content_disp = resp.headers.get("Content-Disposition", "")

            filename = ""
            if content_disp and "filename=" in content_disp:
                fn_match = re.search(r'filename\*?=(?:UTF-8\'\')?["\']?([^"\';\r\n]+)', content_disp, re.I)
                if fn_match:
                    from urllib.parse import unquote
                    filename = unquote(fn_match.group(1).strip('"\''))

            if not filename:
                clean_path = resp.url.split("?")[0].rstrip("/")
                candidate = clean_path.split("/")[-1]
                if "." in candidate and len(candidate) < 120:
                    filename = candidate
                else:
                    filename = "document"

            cl = resp.headers.get("Content-Length")
            max_bytes = max_mb * 1024 * 1024
            if cl and int(cl) > max_bytes:
                logger.info(f"Resource {filename} exceeds size limit: {cl} bytes")
                return None

            chunks = []
            total = 0
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > max_bytes:
                        logger.info(f"Resource {filename} exceeded {max_mb}MB while downloading")
                        return None

            file_bytes = b"".join(chunks)

            if "." not in filename:
                if "pdf" in content_type:
                    filename += ".pdf"
                elif "presentation" in content_type or "powerpoint" in content_type:
                    filename += ".pptx"
                elif "word" in content_type or "document" in content_type:
                    filename += ".docx"
                elif "zip" in content_type:
                    filename += ".zip"
                elif "image/jpeg" in content_type:
                    filename += ".jpg"
                elif "image/png" in content_type:
                    filename += ".png"

            return filename, file_bytes
        except Exception as e:
            logger.warning(f"Error downloading resource {url}: {e}")
            return None

    def get_course_grades(self, course_id: int) -> List[Dict[str, Any]]:
        """Scrape Moodle User Grade Report (/grade/report/user/index.php?id=course_id)."""
        grades = []
        url = f"{LMS_BASE_URL}/grade/report/user/index.php?id={course_id}"
        try:
            resp = self.authenticated_get(url, timeout=25)
            if resp.status_code != 200:
                return grades
            soup = BeautifulSoup(resp.text, "lxml")

            table = (
                soup.find("table", class_=re.compile(r"user-grade|generaltable")) or
                soup.find("table")
            )
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

            seen_items = set()
            for row in all_rows[1:]:
                name_cell = (
                    row.find(["th", "td"], class_=re.compile(r"column-itemname")) or
                    row.find(["th", "td"], attrs={"headers": re.compile(r"itemname")})
                )
                grade_cell = (
                    row.find("td", class_=re.compile(r"column-grade")) or
                    row.find("td", attrs={"headers": re.compile(r"\bgrade\b")})
                )
                range_cell = (
                    row.find("td", class_=re.compile(r"column-range")) or
                    row.find("td", attrs={"headers": re.compile(r"\brange\b")})
                )
                pct_cell = (
                    row.find("td", class_=re.compile(r"column-percentage")) or
                    row.find("td", attrs={"headers": re.compile(r"percentage")})
                )
                fb_cell = (
                    row.find("td", class_=re.compile(r"column-feedback")) or
                    row.find("td", attrs={"headers": re.compile(r"feedback")})
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
                    href = link.get("href", "")
                    if not href.startswith("http"):
                        href = LMS_BASE_URL + href
                    item_url = href
                    m = re.search(r"id=(\d+)", href)
                    if m:
                        item_id = f"mod_{m.group(1)}"

                # Extract clean item name without Moodle category prefix labels
                title_el = name_cell.find(class_="gradeitemheader") or link or name_cell
                raw_name = self._clean_link_text(title_el)
                raw_name = re.sub(
                    r"^(?:البند|عنصر التقييم|الفئة|العلامة المحسوبة|Calculated grade|Category|Grade item)\s*:?\s*",
                    "", raw_name, flags=re.I
                ).strip()
                if not raw_name or len(raw_name) < 2:
                    continue

                grade_val = grade_cell.get_text(" ", strip=True)
                grade_val = re.sub(r"\s+", " ", grade_val).strip()
                if grade_val in ("-", "—", "Empty", "فارغ"):
                    grade_val = ""

                grade_max = range_cell.get_text(" ", strip=True) if range_cell else ""
                percentage = pct_cell.get_text(" ", strip=True) if pct_cell else ""
                feedback = fb_cell.get_text(" ", strip=True) if fb_cell else ""
                if feedback in ("-", "—"):
                    feedback = ""

                # Skip empty category headers that have no link and no grade
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
                    "url": item_url or url,
                    "course_id": course_id,
                })

            logger.debug(f"المادة {course_id} | grades: {len(grades)} عنصر تقييم")
        except Exception as e:
            logger.warning(f"خطأ في جلب العلامات للمادة {course_id}: {e}")
        return grades

    def get_course_announcements(self, course_id: int,
                                 course_soup: Optional[BeautifulSoup] = None) -> List[Dict[str, Any]]:
        """Scrape forum discussions / announcements for a course."""
        announcements = []
        try:
            forum_links = []
            seen_forums = set()

            if course_soup is not None:
                for a in course_soup.find_all("a", href=re.compile(r"/mod/forum/view\.php\?id=\d+")):
                    href = a.get("href", "")
                    if not href.startswith("http"):
                        href = LMS_BASE_URL + href
                    m = re.search(r"id=(\d+)", href)
                    if not m or m.group(1) in seen_forums:
                        continue
                    seen_forums.add(m.group(1))
                    fname = self._clean_link_text(a) or "منتدى الإعلانات"
                    forum_links.append((href, fname))

            if not forum_links:
                idx_url = f"{LMS_BASE_URL}/mod/forum/index.php?id={course_id}"
                resp = self.authenticated_get(idx_url, timeout=20)
                if resp.status_code == 200:
                    idx_soup = BeautifulSoup(resp.text, "lxml")
                    for a in idx_soup.find_all("a", href=re.compile(r"/mod/forum/view\.php\?id=\d+")):
                        href = a.get("href", "")
                        if not href.startswith("http"):
                            href = LMS_BASE_URL + href
                        m = re.search(r"id=(\d+)", href)
                        if not m or m.group(1) in seen_forums:
                            continue
                        seen_forums.add(m.group(1))
                        fname = self._clean_link_text(a) or "منتدى الإعلانات"
                        forum_links.append((href, fname))

            seen_discuss = set()
            for f_url, f_name in forum_links[:4]:
                try:
                    f_resp = self.authenticated_get(f_url, timeout=20)
                    if f_resp.status_code != 200:
                        continue
                    f_soup = BeautifulSoup(f_resp.text, "lxml")

                    # Check discussion table rows first
                    for tr in f_soup.find_all("tr", class_=re.compile(r"discussion")):
                        d_link = tr.find("a", href=re.compile(r"/mod/forum/discuss\.php\?d=\d+"))
                        if not d_link:
                            continue
                        d_href = d_link.get("href", "")
                        if not d_href.startswith("http"):
                            d_href = LMS_BASE_URL + d_href
                        dm = re.search(r"d=(\d+)", d_href)
                        if not dm:
                            continue
                        discuss_id = f"discuss_{dm.group(1)}"
                        if discuss_id in seen_discuss:
                            continue
                        seen_discuss.add(discuss_id)

                        title = self._clean_link_text(d_link)
                        author_el = tr.find(class_=re.compile(r"author"))
                        author = ""
                        posted_date = ""
                        if author_el:
                            a_text = author_el.get_text(" ", strip=True)
                            author = a_text
                        time_el = tr.find("time")
                        if time_el:
                            posted_date = time_el.get_text(strip=True)

                        announcements.append({
                            "discuss_id": discuss_id,
                            "forum_name": f_name,
                            "title": title or f"إعلان #{dm.group(1)}",
                            "author": author,
                            "posted_date": posted_date,
                            "url": d_href,
                            "course_id": course_id,
                        })

                    # Fallback for any discuss.php links outside tr.discussion
                    for d_link in f_soup.find_all("a", href=re.compile(r"/mod/forum/discuss\.php\?d=\d+")):
                        d_href = d_link.get("href", "")
                        if "#" in d_href:
                            continue
                        if not d_href.startswith("http"):
                            d_href = LMS_BASE_URL + d_href
                        dm = re.search(r"d=(\d+)", d_href)
                        if not dm:
                            continue
                        discuss_id = f"discuss_{dm.group(1)}"
                        if discuss_id in seen_discuss:
                            continue
                        title = self._clean_link_text(d_link)
                        if not title or len(title) < 2:
                            continue
                        seen_discuss.add(discuss_id)
                        announcements.append({
                            "discuss_id": discuss_id,
                            "forum_name": f_name,
                            "title": title,
                            "author": "",
                            "posted_date": "",
                            "url": d_href,
                            "course_id": course_id,
                        })
                except Exception as fe:
                    logger.debug(f"Forum fetch error ({f_url}): {fe}")

            logger.debug(f"المادة {course_id} | announcements: {len(announcements)} إعلان")
        except Exception as e:
            logger.warning(f"خطأ في جلب الإعلانات للمادة {course_id}: {e}")
        return announcements

    def get_announcement_details(self, discuss_url: str) -> Dict[str, str]:
        """Fetch the body text, author, and date of a specific forum discussion post."""
        info = {"body": "", "author": "", "date": ""}
        try:
            resp = self.authenticated_get(discuss_url, timeout=20)
            if resp.status_code != 200:
                return info
            soup = BeautifulSoup(resp.text, "lxml")

            post_body = (
                soup.find(class_=re.compile(r"post-content-container|posting|fullpost")) or
                soup.find(attrs={"data-region-content": "forum-post-core"})
            )
            if post_body:
                text = post_body.get_text("\n", strip=True)
                info["body"] = re.sub(r"\n{3,}", "\n\n", text).strip()[:900]

            author_el = soup.find("a", href=re.compile(r"/user/view\.php\?"))
            if author_el:
                info["author"] = author_el.get_text(strip=True)

            time_el = soup.find("time")
            if time_el:
                info["date"] = time_el.get_text(strip=True)
        except Exception as e:
            logger.debug(f"Error fetching announcement details {discuss_url}: {e}")
        return info
