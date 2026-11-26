# FIXES_TRACKER.md — سجل تتبّع إصلاحات وميزات Mutah LMS Monitor

**حالات البند:** `⬜` لم يبدأ | `🔄` جارٍ | `✅` تم + اختبار | `⏭️` غير موجود في الكود (مع الدليل)

## المرحلة P0 — الأمان الحرج ومنع الانهيارات والإنذارات الكاذبة

| ID | الوصف | الحالة | الملفات المعدّلة | الاختبار / الدليل |
|---|---|---|---|---|
| C1 | XSS في `webapp.html`: Event Delegation + `data-*` + `safeHttpUrl()` | ✅ | webapp.html | `tests/test_frontend_security.py` (10 passed; static + Node VM exploit & delegation tests) |
| C2 | سباقات `LMSScraper`: `LMSHttpClient` + `FetchResult` + فحص تسلسلي | ✅ | lms_scraper.py, monitor.py, webapp_server.py | `tests/test_scraper_p0.py`, `tests/test_monitor_p0.py`, `tests/test_webserver_p0.py` |
| C3 | OOM + SSRF: `authenticated_get` / allowlist / `/mod/url/` / filename / status | ✅ | lms_scraper.py, monitor.py | `tests/test_scraper_p0.py` (`test_c3_*`: 6 cases), `tests/test_monitor_p0.py` (`test_c3_*`: 1 case) |
| C4 | التشفير Fail-Closed (`ENCRYPTION_KEY` مستقل + `enc:v1:` + ترحيل آمن + README) | ✅ | security.py, database.py, README.md, .env.example, render.yaml | `tests/test_security_c4.py` (8 passed) |
| C5 | حدود جسم الطلب (`MAX_BODY_BYTES`, 413/400) + التحقق من `auth_date` | ✅ | webapp_server.py | `tests/test_webserver_p0.py` (30 passed) |
| H1 | نقل `initData` إلى الترويسة `X-Telegram-Init-Data` + تحديث CORS و`sw.js` | ✅ | webapp_server.py, webapp.html, sw.js (verified) | `tests/test_webserver_p0.py` + `tests/test_frontend_security.py` (10 passed; header-only + sw.js verified) |
| C6 | `parse_submission_state()` + شارة `View` / `To do` | ✅ | lms_scraper.py | `tests/test_scraper_p0.py` (`test_c6_*`: 32 cases) |
| C7 | أشهر عربية مركّبة + `p.m.`/`a.m.` + `ZoneInfo("Asia/Amman")` + `tzdata` | ✅ | reminder.py, requirements.txt | `tests/test_reminder_dates.py` (70 passed) |
| C8 | منع `q.answer()` المزدوج + `_answer_once` + `error_handler` عام | ✅ | bot.py | `tests/test_bot_callbacks_login.py` (28 C8 tests passed) |
| C10 | حذف رسالة كلمة المرور فوراً في `try/finally` + عدم تعديل كلمة المرور | ✅ | bot.py, webapp_server.py (by @webserver), webapp.html (by @frontend) | `tests/test_bot_callbacks_login.py` (3 C10 tests passed) + `tests/test_webserver_p0.py` (`test_c10_*`) + `tests/test_frontend_security.py` (`test_submit_login_preserves_password_verbatim`) |
| H11 | إلزام `OWNER_TELEGRAM_ID` وإزالة المعرّف الحقيقي الثابت من كامل المستودع | ✅ | config.py, .env.example, render.yaml, README.md | `tests/test_config_h11.py` (10 passed) |

## المرحلة P1 — الاستقرار، قاعدة البيانات، الأداء، وأمان PWA

| ID | الوصف | الحالة | الملفات المعدّلة | الاختبار / الدليل |
|---|---|---|---|---|
| H5 | توحيد مفاتيح الحقول (`item_url`/`att_url`) + إزالة `sync_logs` + `toggleDone(0)` + `/api/mark-submitted` + JSONB `\|\|` + إحياء التذكير | ✅ | database.py, webapp_server.py, bot.py, webapp.html | `tests/test_database_p1.py` (`merge_user_meta` + `mark_item_submitted`), `tests/test_webserver_p1.py` (`test_h5_*`), `tests/test_bot_p1.py` (`test_h5_*`), `tests/test_frontend_p1.py` (`test_h5_*`: 3 passed) |
| H4 | `db.init_db()` قبل `web_thread` + `_ENGINE` مقفلة + ترويسات أمان (CSP/HSTS/XCTO/no-store) + تقييد CORS + timeouts | ✅ | bot.py, monitor.py, webapp_server.py | `tests/test_bot_p1.py` (`test_h4_*`), `tests/test_monitor_p1.py` (`test_h4_and_h13_*`), `tests/test_webserver_p1.py` (`test_h4_*`) |
| H3 | `ConnectionPool` (`psycopg_pool`) + معاملات ذرية (`delete_user`) + عدم إعادة `IntegrityError` + SQLite FK/WAL + فهارس + مراجعة `DIRECT_URL` | ✅ | database.py, requirements.txt, README.md | `tests/test_database_p1.py` (16 passed) + `tests/test_security_c4.py` (8 passed) |
| H6 | `courses.is_seeded` للإعلانات والحضور + `notified` بعد نجاح `_send` + عدم حذف التذكير عند فراغ `due_str` | ✅ | database.py, monitor.py, reminder.py | `tests/test_database_p1.py` (`test_h6_*`), `tests/test_monitor_p1.py` (`test_h6_*`), `tests/test_reminder_p1.py` (`test_h6_*`) |
| H2 | `clearClientSession()` + `sw.js` لا يخزّن 401/403 + حذف `app_sessions` + TTL لرموز الأجهزة + rate limit لـ `/api/pair` لكل IP | ✅ | database.py, webapp_server.py, webapp.html, sw.js | `tests/test_database_p1.py` (`test_h2_*`, `test_h3_h2_*`), `tests/test_webserver_p1.py` (`test_h2_*`), `tests/test_frontend_p1.py` (`test_h2_*`: 2 passed) |
| H7 | فحص `due` قبل `_ensure_logged_in` + تقليل Keepalive + Exponential Backoff + تهدئة `last_request_at` + إلغاء تحليل BS4 المزدوج | ✅ | lms_scraper.py, monitor.py | `tests/test_scraper_p1.py` (`test_h7_*`), `tests/test_monitor_p1.py` (`test_h7_*`) |
| H8 | الحضور: `col_map` ديناميكي + `Group` + `P/A/L/E` + استبعاد `allowsubmissionsfromdate` + تمرير AJAX عبر `LMSHttpClient` | ✅ | lms_scraper.py, monitor.py | `tests/test_scraper_p1.py` (`test_h8_*`: 4 tests) |
| H10 | الواجهة: `try/catch` حول `JSON.parse` + `parseAnyDate` (أقرب سنة/عربي/Safari) + إصلاح `dueInfo` + فحص `res.ok` مع Rollback | ✅ | webapp.html | `tests/test_frontend_p1.py` (`test_h10_*`: 4 passed) |
| H12 | `ParseMode.HTML` مع `html.escape` + تقسيم آمن بدل `[:3900]` + دالة إرسال تحترم `RetryAfter` وتعالج `Forbidden` | ✅ | telegram_utils.py, bot.py, monitor.py, reminder.py | `tests/test_bot_p1.py` (`test_h12_*`: 9 passed), `tests/test_monitor_p1.py` (`test_h12_*`, `test_h6_and_h12_*`), `tests/test_reminder_p1.py` (`test_h12_*`) |
| H13 | تشغيل `logout()` وعمليات DB الثقيلة في `run_in_executor` + انتظار المهام الجارية قبل `close()` في `stop()` | ✅ | bot.py, monitor.py, reminder.py | `tests/test_bot_p1.py` (`test_h13_*`, `test_h4_and_h13_*`), `tests/test_monitor_p1.py` (`test_h4_and_h13_*`, `test_h13_*`) |
| H14 | `sw.js` (`/logo.png`, فحص `res.ok`, معالجة فشل `addAll`) + `webapp.html` (`defer` لـ telegram-web-app.js، إزالة `user-scalable=no`، ARIA/Escape/Focus) | ✅ | sw.js, webapp.html | `tests/test_frontend_p1.py` (`test_h14_*`: 2 passed) |
| H15 | إلغاء N+1 في `/api/user-data` و`/api/force-check` + احترام `notify_quiz` في `get_pending_reminders` | ✅ | database.py, webapp_server.py | `tests/test_database_p1.py` (`test_h15_*`), `tests/test_webserver_p1.py` (`test_h15_*`) |
| H16 | استبدال `mutahAppToken` بكوكي `HttpOnly; Secure; SameSite=Strict` + `apiFetch()` صريحة + توحيد حدود `check_interval` (5–1440) | ✅ | webapp_server.py, bot.py, webapp.html | `tests/test_webserver_p1.py` (`test_h16_*`), `tests/test_bot_p1.py` (`test_h16_*`: 13 passed), `tests/test_frontend_p1.py` (`test_h16_*`: 2 passed) + `tests/test_frontend_security.py` (10 passed) |
| C9 | الاستضافة: `healthCheckPath: /health` في `render.yaml` + توثيق UptimeRobot + تخزين `_pair_codes` في DB | ✅ | database.py, webapp_server.py, render.yaml, .env.example, README.md | `tests/test_database_p1.py` (`test_h2_c9_*`, `test_c9_*`), `tests/test_webserver_p1.py` (`test_c9_*`) |

## المرحلة P2 — إعادة الهيكلة والمعايير الصناعية

| ID | الوصف | الحالة | الملفات المعدّلة | الاختبار / الدليل |
|---|---|---|---|---|
| H9 | تفكيك `bot.py` و`lms_scraper.py` و`webapp.html` إلى وحدات منظمة + FastAPI + APScheduler + CI + lockfile | ✅ | `scraper/{__init__,client,dates,parsers,attendance,quizzes,core}.py`, `lms_scraper.py` (thin facade), `services/{__init__,sync_service}.py`, `monitor.py` (`AsyncIOScheduler` + `run_with_user_scraper`), `reminder.py` (`AsyncIOScheduler`), `bot.py` + `handlers/*`, `webapp_server.py` (FastAPI + Uvicorn), `webapp.html` (84 lines), `static/css/app.css`, `static/js/{state,dates,api,render,actions}.js`, `sw.js` (v4), `pyproject.toml`, `.github/workflows/ci.yml`, `requirements.txt`, `requirements-dev.txt`, `requirements.lock`, `README.md` | `tests/test_scraper_p0.py` (42), `tests/test_scraper_p1.py` (5), `tests/test_monitor_p0.py` (3), `tests/test_monitor_p1.py` (11), `tests/test_reminder_dates.py` (70), `tests/test_reminder_p1.py` (3), `tests/test_p2_scheduler_sync.py` (8), `tests/test_bot_p2.py` (11), `tests/test_webserver_p0.py` (30), `tests/test_webserver_p1.py` (38), `tests/test_fastapi_p2.py` (22), `tests/test_frontend_security.py` (10), `tests/test_frontend_p1.py` (14), `tests/test_frontend_p2.py` (5) + `ruff check` + `node --check` |

## المشاكل المتوسطة والمنخفضة

| ID | الوصف | الحالة | الملفات المعدّلة | الاختبار / الدليل |
|---|---|---|---|---|
| M-QUIZ | رؤوس `quizattemptsummary` + منع `dd/mm/yyyy` كعلامة + `,`/`٫` + `notify_grade` | ✅ | `scraper/quizzes.py` (`build_quiz_attempt_col_map`, `_SLASH_DATE_RE`, `_normalize_grade_numbers` للفاصلة `,`/`٫` والأرقام العربية), `scraper/core.py`, `monitor.py` (تقييد إشعارات علامات الكويز والعلامات العادية بـ `notify_grade`) | `tests/test_medium_scraper_monitor.py` (`test_m_quiz_*`: 21 passed) + `tests/test_parsers_fixtures.py` (11) + `tests/test_monitor_p1.py` (11) |
| M-ICS | `.ics` RFC 5545 + DTEND/UID/DTSTAMP + `revokeObjectURL` + بلا موعد وهمي | ✅ | `static/js/dates.js` (`escapeICSText`, `formatICSDateUTC`), `static/js/actions.js` (`downloadICS` CRLF + `UID`/`DTSTAMP`/`DTSTART`/`DTEND` + `setTimeout(..., 2000)` + تخطي المهام بلا تاريخ) | `tests/test_medium_frontend_docs.py` (`test_m_ics_*`: 3 passed) + `node --check` |
| M-GUARD | `TypeHandler(owner_guard)` group=-1 + `set_chat_menu_button` للمالك | ✅ | `handlers/common.py` (`owner_guard` + `ApplicationHandlerStop`), `handlers/__init__.py` (`TypeHandler(Update, owner_guard)` في `group=-1`), `bot.py` (`set_chat_menu_button(chat_id=OWNER_TELEGRAM_ID, ...)`), `tests/test_bot_p2.py` | `tests/test_medium_bot_web.py` (`test_m_guard_*`: 3 passed) + `tests/test_bot_callbacks_login.py` (31) + `tests/test_bot_p1.py` (29) + `tests/test_bot_p2.py` (11) |
| M-VALID | تحقق مدخلات API + تسجيل 4xx/5xx | ✅ | `webapp_server.py` (`_validate_course_id` `1..999999999` عبر `/api/add-course`, `/api/toggle-course`, `/api/delete-course`, `/api/mark-submitted` + `AccessErrorLoggingMiddleware` لتسجيل `4xx` WARNING و`5xx` ERROR بلا query/أسرار) | `tests/test_medium_bot_web.py` (`test_m_valid_*`: 52 passed) + `tests/test_webserver_p0.py` (30) + `tests/test_webserver_p1.py` (38) + `tests/test_fastapi_p2.py` (22) |
| M-ANN | مؤلف الإعلان + أولوية منتدى الإعلانات + `urljoin` | ✅ | `scraper/parsers.py` (`resolve_lms_url` بـ `urljoin`، `is_announcement_forum` + فرز منتديات الإعلانات أولاً، قصر المؤلف على `data-region="post"` / `.forumpost`), `scraper/core.py` (إزالة حد `forum_links[:4]` + `resolve_lms_url`), `scraper/attendance.py` (`resolve_lms_url`) | `tests/test_medium_scraper_monitor.py` (`test_m_ann_*`: 3 passed) + `tests/test_parsers_fixtures.py` (11) + `tests/test_scraper_p0.py` (42) + `tests/test_scraper_p1.py` (5) |
| M-REF | `replaceState` قبل await + `no-referrer` + `RotatingFileHandler` | ✅ | `webapp.html` (`<meta name="referrer" content="no-referrer">`), `static/js/actions.js` (`history.replaceState` قبل `await pairWithCode`), `bot.py` (`RotatingFileHandler` 5MB × 3) | `tests/test_medium_frontend_docs.py` (`test_m_ref_*`: 2 passed) + `tests/test_medium_bot_web.py` (`test_m_ref_*`: 1 passed) + `node --check` |
| M-UTC | `last_check` بصيغة UTC ISO-8601 | ✅ | `database.py` (`utc_now_iso` + `update_last_check` بصيغة UTC ISO-8601 `Z`), `monitor.py` (`_is_user_due` مقارنة UTC موحدة), `webapp_server.py` (بواسطة `@m-bot-web`) | `tests/test_medium_scraper_monitor.py` (`test_m_utc_*`: 2 passed) + `tests/test_database_p1.py` (16) + `tests/test_monitor_p1.py` (11) |
| M-DOCS | تصحيح README (Fernet) + الفصل الدراسي الديناميكي | ✅ | `README.md` (verified Fernet `AES-128-CBC + HMAC-SHA256`, zero `AES-256`), `static/js/dates.js` (`getCurrentSemesterLabel`), `static/js/render.js`, `webapp.html` (`#semesterLabel`) | `tests/test_medium_frontend_docs.py` (`test_m_docs_*`: 3 passed) + `node --check` |

## الميزات الجديدة (F1–F8)

| ID | الوصف | الحالة | الملفات المعدّلة | الاختبار / الدليل |
|---|---|---|---|---|
| F1 | `MoodleApiClient` (API-First هجين خلف feature flag معطّل افتراضياً + Fallback) | ⬜ | | |
| F2 | FastAPI + Rate Limiting + Docs (ضمن P2) | ✅ | `webapp_server.py` (FastAPI + Uvicorn على حلقة asyncio المشتركة، Pydantic v2، CORS/CSP بدون `'unsafe-inline'` في `script-src`، Rate Limiting لكل IP، `/static/*` محمية ضد path traversal، OpenAPI `/docs` و`/openapi.json`، تفويض `seed_course_snapshot`) | `tests/test_webserver_p0.py` (30 passed), `tests/test_webserver_p1.py` (38 passed), `tests/test_fastapi_p2.py` (22 passed) — 90 passed + `ruff check` clean |
| F3 | اختبارات المحللات الأربعة + CI (ضمن P2) | ✅ | `tests/fixtures/{dates_samples.json,submission_moodle5.html,quiz_grade_moodle5.html,attendance_moodle5.html,announcements_moodle5.html}`, `tests/test_parsers_fixtures.py`, `pyproject.toml`, `.github/workflows/ci.yml`, `requirements-dev.txt`, `requirements.lock` | `tests/test_parsers_fixtures.py` (11 passed: submission, quiz_grade, attendance, announcements, dates) + `ruff check` + `.github/workflows/ci.yml` (`ruff` + `pytest` + `node --check`) |
| F4 | Live iCal Feed `/api/calendar.ics?token=` | ⬜ | | |
| F5 | Daily Digest | ⬜ | | |
| F6 | Absence Risk | ⬜ | | |
| F7 | Web Push (VAPID) | ⬜ | | |
| F8 | Observability (`/status`, `/health`، JSON logs) | ⬜ | | |
