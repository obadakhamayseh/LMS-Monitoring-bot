import asyncio
import inspect
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

from cryptography.fernet import Fernet

# Configure safe isolated test environment BEFORE importing project modules
os.environ["DATABASE_URL"] = ""
os.environ["DIRECT_URL"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:TEST"
os.environ["OWNER_TELEGRAM_ID"] = "1"
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from telegram.constants import ParseMode
from telegram.error import Forbidden, RetryAfter

import reminder
from reminder import ReminderEngine


# ===========================================================================
# H6: register_assignments must NOT delete reminder on empty/unparseable due_str
# ===========================================================================

def test_h6_register_assignments_preserves_reminder_when_due_str_empty_or_unparseable():
    bot = MagicMock()
    eng = ReminderEngine(bot=bot)

    assignments = [
        {"id": "assign_1", "name": "HW Empty", "url": "https://mulms.mutah.edu.jo/mod/assign/view.php?id=1", "due_date": ""},
        {"id": "assign_2", "name": "HW None", "url": "https://mulms.mutah.edu.jo/mod/assign/view.php?id=2", "due_date": None},
        {"id": "assign_3", "name": "HW Bad", "url": "https://mulms.mutah.edu.jo/mod/assign/view.php?id=3", "due_date": "not a valid date"},
        {"id": "assign_4", "name": "HW Past", "url": "https://mulms.mutah.edu.jo/mod/assign/view.php?id=4", "due_date": "15 January 2020, 11:59 PM"},
        {"id": "assign_5", "name": "HW Future", "url": "https://mulms.mutah.edu.jo/mod/assign/view.php?id=5", "due_date": "15 January 2099, 11:59 PM"},
    ]

    with patch("reminder.db.delete_reminder") as delete_mock, \
         patch("reminder.db.upsert_reminder") as upsert_mock:
        eng.register_assignments(100, 71177, assignments)

    deleted_ids = [call.args[1] for call in delete_mock.call_args_list]
    assert "assign_1" not in deleted_ids, "Empty due_date must NOT delete existing reminder"
    assert "assign_2" not in deleted_ids, "None due_date must NOT delete existing reminder"
    assert "assign_3" not in deleted_ids, "Unparseable due_date must NOT delete existing reminder"
    assert "assign_4" in deleted_ids, "Genuinely past due_date may clean up expired reminder"

    assert upsert_mock.call_count == 1
    assert upsert_mock.call_args.kwargs["content_id"] == "assign_5"


def test_h6_check_reminders_marks_reminded_only_after_send_succeeds():
    async def _run():
        bot = MagicMock()
        eng = ReminderEngine(bot=bot)

        now_ts = int(time.time())
        pending = [{
            "id": 99,
            "telegram_id": 100,
            "course_id": 71177,
            "content_id": "assign_5",
            "content_name": "HW <1>",
            "content_url": "https://mulms.mutah.edu.jo/mod/assign/view.php?id=5",
            "due_ts": now_ts + 1800,
            "due_str": "Today 11:59 PM",
            "reminded_24h": 1,
            "reminded_3h": 1,
            "reminded_1h": 0,
        }]

        # Send fails -> mark_reminded must NOT be called
        with patch("reminder.db.get_pending_reminders", return_value=pending), \
             patch.object(eng, "_send_reminder", new=AsyncMock(return_value=False)), \
             patch("reminder.db.mark_reminded") as mark_mock:
            await eng._check_reminders()
        assert mark_mock.call_count == 0

        # Send succeeds -> mark_reminded IS called
        with patch("reminder.db.get_pending_reminders", return_value=pending), \
             patch.object(eng, "_send_reminder", new=AsyncMock(return_value=True)), \
             patch("reminder.db.mark_reminded") as mark_mock:
            await eng._check_reminders()
        mark_mock.assert_called_once_with(99, "reminded_1h")

    asyncio.run(_run())


# ===========================================================================
# H12: Reminder uses ParseMode.HTML, escapes HTML, handles RetryAfter & Forbidden
# ===========================================================================

def test_h12_reminder_uses_html_escapes_and_handles_retry_after_and_forbidden():
    src = inspect.getsource(reminder)
    assert "ParseMode.MARKDOWN" not in src, "reminder.py must not use deprecated ParseMode.MARKDOWN"

    rem = {
        "id": 10,
        "telegram_id": 100,
        "course_id": 71177,
        "content_name": "Lab <1> & Notes",
        "content_url": "https://mulms.mutah.edu.jo/mod/assign/view.php?id=5&x=1",
        "due_str": "28 Nov 2026 <11:59 PM>",
    }

    async def _run():
        # 1. Sends HTML with properly escaped entities and retries on RetryAfter
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=[RetryAfter(0.01), MagicMock()])
        eng = ReminderEngine(bot=bot)

        with patch("reminder.db.get_course", return_value={"course_name": "CS <101> & Algo"}):
            ok = await eng._send_reminder(100, rem, "1h")

        assert ok is True
        assert bot.send_message.call_count == 2
        sent_kwargs = bot.send_message.call_args.kwargs
        assert sent_kwargs.get("parse_mode") == ParseMode.HTML
        sent_text = sent_kwargs["text"]
        assert "Lab &lt;1&gt; &amp; Notes" in sent_text
        assert "CS &lt;101&gt; &amp; Algo" in sent_text
        assert "Lab <1>" not in sent_text

        # 2. On Forbidden -> calls db.set_user_notifications_enabled(tid, False) and returns False
        bot2 = MagicMock()
        bot2.send_message = AsyncMock(side_effect=Forbidden("bot was blocked"))
        eng2 = ReminderEngine(bot=bot2)

        with patch("reminder.db.get_course", return_value={"course_name": "CS101"}), \
             patch("reminder.db.set_user_notifications_enabled", create=True) as disable_mock:
            ok2 = await eng2._send_reminder(200, rem, "3h")

        assert ok2 is False
        disable_mock.assert_called_once_with(200, False)

    asyncio.run(_run())
