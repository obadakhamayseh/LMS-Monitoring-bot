**الافتراضات (Assumptions) — أول سطر إلزامي:** (1) النظام شخصي لمالك واحد (`OWNER_TELEGRAM_ID`)؛ (2) Moodle Mobile API غير معروف التوفّر ⇐ الخيار الأكثر أماناً: يبقى Scraping هو المسار الأساسي، وتُبنى طبقة API-First (F1) خلف مفتاح تشغيل **معطّل افتراضياً** مع اكتشاف تلقائي وFallback؛ (3) الاستضافة Render Free (نوع `web`) + UptimeRobot ⇐ نُبقي `web` مع `healthCheckPath: /health` ونوثّق الـ ping، ونحفظ `_pair_codes` في DB؛ (4) المنطقة الزمنية `Asia/Amman`؛ (5) لا يُدفع (`git push`) شيء إلى GitHub/Render قبل أن يضبط المالك `ENCRYPTION_KEY` و`OWNER_TELEGRAM_ID` في Render (وإلا سيفشل الإقلاع عمداً — Fail-Closed).

# DECISIONS.md — سجل القرارات المعمارية (Mutah LMS Monitor)

| # | القرار | الخيار المعتمد | السبب / الأثر |
|---|---|---|---|
| D1 | نطاق المستخدمين | شخصي لمالك واحد (`OWNER_TELEGRAM_ID` إلزامي) | إزالة المعرّف القديم الثابت من الكود وتطبيق Fail-Closed عند غياب المتغير. |
| D2 | Moodle Mobile API | غير معروف التوفّر ⇐ Scraping أساسي + F1 خلف `ENABLE_MOODLE_API=0` | عدم الاعتماد على نقطة نهاية قد تكون معطّلة في خادم الجامعة، مع جاهزية التحويل لاحقاً. |
| D3 | الاستضافة | Render Free (`type: web`) + UptimeRobot | إبقاء خادم HTTP لصحة `/health` ومنع السبات، وتخزين أكواد الاقتران في قاعدة البيانات بدل الذاكرة (P1). |
| D4 | المنطقة الزمنية | `Asia/Amman` عبر `zoneinfo.ZoneInfo` + `tzdata` | إلغاء الإزاحة الثابتة `+03:00` ودعم التوقيت الصيفي/الشتوي على Windows وLinux. |
| D5 | تشفير كلمات المرور | مفتاح `ENCRYPTION_KEY` مستقل (Fernet) ببادئة `enc:v1:` | منع اشتقاق مفتاح التشفير من `TELEGRAM_BOT_TOKEN`، مع ترحيل تلقائي آمن (Idempotent) للبيانات القديمة عند الإقلاع. |
| D6 | توثيق Mini App (`initData`) | عبر الترويسة `X-Telegram-Init-Data` فقط + فحص `auth_date` (`<= 300s`) | منع تسرب `initData` في سجلات URL/History/Referer ومنع Replay Attacks. |
| D7 | النشر والـ Git | Commit محلي مستقل لكل مرحلة (P0 → P1 → P2 → الميزات) دون `git push` | حماية بيئة Render من التوقف حتى يضبط المالك `ENCRYPTION_KEY` و`OWNER_TELEGRAM_ID` في لوحة Render. |
