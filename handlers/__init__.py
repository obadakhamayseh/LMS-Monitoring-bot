"""Telegram bot handlers package — registration entrypoint."""

from __future__ import annotations

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from handlers.auth import (
    cancel_conv,
    cmd_app,
    cmd_logout,
    cmd_pair,
    cmd_start,
    cmd_unlink,
    handle_text,
    login_got_pass,
    login_got_user,
    login_start,
)
from handlers.callbacks import fallback_callback, handle_callback
from handlers.common import (
    STATE_ADD_COURSE,
    STATE_LOGIN_PASS,
    STATE_LOGIN_USER,
    error_handler,
    owner_guard,
)
from handlers.courses import add_course_got_id, add_course_start, cmd_courses
from handlers.views import cmd_help


def register_handlers(app: Application) -> None:
    """Register owner guard (group=-1), commands, conversations, callbacks, and error handler."""
    app.add_handler(TypeHandler(Update, owner_guard), group=-1)

    conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(login_start, pattern="^login_start$"),
            CallbackQueryHandler(add_course_start, pattern="^add_course_start$"),
        ],
        states={
            STATE_LOGIN_USER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_got_user)
            ],
            STATE_LOGIN_PASS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, login_got_pass)
            ],
            STATE_ADD_COURSE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_course_got_id)
            ],
        },
        fallbacks=[
            CommandHandler("start", cmd_start),
            CallbackQueryHandler(login_start, pattern="^login_start$"),
            CallbackQueryHandler(add_course_start, pattern="^add_course_start$"),
            CallbackQueryHandler(cancel_conv, pattern="^cancel_conv$"),
            CallbackQueryHandler(fallback_callback),
        ],
        allow_reentry=True,
        per_message=False,
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("courses", cmd_courses))
    app.add_handler(CommandHandler("logout", cmd_logout))
    app.add_handler(CommandHandler("app", cmd_app))
    app.add_handler(CommandHandler("pair", cmd_pair))
    app.add_handler(CommandHandler("unlink", cmd_unlink))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)


__all__ = ["register_handlers"]
