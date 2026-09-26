import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

LMS_BASE_URL = "https://mulms.mutah.edu.jo"
LMS_LOGIN_URL = f"{LMS_BASE_URL}/login/index.php"
LMS_MY_COURSES_URL = f"{LMS_BASE_URL}/my/courses.php"
LMS_API_URL = f"{LMS_BASE_URL}/webservice/rest/server.php"

DATABASE_PATH = "lms_monitor.db"
DATABASE_URL = os.getenv("DATABASE_URL", "")
DIRECT_URL = os.getenv("DIRECT_URL", "")
LMS_PROXY = os.getenv("LMS_PROXY", "")

CHECK_INTERVAL_MINUTES = 5

MESSAGES = {
    "welcome": (
        "🎓 *أهلاً بك في بوت مراقب LMS!*\n\n"
        "هذا البوت يراقب نظام التعليم الإلكتروني ويُنبّهك فور نزول:\n"
        "📚 واجبات جديدة\n"
        "📝 كويزات جديدة\n"
        "📢 إعلانات جديدة\n\n"
        "للبدء، أرسل بيانات تسجيل الدخول الخاصة بك باستخدام:\n"
        "`/login <رقم الطالب> <كلمة المرور>`\n\n"
        "مثال:\n"
        "`/login 20240000 123456`"
    ),
    "login_success": "✅ *تم تسجيل الدخول بنجاح!*\n\nاستخدم /courses لعرض موادك وبدء المراقبة.",
    "login_failed": "❌ *فشل تسجيل الدخول!*\n\nتأكد من رقم الطالب وكلمة المرور وحاول مجدداً.",
    "not_logged_in": "⚠️ *لم تقم بتسجيل الدخول بعد!*\n\nاستخدم `/login <رقم الطالب> <كلمة المرور>` أولاً.",
    "monitoring_started": "🟢 *بدأت المراقبة!*\n\nسيتم التحقق كل {} دقيقة وإشعارك بأي تغييرات.",
    "monitoring_stopped": "🔴 *توقفت المراقبة.*",
    "new_assignment": (
        "📚 *واجب جديد!*\n\n"
        "📖 المادة: *{}*\n"
        "📝 الواجب: *{}*\n"
        "⏰ الموعد النهائي: *{}*\n"
        "🔗 [فتح الواجب]({})"
    ),
    "new_quiz": (
        "📝 *كويز جديد!*\n\n"
        "📖 المادة: *{}*\n"
        "❓ الكويز: *{}*\n"
        "⏰ متاح حتى: *{}*\n"
        "🔗 [فتح الكويز]({})"
    ),
    "new_resource": (
        "📎 *ملف/مورد جديد!*\n\n"
        "📖 المادة: *{}*\n"
        "📁 الاسم: *{}*\n"
        "🔗 [فتح]({})"
    ),
}
