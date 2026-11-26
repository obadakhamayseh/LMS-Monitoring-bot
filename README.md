# 🎓 بوت مراقب نظام التعليم الإلكتروني — Mutah LMS Monitor

> مساعد أكاديمي شخصي متكامل لطلاب جامعة مؤتة (`mulms.mutah.edu.jo`): يراقب المقررات على Moodle دورياً عبر بوت تيليجرام وتطبيق ويب تقدّمي (Telegram Mini App / PWA) مع تقويم حيّ (iCal)، ملخص يومي صباحي، رادار خطر الحرمان بالغياب، إشعارات Web Push، ورصد تشغيلي كامل.

---

## ✨ الميزات الجديدة والقدرات الرئيسية (F1–F8)

| الرمز | الميزة | الحالة الافتراضية | مفتاح التفعيل / التعطيل |
|---|---|---|---|
| **F1** | **طبقة Moodle API الهجينة (`MoodleApiClient`)**: تحاول استخدام Moodle Mobile WS / AJAX JSON أولاً وتعود تلقائياً للـ HTML Scraping عند التعذّر مع قاطع دائرة (Circuit Breaker) لمدة ساعة | معطّلة افتراضياً (`DECISIONS.md` D2) | عالمياً: `ENABLE_MOODLE_API=0/1` · للمستخدم: مفتاح `use_moodle_api` في الإعدادات |
| **F2** | **خادم FastAPI + Uvicorn + تحديد المعدل + OpenAPI**: خادم API حديث يعمل ضمن حلقة `asyncio` المشتركة، تحقق صارم عبر Pydantic v2، ترويسات أمان (`CSP`/`HSTS`/`XCTO`)، وتوثيق `/docs` | مفعّلة دائماً | مدمج في `webapp_server.py` |
| **F3** | **اختبارات المحلّلات (Fixtures) + خط CI**: اختبارات ثابتة لعينات Moodle 5 (التسليم، الكويزات، الحضور، الإعلانات، التواريخ) + GitHub Actions CI + `requirements.lock` | مفعّلة دائماً | `.github/workflows/ci.yml` و`tests/fixtures/` |
| **F4** | **تغذية التقويم الحيّة (Live iCal Feed)**: رابط اشتراك قياسي RFC 5545 (`/api/calendar.ics?token=...`) يتحدّث تلقائياً في تقويم Google وApple وOutlook مع تدوير وإلغاء التوكن بضغطة زر | مفعّلة افتراضياً | عالمياً: `ENABLE_ICAL_FEED=1/0` · للمستخدم: مفتاح `enable_ical_feed` في الإعدادات |
| **F5** | **الملخص اليومي الصباحي (Daily Digest)**: رسالة HTML صباحية بتوقيت عمّان (`Asia/Amman`) تلخّص مهام وتذكيرات الـ 48 ساعة القادمة والمقررات المعرّضة لخطر الغياب | مفعّلة افتراضياً (`08:00`) | عالمياً: `ENABLE_DAILY_DIGEST=1/0` و`DEFAULT_DIGEST_TIME=08:00` · للمستخدم: `digest_enabled` و`digest_time` في الإعدادات |
| **F6** | **محرك خطر الغياب والحرمان (Absence Risk)**: يحسب عدد الغيابات المتبقية (`بقي X غيابات`) لكل مقرر حسب الساعات المعتمدة والحد الأقصى للغياب، يرسل تنبيهاً فورياً عند تدهور المستوى، ويعرض شارة الخطر في صفحة الحضور | مفعّلة افتراضياً (`15%`) | عالمياً: `ENABLE_ABSENCE_RISK=1/0` و`DEFAULT_MAX_ABSENCE_PCT=15.0` · للمستخدم: `notify_absence_risk` و`max_absence_pct` في الإعدادات |
| **F7** | **إشعارات المتصفح والتطبيق (Web Push — VAPID + SW v5)**: قناة إشعارات فورية ثانوية للـ PWA بالتوازي مع تيليجرام عبر مفاتيح VAPID وService Worker (`sw.js` v5) | معطّلة افتراضياً (تتطلب مفاتيح VAPID) | عالمياً: `ENABLE_WEB_PUSH=0/1` + `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY` / `VAPID_SUBJECT` · للمستخدم: زر الاشتراك في الإعدادات |
| **F8** | **الرصد والمراقبة التشغيلية (Observability)**: أمر `/status` للمالك، نقطة `/health` المحسّنة، سجلات JSON مهيكلة، وتنبيه تلقائي عند توقّف الفحص (Stale Monitor Alert) | مفعّلة افتراضياً | `LOG_FORMAT=json\|text` · `ENABLE_STALE_ALERT=1/0` · `STALE_MONITOR_SECONDS=3600` |

### تفاصيل استخدام الميزات

#### 1. طبقة Moodle API الهجينة (F1)
- يبقى الـ HTML Scraping هو المسار الأساسي الآمن؛ وعند تفعيل `ENABLE_MOODLE_API=1` أو تفعيل خيار **«استخدام Moodle API الهجين»** (`use_moodle_api`) من إعدادات التطبيق، يحاول `MoodleApiClient` جلب التقويم والواجبات والعلامات عبر واجهة Moodle JSON (`/login/token.php`, `/webservice/rest/server.php`, `/lib/ajax/service.php`) مع مرور كل الطلبات عبر `LMSHttpClient`.
- إذا أعاد الخادم `enablewebservices` أو `invalidtoken` أو `403`/`404`، يُفعَّل قاطع الدائرة مؤقتاً (`3600` ثانية) ويعود النظام فوراً إلى الـ Scraping دون انقطاع.

#### 2. تغذية التقويم الحيّة — Live iCal Feed (F4)
- من صفحة **الإعدادات** داخل التطبيق (أو عبر `/api/calendar-token/rotate`)، فعّل خيار **«تغذية التقويم الحيّة (iCal)»** وانسخ رابط التغذية:
  ```text
  https://<your-app>.onrender.com/api/calendar.ics?token=<your-secret-token>
  ```
- **الإضافة إلى Google Calendar:** افتح Google Calendar على الويب ← من القائمة الجانبية بجانب *Other calendars* اضغط **`+`** ← اختر **From URL** ← الصق رابط التغذية واضغط **Add calendar**.
- **الإضافة إلى Apple Calendar (iPhone / macOS):**
  - على **iPhone / iPad:** الإعدادات ← التقويم ← الحسابات ← إضافة حساب ← أخرى ← **إضافة تقويم مشترك (Add Subscribed Calendar)** ← الصق الرابط.
  - على **macOS:** تطبيق Calendar ← قائمة **File** ← **New Calendar Subscription…** ← الصق الرابط.
- **الأمان والخصوصية:** يُخزَّن التوكن مشفّراً ومُهشّماً بـ `SHA-256` مع مقارنة زمنية ثابتة (`hmac.compare_digest`)، ولا يُسجَّل معامل `?token=` أبداً في سجلات الخادم، وتُرسل التغذية بترويسة `Cache-Control: no-store`. يمكنك تدوير الرابط (`تدوير الرابط`) أو إبطاله فوراً (`إلغاء الرابط`) من الإعدادات في أي وقت.

#### 3. الملخص اليومي الصباحي — Daily Digest (F5)
- يرسل البوت ملخصاً يومياً منظماً بتوقيت `Asia/Amman` يشمل الواجبات والكويزات والتذكيرات الشخصية المستحقة خلال الـ **48 ساعة** القادمة، بالإضافة إلى أي مقرر في حالة تحذير أو خطر غياب.
- يمكنك تشغيله/إيقافه وتحديد وقت الإرسال (`HH:MM`، مثلاً `08:00`) من صفحة **الإعدادات** في التطبيق، أو تعطيله عالمياً عبر `ENABLE_DAILY_DIGEST=0`.

#### 4. رادار خطر الغياب — Absence Risk (F6)
- يحسب النظام عدد الغيابات المسموح بها والمتبقية لكل مقرر بناءً على نسبة الحرمان (`max_absence_pct`، الافتراضي `15%`) وعدد الساعات المعتمدة، ويصنّف الحالة إلى: `آمن (safe)`، `تحذير (warning)`، `خطر (critical)`، أو `حرمان (deprived)`.
- تظهر شارة **«بقي X غيابات»** وحالة الخطر مباشرة على بطاقات المقررات في صفحة **الحضور**، ويرسل البوت تنبيهاً فورياً عند تفاقم مستوى الخطر.
- يمكنك تعطيله من الإعدادات عبر مفتاح `notify_absence_risk` أو عالمياً عبر `ENABLE_ABSENCE_RISK=0`.

#### 5. إشعارات Web Push وإعداد مفاتيح VAPID (F7)
- لتفعيل إشعارات المتصفح/التطبيق (PWA Web Push):
  1. ولّد زوج مفاتيح VAPID (بصيغة URL-safe Base64) باستخدام Python (`py_vapid` المثبّتة مع `pywebpush`):
     ```powershell
     python -c "from cryptography.hazmat.primitives import serialization; from py_vapid import Vapid; from py_vapid.utils import b64urlencode; v = Vapid(); v.generate_keys(); print('VAPID_PUBLIC_KEY=' + b64urlencode(v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))); print('VAPID_PRIVATE_KEY=' + b64urlencode(v.private_key.private_numbers().private_value.to_bytes(32, 'big')))"
     ```
     *(أو عبر الأمر `vapid --gen --applicationServerKey` إن كان متوفراً في بيئتك)*.
  2. أضف المتغيرات التالية إلى `.env` أو لوحة Render:
     ```env
     ENABLE_WEB_PUSH=1
     VAPID_PUBLIC_KEY=<المفتاح العام>
     VAPID_PRIVATE_KEY=<المفتاح الخاص>
     VAPID_SUBJECT=mailto:your-email@example.com
     ```
  3. افتح تطبيق الويب (PWA) ← **الإعدادات** ← اضغط **«تفعيل إشعارات المتصفح (Web Push)»** وامنح إذن الإشعارات.
- عند عدم ضبط مفاتيح VAPID أو عند تعطيل `ENABLE_WEB_PUSH=0`، يُخفى/يُعطَّل خيار Web Push تلقائياً في الواجهة بسلاسة دون أي أخطاء، وعند فصل الجهاز أو تسجيل الخروج تُحذف اشتراكات Push المرتبطة به تلقائياً.
- يتحقق `sw.js` (`v5`) عند النقر على الإشعار (`notificationclick`) من أن الرابط نسبي آمن يبدأ بـ `/` أو رابط `https://` صالح فقط.

#### 6. الرصد والتشخيص — `/status` و`/health` والسجلات المهيكلة (F8)
- **أمر `/status` في تيليجرام (للمالك فقط):** يعرض حالة المحرك، وقت التشغيل (`uptime`)، آخر فحص ناجح، حالة WAF، عدد ومعدل الأخطاء، ومقاييس توفير طلبات Moodle API.
- **نقطة `GET /health`:** تُعيد `200 OK` عند سلامة قاعدة البيانات أو `503 Service Unavailable` عند تعذّرها، وتتضمن حقول التشخيص غير الحساسة (`status`, `db`, `monitor_running`, `last_successful_check_at`, `waf_blocked`, `error_count`, `error_rate`, `uptime_seconds`) دون تسريب أي أسرار أو توكنات.
- **سجلات JSON المهيكلة (`LOG_FORMAT=json`):** يُخرج البوت سجلات JSON قابلة للفهرسة مع تدوير ملفات تلقائي (`RotatingFileHandler` — `5MB × 3`). للتبديل إلى السجلات النصية التقليدية اضبط `LOG_FORMAT=text`.
- **تنبيه توقّف الفحص (Stale Alert):** إذا مرّ أكثر من `STALE_MONITOR_SECONDS` (الافتراضي `3600` ثانية) دون فحص ناجح أثناء تفعيل المراقبة، يرسل النظام تنبيهاً للمالك (يمكن تعطيله عبر `ENABLE_STALE_ALERT=0`).

---

## 🚀 دليل التشغيل المحلي (Run Guide)

### الخطوة 1: إنشاء بوت تيليجرام ومعرفة معرّفك
1. افتح تيليجرام وابحث عن **@BotFather** ← أرسل `/newbot` واحفظ **Token**.
2. احصل على معرّفك الرقمي في تيليجرام (مثلاً عبر `@userinfobot`) لضبطه في `OWNER_TELEGRAM_ID`.

### الخطوة 2: إعداد البيئة وتثبيت المتطلبات
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# تثبيت المتطلبات الأساسية:
pip install -r requirements.txt

# أو تثبيت النسخ المطابقة المثبّتة بدقة (موصى به للإنتاج وCI):
pip install -r requirements.lock

# للتطوير والاختبار (pytest + ruff):
pip install -r requirements-dev.txt
```

### الخطوة 3: إعداد ملف `.env`
انسخ `.env.example` إلى `.env` وولّد مفتاح التشفير الإلزامي:

```powershell
Copy-Item .env.example .env
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

#### مرجع متغيرات البيئة

| المتغير | إلزامي؟ | القيمة الافتراضية | الوصف |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | نعم | — | توكن البوت من BotFather |
| `OWNER_TELEGRAM_ID` | **نعم (Fail-Closed)** | لا يوجد | معرّف تيليجرام الرقمي للمالك (يتوقف الإقلاع إن غاب أو لم يكن رقماً موجباً) |
| `ENCRYPTION_KEY` | **نعم (Fail-Closed)** | لا يوجد | مفتاح Fernet مستقل لتشفير كلمات مرور LMS (`enc:v1:`) |
| `WEBAPP_URL` | في الإنتاج (Render) | — | الرابط العام للخدمة (مثال `https://your-app.onrender.com`) لضبط CORS وMini App وروابط iCal |
| `DATABASE_URL` | في الإنتاج | `""` (SQLite محلي) | رابط PostgreSQL عبر PgBouncer Transaction Pooler (`:6543?pgbouncer=true`) لـ `psycopg_pool` |
| `DIRECT_URL` | موصى به مع Supabase | `""` | رابط PostgreSQL المباشر (`:5432`) لترحيلات `init_db()` عند الإقلاع |
| `ENABLE_MOODLE_API` | اختياري (F1) | `0` | `1` لتفعيل طبقة Moodle API الهجينة عالمياً (`0` = Scraping أساسي) |
| `ENABLE_ICAL_FEED` | اختياري (F4) | `1` | `1` لتفعيل تغذية `/api/calendar.ics`، و`0` لتعطيلها (`404`) |
| `ENABLE_DAILY_DIGEST` | اختياري (F5) | `1` | `1` لتفعيل الملخص الصباحي اليومي |
| `DEFAULT_DIGEST_TIME` | اختياري (F5) | `08:00` | وقت الإرسال الافتراضي للملخص اليومي (`HH:MM` بتوقيت عمّان) |
| `ENABLE_ABSENCE_RISK` | اختياري (F6) | `1` | `1` لتفعيل حساب وتنبيهات خطر الغياب |
| `DEFAULT_MAX_ABSENCE_PCT` | اختياري (F6) | `15.0` | النسبة المئوية الافتراضية القصوى للغياب (`1.0`–`50.0`) |
| `ENABLE_WEB_PUSH` | اختياري (F7) | `0` | `1` لتفعيل إشعارات Web Push (يتطلب `VAPID_PUBLIC_KEY` و`VAPID_PRIVATE_KEY`) |
| `VAPID_PUBLIC_KEY` | مع F7 | `""` | مفتاح VAPID العام (URL-safe Base64) |
| `VAPID_PRIVATE_KEY` | مع F7 | `""` | مفتاح VAPID الخاص (URL-safe Base64) |
| `VAPID_SUBJECT` | مع F7 | `mailto:admin@example.com` | بريد/رابط تعريف المرسل وفق معيار VAPID |
| `LOG_FORMAT` | اختياري (F8) | `json` | صيغة السجلات (`json` أو `text`) |
| `ENABLE_STALE_ALERT` | اختياري (F8) | `1` | `1` لتنبيه المالك عند توقف الفحص لفترة طويلة |
| `STALE_MONITOR_SECONDS` | اختياري (F8) | `3600` | مدة الخمول بالثواني قبل إطلاق تنبيه التوقف |

### الخطوة 4: تشغيل البوت وخادم التطبيق
```powershell
python bot.py
```

---

## 🔄 دليل الترقية والترحيل من الإصدارات القديمة (Upgrade & Migration Guide)

إذا كنت ترقّي من إصدار سابق كان يعتمد على تشفير مشتق من `TELEGRAM_BOT_TOKEN` أو معرّف مالك ثابت في الكود، اتبع الخطوات التالية **بالترتيب** قبل دفع التحديث أو تشغيله:

1. **توليد مفتاح `ENCRYPTION_KEY` جديد ومستقل:**
   ```powershell
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
2. **ضبط `OWNER_TELEGRAM_ID` و`WEBAPP_URL` و`ENCRYPTION_KEY` في بيئة التشغيل (أو لوحة Render) قبل النشر:**
   - يطبّق النظام سياسة **Fail-Closed**؛ إذا غاب `OWNER_TELEGRAM_ID` أو `ENCRYPTION_KEY` سيتوقف الإقلاع عمداً برسالة خطأ واضحة.
3. **الإبقاء على `TELEGRAM_BOT_TOKEN` القديم في أول تشغيل بعد الترقية:**
   - عند أول تشغيل، تنفّذ `init_db()` ترحيلاً تلقائياً آمن التكرار (Idempotent) يفكّ تشفير كلمات المرور القديمة (سواء المشفّرة سابقاً بمفتاح مشتق من `TELEGRAM_BOT_TOKEN` أو النص الصريح) ويعيد تشفيرها بصيغة `enc:v1:` باستخدام `ENCRYPTION_KEY`.
   - أي سجل يتعذّر فك تشفيره يُسجَّل كخطأ دون حذفه أو تعطيل الإقلاع.
   - **بعد اكتمال أول تشغيل ناجح**، يصبح التشفير مستقلاً تماماً عن `TELEGRAM_BOT_TOKEN`، ويمكنك تدوير توكن البوت لاحقاً بأمان تام.
4. **ترحيل الجداول والأعمدة الجديدة تلقائياً:**
   - تضيف `init_db()` تلقائياً الجداول الجديدة (`calendar_tokens`, `push_subscriptions`, `pair_codes`, `app_sessions`) والأعمدة الجديدة (`use_moodle_api`, `enable_ical_feed`, `digest_enabled`, `digest_time`, `last_digest_date`, `notify_absence_risk`, `max_absence_pct`, `enable_web_push`, `credit_hours`, `last_risk_level`) دون فقدان أي بيانات سابقة.
5. **إعداد PostgreSQL (`DATABASE_URL` + `DIRECT_URL`):**
   - اضبط `DATABASE_URL` على رابط PgBouncer Transaction Pooler (`:6543?pgbouncer=true`) لاستخدام `psycopg_pool.ConnectionPool` بأداء عالٍ دون استنزاف الاتصالات (يُعطَّل `prepare_threshold` تلقائياً عند اكتشاف PgBouncer).
   - اضبط `DIRECT_URL` على الاتصال المباشر (`:5432`) لعمليات تهيئة وترحيل الجداول (`init_db()`).
6. **تحديث الحزم والتحقق قبل النشر:**
   ```powershell
   pip install -r requirements.lock
   ruff check .
   node --check sw.js static/js/state.js static/js/dates.js static/js/api.js static/js/render.js static/js/actions.js
   pytest -v
   ```

---

## ☁️ دليل النشر على Render Free + UptimeRobot

1. **ربط المستودع عبر `render.yaml`:**
   - يُنشر المشروع كخدمة `type: web` على خطة `free` مع `healthCheckPath: /health` وأمر تشغيل `python bot.py`.
2. **ضبط متغيرات البيئة في Render Dashboard (قبل أول Deploy):**
   - الإلزامية: `TELEGRAM_BOT_TOKEN`، `OWNER_TELEGRAM_ID`، `ENCRYPTION_KEY`، `WEBAPP_URL` (`https://<your-service>.onrender.com`)، `DATABASE_URL`، `DIRECT_URL`.
   - الاختيارية للميزات: `ENABLE_MOODLE_API`، `ENABLE_ICAL_FEED`، `ENABLE_DAILY_DIGEST`، `DEFAULT_DIGEST_TIME`، `ENABLE_ABSENCE_RISK`، `DEFAULT_MAX_ABSENCE_PCT`، `ENABLE_WEB_PUSH`، `VAPID_PUBLIC_KEY`، `VAPID_PRIVATE_KEY`، `VAPID_SUBJECT`، `LOG_FORMAT`، `ENABLE_STALE_ALERT`، `STALE_MONITOR_SECONDS`.
3. **منع السبات باستخدام UptimeRobot:**
   - تدخل خطة Render Free في السبات بعد **15 دقيقة** من عدم النشاط.
   - أنشئ مراقباً مجانياً في **UptimeRobot** من نوع `HTTP(s)` يفحص الرابط التالي كل **5 دقائق**:
     ```text
     https://<your-service>.onrender.com/health
     ```
   - تُحفظ أكواد الاقتران (`pair_codes`) وجلسات الأجهزة (`app_sessions` بمدة صلاحية 30 يوماً) واشتراكات Web Push وتوكنات iCal داخل قاعدة البيانات حتى لا تضيع عند إعادة تشغيل الحاوية.
4. **ملاحظة حول الدفع إلى GitHub (`DECISIONS.md` D7):**
   - لا تدفع التحديثات (`git push`) إلى المستودع المتصل بـ Render قبل ضبط `ENCRYPTION_KEY` و`OWNER_TELEGRAM_ID` في لوحة Render حتى لا يفشل الإقلاع عمداً.

---

## 📱 أوامر البوت والواجهة

| الأمر / العنصر | الوظيفة |
|---|---|
| `/start` | تشغيل البوت وفتح لوحة التحكم الرئيسية بالأزرار |
| `/app` | توليد رمز اقتران مؤقت لربط الهاتف أو المتصفح بتطبيق الويب (PWA) |
| `/status` | عرض تقرير الحالة والتشخيص الحي للمحرك والأخطاء وWAF ومقاييس API (F8) |
| `📱 لوحة التحكم` | فتح تطبيق Telegram Mini App التفاعلي (المقررات، المهام، العلامات، الحضور، الإعلانات، الملفات، الإعدادات) |

---

## 🛡️ الأمان وحماية البيانات

- **تشفير كلمات المرور (Fernet — `AES-128-CBC` + `HMAC-SHA256`):** تُشفَّر كلمات المرور عبر `cryptography.fernet` ببادئة `enc:v1:` باستخدام `ENCRYPTION_KEY` المستقل بنظام Fail-Closed.
- **حماية جلسات الويب (PWA / Mini App):**
  - تمرير Telegram `initData` حصرياً عبر الترويسة `X-Telegram-Init-Data` مع التحقق من صلاحية `auth_date` (`<= 300s`).
  - جلسات المتصفح عبر كوكي `HttpOnly; Secure; SameSite=Strict` + حماية CSRF (`X-CSRF-Token`) بدون تخزين أي توكن في `localStorage`.
  - ترويسات أمان صارمة (`CSP` بدون `'unsafe-inline'` في `script-src`، و`HSTS`، و`X-Content-Type-Options: nosniff`، و`Referrer-Policy` / `<meta name="referrer" content="no-referrer">`).
  - واجهة أمامية خالية تماماً من معالجات `on*=` المضمنة (تعتمد حصراً على Event Delegation بقوائم سماح صريحة + `escapeHTML` + `safeHttpUrl`).
- **عزل المالك (`owner_guard`):** طبقة حماية عامة في `group=-1` تمنع أي مستخدم غير `OWNER_TELEGRAM_ID` من الوصول للبوت، مع حذف رسائل كلمات المرور فور استلامها في `try/finally`.

---

## 📂 هيكل المشروع وخط التكامل المستمر (CI)

```text
LMS_Monitor/
├── bot.py                     # نقطة الدخول وتهيئة التطبيق ودورة التشغيل
├── config.py                  # الإعدادات، OWNER_TELEGRAM_ID الإلزامي، وأعلام الميزات F1–F8
├── database.py                # قاعدة البيانات (psycopg_pool / SQLite WAL) وجداول iCal وWeb Push
├── security.py                # تشفير Fernet (AES-128-CBC + HMAC-SHA256) ببادئة enc:v1:
├── telegram_utils.py          # تهريب HTML وتقسيم الرسائل وإرسال آمن يحترم RetryAfter
├── monitor.py                 # محرك المراقبة الدورية (APScheduler) + F1 Hybrid API + F6/F7/F8
├── reminder.py                # محرك التذكيرات (Asia/Amman) + F5 Daily Digest + F7
├── handlers/                  # معالجات أوامر وأزرار تيليجرام (بما فيها /status وowner_guard)
├── scraper/                   # LMSHttpClient + المحللات + moodle_api.py (F1)
├── lms_scraper.py             # واجهة التوافق (Facade) للسكرابر
├── services/                  # sync_service, ical_service (F4), digest_service (F5),
│                              # absence_risk (F6), web_push (F7), observability (F8)
├── webapp_server.py           # خادم FastAPI + Uvicorn (REST API, iCal Feed, Web Push, /health)
├── webapp.html                # هيكل HTML خفيف (بدون inline script/style أو on*=)
├── static/
│   ├── css/app.css            # أنماط الواجهة الأمامية
│   └── js/                    # state.js, dates.js, api.js, render.js, actions.js
├── sw.js                      # Service Worker (v5): تخزين مؤقت + Web Push + notificationclick آمن
├── pyproject.toml             # إعدادات Ruff لفحص جودة الكود
├── .github/workflows/ci.yml   # مسار CI: ruff check . + node --check + pytest -v
├── render.yaml                # تهيئة النشر على Render Free مع healthCheckPath: /health
├── requirements.txt           # المتطلبات الأساسية (تشمل pywebpush وFastAPI وAPScheduler وpsycopg_pool)
├── requirements-dev.txt       # متطلبات التطوير والاختبار
├── requirements.lock          # نسخ الحزم المثبّتة بدقة (Lockfile)
└── tests/                     # اختبارات الأمان، المحللات (fixtures/)، الويب، البوت، الواجهة، والميزات F1–F8
```

- **التحقق التلقائي (GitHub Actions CI — `.github/workflows/ci.yml`):** يعمل عند كل `push` و`pull_request` وينفّذ:
  1. `ruff check .` وفق `pyproject.toml`
  2. `node --check` على جميع ملفات `static/js/*.js` و`sw.js`
  3. `pytest -v` مع متغيرات بيئة اختبار آمنة ومفتاح `ENCRYPTION_KEY` مولّد ديناميكياً.
