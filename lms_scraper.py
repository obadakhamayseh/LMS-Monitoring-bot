import requests
import logging
import re
from bs4 import BeautifulSoup
from typing import Optional, List, Dict, Any
from config import LMS_BASE_URL, LMS_LOGIN_URL

logger = logging.getLogger(__name__)

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
        self.logged_in = False
        self.user_id: Optional[str] = None
        self.full_name: Optional[str] = None
        self.error_message: Optional[str] = None

    def login(self) -> bool:
        self.error_message = None
        try:
            logger.info(f"محاولة تسجيل الدخول: {self.username}")

            resp = self.session.get(LMS_LOGIN_URL, timeout=30)
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
            else:
                err = soup.find(class_=["loginerrors", "alert-danger", "error"])
                msg = err.get_text(strip=True) if err else "اسم المستخدم أو كلمة المرور غير صحيحة"
                self.error_message = msg
                logger.error(f"❌ فشل تسجيل الدخول: {msg}")
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
            uid = body.get("data-userid", "0")
            if uid and uid != "0":
                return True
        if soup.find("a", href=re.compile(r"logout")):
            return True
        return False

    def _extract_user_info(self, soup: BeautifulSoup):
        body = soup.find("body")
        if body:
            self.user_id = body.get("data-userid")

        raw_name = None

        initials_el = soup.find(class_="userinitials")
        if initials_el:
            raw_name = initials_el.get("aria-label") or initials_el.get("title")

        if not raw_name:
            user_btn = soup.find(class_="userbutton") or soup.find(class_="usermenu")
            if user_btn:
                for attr_el in user_btn.find_all(attrs={"aria-label": True}):
                    val = attr_el.get("aria-label", "").strip()
                    if len(val) > 4 and val not in ("User menu", "Language selector"):
                        raw_name = val
                        break
                if not raw_name:
                    for attr_el in user_btn.find_all(attrs={"title": True}):
                        val = attr_el.get("title", "").strip()
                        if len(val) > 4:
                            raw_name = val
                            break

        if not raw_name:
            for selector in [
                {"class": "usertext"},
                {"class": "username"},
                {"data-region": "user-name"},
            ]:
                el = soup.find(attrs=selector)
                if el and len(el.get_text(strip=True)) > 3:
                    raw_name = el.get_text(strip=True)
                    break

        if not raw_name or len(raw_name.strip()) <= 3:
            try:
                resp = self.session.get(f"{LMS_BASE_URL}/user/profile.php", timeout=15)
                if resp.status_code == 200:
                    prof_soup = BeautifulSoup(resp.text, "lxml")
                    header_el = prof_soup.find(class_="page-header-headings") or prof_soup.find("h1")
                    if header_el:
                        raw_name = header_el.get_text(strip=True)
            except Exception as e:
                logger.debug(f"فشل جلب الملف الشخصي للاسم: {e}")

        if raw_name:
            cleaned = re.sub(r'\s*\d+\s*$', '', raw_name).strip()
            cleaned = re.sub(r'^\s*\d+\s*', '', cleaned).strip()
            self.full_name = cleaned if cleaned else raw_name.strip()

    def get_courses(self) -> List[Dict[str, Any]]:
        if not self.logged_in:
            logger.warning("لم يتم تسجيل الدخول")
            return []

        courses = []
        seen_ids: set = set()

        try:
            resp = self.session.get(
                f"{LMS_BASE_URL}/my/courses.php", timeout=30
            )
            soup = BeautifulSoup(resp.text, "lxml")
            self._extract_courses_from_soup(soup, courses, seen_ids)
        except Exception as e:
            logger.error(f"خطأ في صفحة My Courses: {e}")

        if not courses:
            try:
                resp = self.session.get(f"{LMS_BASE_URL}/my/", timeout=30)
                soup = BeautifulSoup(resp.text, "lxml")
                self._extract_courses_from_soup(soup, courses, seen_ids)
            except Exception as e:
                logger.error(f"خطأ في Dashboard: {e}")

        logger.info(f"✅ تم جلب {len(courses)} مادة")
        return courses

    def _extract_courses_from_soup(self, soup: BeautifulSoup,
                                   courses: list, seen_ids: set):
        for a in soup.find_all("a", href=re.compile(r"/course/view\.php\?id=\d+")):
            href = a.get("href", "")
            match = re.search(r"id=(\d+)", href)
            if not match:
                continue
            cid = int(match.group(1))
            if cid in seen_ids:
                continue
            seen_ids.add(cid)

            name = self._extract_course_name(a)

            if not href.startswith("http"):
                href = LMS_BASE_URL + href

            courses.append({
                "id": cid,
                "name": name or f"مادة {cid}",
                "url": href,
            })

    def _extract_course_name(self, link_el) -> str:
        parent = link_el.parent
        for _ in range(4):
            if parent is None:
                break
            title = (
                parent.find(class_=re.compile(r"course-title|coursename|title|fullname")) or
                parent.find("h3") or parent.find("h4")
            )
            if title:
                txt = title.get_text(strip=True)
                if len(txt) > 3:
                    return txt
            parent = parent.parent

        name = link_el.get_text(strip=True)
        name = re.sub(r"\s+", " ", name).strip()
        return name if len(name) > 2 else ""

    def get_course_content(self, course_id: int) -> Dict[str, List[Dict]]:
        result = {
            "assignments": [],
            "quizzes": [],
            "resources": [],
        }

        result["assignments"] = self._get_activities_from_index(
            course_id, "assign"
        )

        result["quizzes"] = self._get_activities_from_index(
            course_id, "quiz"
        )

        result["resources"] = self._get_resources_from_course_page(course_id)

        return result

    def _get_activities_from_index(self, course_id: int,
                                   mod_type: str) -> List[Dict]:
        url = f"{LMS_BASE_URL}/mod/{mod_type}/index.php?id={course_id}"
        activities = []

        try:
            resp = self.session.get(url, timeout=30)
            if resp.status_code == 404:
                return activities
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")

            table = soup.find("table", class_=re.compile(r"generaltable"))
            if table:
                rows = table.find_all("tr")[1:]
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

                    name = link.get_text(strip=True)
                    name = re.sub(r"\s+", " ", name).strip()

                    due_date = ""
                    if len(cells) >= 2:
                        due_text = cells[-1].get_text(strip=True)
                        if due_text and due_text != "-":
                            due_date = due_text

                    activities.append({
                        "id": f"{mod_type}_{activity_id}",
                        "name": name or f"{mod_type} #{activity_id}",
                        "url": href,
                        "type": mod_type,
                        "course_id": course_id,
                        "due_date": due_date,
                    })

            if not activities:
                for a in soup.find_all("a", href=re.compile(
                    rf"/mod/{mod_type}/view\.php\?id=\d+"
                )):
                    href = a.get("href", "")
                    if not href.startswith("http"):
                        href = LMS_BASE_URL + href
                    id_match = re.search(r"id=(\d+)", href)
                    activity_id = id_match.group(1) if id_match else href
                    name = a.get_text(strip=True)

                    activities.append({
                        "id": f"{mod_type}_{activity_id}",
                        "name": name or f"{mod_type} #{activity_id}",
                        "url": href,
                        "type": mod_type,
                        "course_id": course_id,
                        "due_date": "",
                    })

            logger.debug(f"المادة {course_id} | {mod_type}: {len(activities)} نشاط")

        except Exception as e:
            logger.error(f"خطأ في جلب {mod_type} للمادة {course_id}: {e}")

        return activities

    def _get_resources_from_course_page(self, course_id: int) -> List[Dict]:
        resources = []
        url = f"{LMS_BASE_URL}/course/view.php?id={course_id}"

        try:
            resp = self.session.get(url, timeout=30)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")

            resource_patterns = [
                r"/mod/resource/view\.php\?id=\d+",
                r"/mod/folder/view\.php\?id=\d+",
                r"/mod/url/view\.php\?id=\d+",
                r"/mod/page/view\.php\?id=\d+",
                r"/mod/forum/view\.php\?id=\d+",
                r"/mod/scorm/view\.php\?id=\d+",
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

                name = a.get_text(strip=True)
                name = re.sub(r"\s+", " ", name).strip()

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
            logger.error(f"خطأ في جلب موارد المادة {course_id}: {e}")

        return resources

    def get_course_info(self, course_id: int):
        course_name = ""
        content = {"assignments": [], "quizzes": [], "resources": []}

        try:
            url = f"{LMS_BASE_URL}/course/view.php?id={course_id}"
            resp = self.session.get(url, timeout=30)

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
                course_name = course_name.split("|")[0].strip()

            if not course_name:
                crumb = soup.find(class_=re.compile(r"breadcrumb"))
                if crumb:
                    items = crumb.find_all("li")
                    if items:
                        course_name = items[-1].get_text(strip=True)

            if not course_name:
                return None, content

            content = self.get_course_content(course_id)

        except Exception as e:
            logger.error(f"get_course_info error for {course_id}: {e}")
            return None, content

        return course_name, content

    def get_assignment_due_date(self, assignment_url: str) -> str:
        try:
            resp = self.session.get(assignment_url, timeout=20)
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
