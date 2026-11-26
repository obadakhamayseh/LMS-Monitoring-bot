import os
from dotenv import load_dotenv

load_dotenv()

def _load_owner_telegram_id() -> int:
    """Load and validate the required OWNER_TELEGRAM_ID environment variable."""
    raw = os.getenv("OWNER_TELEGRAM_ID")
    if raw is None or not raw.strip():
        raise RuntimeError(
            "OWNER_TELEGRAM_ID environment variable is required and must be set to your numeric Telegram user ID."
        )
    try:
        owner_id = int(raw.strip())
    except ValueError as exc:
        raise RuntimeError(
            f"OWNER_TELEGRAM_ID must be a valid integer, got {raw!r}."
        ) from exc
    if owner_id <= 0:
        raise RuntimeError(
            f"OWNER_TELEGRAM_ID must be a positive integer, got {owner_id}."
        )
    return owner_id


def _env_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean environment variable with a safe fallback."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    val = raw.strip().lower()
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off"):
        return False
    return default


def _env_int(name: str, default: int) -> int:
    """Parse an integer environment variable with a safe fallback."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    """Parse a float environment variable with a safe fallback."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
OWNER_TELEGRAM_ID: int = _load_owner_telegram_id()

# Feature flags & defaults (all env-driven; zero hardcoded secrets)
ENABLE_MOODLE_API: bool = _env_bool("ENABLE_MOODLE_API", False)
ENABLE_ICAL_FEED: bool = _env_bool("ENABLE_ICAL_FEED", True)
ENABLE_DAILY_DIGEST: bool = _env_bool("ENABLE_DAILY_DIGEST", True)
DEFAULT_DIGEST_TIME: str = (os.environ.get("DEFAULT_DIGEST_TIME", "08:00") or "08:00").strip() or "08:00"
ENABLE_ABSENCE_RISK: bool = _env_bool("ENABLE_ABSENCE_RISK", True)
DEFAULT_MAX_ABSENCE_PCT: float = _env_float("DEFAULT_MAX_ABSENCE_PCT", 15.0)
VAPID_PUBLIC_KEY: str = os.environ.get("VAPID_PUBLIC_KEY", "").strip()
VAPID_PRIVATE_KEY: str = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
VAPID_SUBJECT: str = (
    os.environ.get("VAPID_SUBJECT", "mailto:admin@example.com") or "mailto:admin@example.com"
).strip() or "mailto:admin@example.com"
ENABLE_WEB_PUSH: bool = _env_bool("ENABLE_WEB_PUSH", False)
LOG_FORMAT: str = (os.environ.get("LOG_FORMAT", "json") or "json").strip().lower() or "json"
ENABLE_STALE_ALERT: bool = _env_bool("ENABLE_STALE_ALERT", True)
STALE_MONITOR_SECONDS: int = _env_int("STALE_MONITOR_SECONDS", 3600)

LMS_BASE_URL = "https://mulms.mutah.edu.jo"
LMS_LOGIN_URL = f"{LMS_BASE_URL}/login/index.php"
LMS_MY_COURSES_URL = f"{LMS_BASE_URL}/my/courses.php"
LMS_API_URL = f"{LMS_BASE_URL}/webservice/rest/server.php"

DATABASE_PATH = "lms_monitor.db"
DATABASE_URL = os.getenv("DATABASE_URL", "")
DIRECT_URL = os.getenv("DIRECT_URL", "")
CHECK_INTERVAL_MINUTES = 10

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
