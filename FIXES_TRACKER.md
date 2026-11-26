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
| H5 | توحيد مفاتيح الحقول (`item_url`/`att_url`) + إزالة `sync_logs` + `toggleDone(0)` + `/api/mark-submitted` + JSONB `\|\|` + إحياء التذكير | ⬜ | | |
| H4 | `db.init_db()` قبل `web_thread` + `_ENGINE` مقفلة + ترويسات أمان (CSP/HSTS/XCTO/no-store) + تقييد CORS + timeouts | ⬜ | | |
| H3 | `ConnectionPool` (`psycopg_pool`) + معاملات ذرية (`delete_user`) + عدم إعادة `IntegrityError` + SQLite FK/WAL + فهارس + مراجعة `DIRECT_URL` | ⬜ | | |
| H6 | `courses.is_seeded` للإعلانات والحضور + `notified` بعد نجاح `_send` + عدم حذف التذكير عند فراغ `due_str` | ⬜ | | |
| H2 | `clearClientSession()` + `sw.js` لا يخزّن 401/403 + حذف `app_sessions` + TTL لرموز الأجهزة + rate limit لـ `/api/pair` لكل IP | ⬜ | | |
| H7 | فحص `due` قبل `_ensure_logged_in` + تقليل Keepalive + Exponential Backoff + تهدئة `last_request_at` + إلغاء تحليل BS4 المزدوج | ⬜ | | |
| H8 | الحضور: `col_map` ديناميكي + `Group` + `P/A/L/E` + استبعاد `allowsubmissionsfromdate` + تمرير AJAX عبر `LMSHttpClient` | ⬜ | | |
| H10 | الواجهة: `try/catch` حول `JSON.parse` + `parseAnyDate` (أقرب سنة/عربي/Safari) + إصلاح `dueInfo` + فحص `res.ok` مع Rollback | ⬜ | | |
| H12 | `ParseMode.HTML` مع `html.escape` + تقسيم آمن بدل `[:3900]` + دالة إرسال تحترم `RetryAfter` وتعالج `Forbidden` | ⬜ | | |
| H13 | تشغيل `logout()` وعمليات DB الثقيلة في `run_in_executor` + انتظار المهام الجارية قبل `close()` في `stop()` | ⬜ | | |
| H14 | `sw.js` (`/logo.png`, فحص `res.ok`, معالجة فشل `addAll`) + `webapp.html` (`defer` لـ telegram-web-app.js، إزالة `user-scalable=no`، ARIA/Escape/Focus) | ⬜ | | |
| H15 | إلغاء N+1 في `/api/user-data` و`/api/force-check` + احترام `notify_quiz` في `get_pending_reminders` | ⬜ | | |
| H16 | استبدال `mutahAppToken` بكوكي `HttpOnly; Secure; SameSite=Strict` + `apiFetch()` صريحة + توحيد حدود `check_interval` (5–1440) | ⬜ | | |
| C9 | الاستضافة: `healthCheckPath: /health` في `render.yaml` + توثيق UptimeRobot + تخزين `_pair_codes` في DB | ⬜ | | |

## المرحلة P2 — إعادة الهيكلة والمعايير الصناعية

| ID | الوصف | الحالة | الملفات المعدّلة | الاختبار / الدليل |
|---|---|---|---|---|
| H9 | تفكيك `bot.py` و`lms_scraper.py` إلى وحدات منظمة + FastAPI + APScheduler + CI + lockfile | ⬜ | | |

## المشاكل المتوسطة والمنخفضة

| ID | الوصف | الحالة | الملفات المعدّلة | الاختبار / الدليل |
|---|---|---|---|---|
| M-QUIZ | رؤوس `quizattemptsummary` + منع `dd/mm/yyyy` كعلامة + `,`/`٫` + `notify_grade` | ⬜ | | |
| M-ICS | `.ics` RFC 5545 + DTEND/UID/DTSTAMP + `revokeObjectURL` + بلا موعد وهمي | ⬜ | | |
| M-GUARD | `TypeHandler(owner_guard)` group=-1 + `set_chat_menu_button` للمالك | ⬜ | | |
| M-VALID | تحقق مدخلات API + تسجيل 4xx/5xx | ⬜ | | |
| M-ANN | مؤلف الإعلان + أولوية منتدى الإعلانات + `urljoin` | ⬜ | | |
| M-REF | `replaceState` قبل await + `no-referrer` + `RotatingFileHandler` | ⬜ | | |
| M-UTC | `last_check` بصيغة UTC ISO-8601 | ⬜ | | |
| M-DOCS | تصحيح README (Fernet) + الفصل الدراسي الديناميكي | ⬜ | | |

## الميزات الجديدة (F1–F8)

| ID | الوصف | الحالة | الملفات المعدّلة | الاختبار / الدليل |
|---|---|---|---|---|
| F1 | `MoodleApiClient` (API-First هجين خلف feature flag معطّل افتراضياً + Fallback) | ⬜ | | |
| F2 | FastAPI + Rate Limiting + Docs (ضمن P2) | ⬜ | | |
| F3 | اختبارات المحللات الأربعة + CI (ضمن P2) | ⬜ | | |
| F4 | Live iCal Feed `/api/calendar.ics?token=` | ⬜ | | |
| F5 | Daily Digest | ⬜ | | |
| F6 | Absence Risk | ⬜ | | |
| F7 | Web Push (VAPID) | ⬜ | | |
| F8 | Observability (`/status`, `/health`، JSON logs) | ⬜ | | |
