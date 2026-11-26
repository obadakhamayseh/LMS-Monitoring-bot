import asyncio
import functools
import io
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import Forbidden, RetryAfter, TelegramError

from config import LMS_BASE_URL
import database as db
from lms_scraper import FetchResult, LMSScraper
from reminder import _parse_due_date
from telegram_utils import escape_html, html_link, send_html_message

logger = logging.getLogger(__name__)


def _canonical_content_url(url: str) -> str:
    """Normalize volatile Moodle link parameters before checking for updates."""
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        query = sorted(
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key.casefold() != "__ncforminfo"
        )
        return urlunsplit((
            parts.scheme.casefold(),
            parts.netloc.casefold(),
            parts.path,
            urlencode(query, doseq=True),
            "",
        ))
    except ValueError:
        return url.strip()


class MonitorEngine:

    MAX_AUTO_DOWNLOAD_COUNT = 3
    MAX_AUTO_DOWNLOAD_MB = 20
    INCOMPLETE_BACKOFF_BASE = 120   # 2 minutes initial exponential backoff on incomplete scans
    INCOMPLETE_BACKOFF_MAX = 1800   # 30 minutes cap on exponential backoff
    MIN_REQUEST_COOLDOWN_SECONDS = 1.0

    def __init__(self, bot: Bot, reminder_engine=None):
        self.bot = bot
        self.reminder_engine = reminder_engine
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._state_lock = threading.RLock()
        self._active_tasks: set[asyncio.Task] = set()
        self._sessions = {}
        self._user_locks = {}
        self._check_slots = asyncio.Semaphore(5)
        self._auth_required = set()
        self._auth_alerted = set()
        self._waf_backoff_until = {}  # tid -> monotonic timestamp
        self._transient_backoff_until = {}  # tid -> monotonic timestamp (timeouts/503/incomplete)
        self._incomplete_failures = {}  # tid -> consecutive complete=False count
        self._last_scan_waf = {}  # tid -> bool from latest FetchResult scan
        self._last_scan_expired = {}  # tid -> bool from latest FetchResult scan
        self._course_cache = {}  # (tid, course_id) -> (timestamp, content)
        self.COURSE_CACHE_TTL = 300  # 5 minutes shared cache TTL
        self.WAF_BACKOFF_SECONDS = 900  # 15 minutes polite cooldown if WAF triggers
        self.TRANSIENT_BACKOFF_SECONDS = 300  # 5 minutes retry backoff on temporary login errors

    # ------------------------------------------------------------------
    # H4 + H13: Thread-safe public lifecycle & session management API
    # ------------------------------------------------------------------

    def attach_loop(self, loop: Optional[asyncio.AbstractEventLoop]) -> None:
        """Attach the asyncio event loop used for cross-thread dispatch."""
        with self._state_lock:
            self._loop = loop

    def _track_task(self, coro) -> asyncio.Task:
        """Create and track an in-flight asyncio task so stop() awaits it before closing resources."""
        task = asyncio.create_task(coro)
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)
        return task

    async def _run_db(self, fn, *args, **kwargs):
        """Run a blocking database call in the default thread executor."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))

    def _clear_user_state_locked(self, tid: int) -> None:
        """Clear all per-user auth/backoff/cache state (caller must hold self._state_lock)."""
        self._auth_required.discard(tid)
        self._auth_alerted.discard(tid)
        self._waf_backoff_until.pop(tid, None)
        self._transient_backoff_until.pop(tid, None)
        self._incomplete_failures.pop(tid, None)
        self._last_scan_waf.pop(tid, None)
        self._last_scan_expired.pop(tid, None)
        for key in [k for k in self._course_cache if isinstance(k, tuple) and k[0] == tid]:
            self._course_cache.pop(key, None)

    @staticmethod
    def _close_scraper_sync(scraper: LMSScraper, logout: bool = False) -> None:
        if logout:
            try:
                scraper.logout()
                return
            except Exception as exc:
                logger.debug(f"Scraper logout error during cleanup: {exc}")
        try:
            scraper.close()
        except Exception as exc:
            logger.debug(f"Scraper close error during cleanup: {exc}")

    async def _discard_session_async(self, tid: int, logout: bool = False) -> None:
        with self._state_lock:
            current = self._sessions.pop(int(tid), None)
        if current:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._close_scraper_sync, current[2], logout)

    def _discard_session(self, tid: int, logout: bool = False) -> None:
        """Synchronous helper (safe when called outside an event loop or from fallback lock)."""
        with self._state_lock:
            current = self._sessions.pop(int(tid), None)
        if current:
            self._close_scraper_sync(current[2], logout=logout)

    async def invalidate_user_session_async(self, telegram_id: int, logout: bool = True) -> None:
        """Async locked session invalidation: discards scraper (via executor) and clears user state."""
        tid = int(telegram_id)
        lock = self._user_locks.setdefault(tid, asyncio.Lock())
        async with lock:
            with self._state_lock:
                self._clear_user_state_locked(tid)
            await self._discard_session_async(tid, logout=logout)

    def invalidate_user_session(
        self, telegram_id: int, logout: bool = True, timeout: float = 10.0
    ) -> None:
        """Thread-safe public API to invalidate a user's LMS session and clear auth/backoff/cache state.

        Dispatches onto the attached asyncio event loop via run_coroutine_threadsafe when running,
        or falls back to self._state_lock when no event loop is active.
        """
        tid = int(telegram_id)
        with self._state_lock:
            loop = self._loop

        if loop is not None and loop.is_running():
            try:
                current_loop = asyncio.get_running_loop()
            except RuntimeError:
                current_loop = None

            if current_loop is loop:
                self._track_task(self.invalidate_user_session_async(tid, logout=logout))
                return

            fut = asyncio.run_coroutine_threadsafe(
                self.invalidate_user_session_async(tid, logout=logout), loop
            )
            fut.result(timeout=timeout)
            return

        # Fallback when no running event loop is attached
        with self._state_lock:
            current = self._sessions.pop(tid, None)
            self._clear_user_state_locked(tid)
        if current:
            self._close_scraper_sync(current[2], logout=logout)

    def reset_user_backoff(self, telegram_id: int) -> None:
        """Thread-safe public API to clear auth flags and retry/WAF backoffs for a user."""
        tid = int(telegram_id)
        with self._state_lock:
            self._auth_required.discard(tid)
            self._auth_alerted.discard(tid)
            self._waf_backoff_until.pop(tid, None)
            self._transient_backoff_until.pop(tid, None)
            self._incomplete_failures.pop(tid, None)

    async def start(self):
        if self._running:
            return
        self.attach_loop(asyncio.get_running_loop())
        self._running = True
        self._task = asyncio.create_task(self._loop_run())
        logger.info("Monitor engine started")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        # H13: Await ALL in-flight tasks BEFORE closing any scraper sessions/resources
        in_flight = [t for t in list(self._active_tasks) if not t.done()]
        if in_flight:
            await asyncio.gather(*in_flight, return_exceptions=True)

        with self._state_lock:
            remaining = list(self._sessions.values())
            self._sessions.clear()

        if remaining:
            loop = asyncio.get_running_loop()
            await asyncio.gather(
                *(loop.run_in_executor(None, self._close_scraper_sync, scraper, False) for _, _, scraper in remaining),
                return_exceptions=True,
            )
        logger.info("Monitor engine stopped")

    async def _loop_run(self):
        await self._loop_body()

    async def _loop(self):
        """Backward-compatible alias for the main monitor loop."""
        await self._loop_body()

    async def _loop_body(self):
        while self._running:
            try:
                users = await self._run_db(db.get_all_monitoring_users)
                active_ids = {int(user["telegram_id"]) for user in users}
                with self._state_lock:
                    stale_ids = set(self._sessions) - active_ids
                for tid in stale_ids:
                    lock = self._user_locks.setdefault(tid, asyncio.Lock())
                    async with lock:
                        await self._discard_session_async(tid, logout=False)
                        with self._state_lock:
                            self._auth_required.discard(tid)
                            self._auth_alerted.discard(tid)
                tasks = [self._track_task(self._safe_check_user(user)) for user in users]
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Loop error: {e}")
            await asyncio.sleep(60)

    async def _safe_check_user(self, user: dict):
        async with self._check_slots:
            try:
                await self._check_user(user)
            except Exception as e:
                logger.error(f"Error checking user {user['telegram_id']}: {e}")

    @staticmethod
    def _is_user_due(user: dict) -> bool:
        """Determine whether a user's scheduled monitoring interval has elapsed."""
        last_check = user.get("last_check")
        if not last_check:
            return True
        try:
            if isinstance(last_check, datetime):
                last_dt = last_check
            else:
                lc_str = str(last_check).strip()
                if not lc_str:
                    return True
                try:
                    last_dt = datetime.fromisoformat(lc_str.replace("Z", "+00:00"))
                except ValueError:
                    last_dt = datetime.strptime(lc_str[:19], "%Y-%m-%d %H:%M:%S")
            if last_dt.tzinfo is not None:
                elapsed = (datetime.now(timezone.utc) - last_dt.astimezone(timezone.utc)).total_seconds()
            else:
                elapsed = (datetime.now() - last_dt).total_seconds()
            interval_min = max(1, int(user.get("check_interval", 10)))
            return elapsed >= interval_min * 60
        except (TypeError, ValueError):
            return True

    async def _respect_request_cooldown(self, scraper: LMSScraper) -> None:
        """Respect LMSHttpClient / scraper.last_request_at cooldown before issuing a new request."""
        if self.MIN_REQUEST_COOLDOWN_SECONDS <= 0:
            return
        last_req = getattr(scraper, "last_request_at", 0.0)
        if isinstance(last_req, (int, float)) and last_req > 0:
            elapsed = time.monotonic() - last_req
            remaining = self.MIN_REQUEST_COOLDOWN_SECONDS - elapsed
            if remaining > 0:
                await asyncio.sleep(remaining)

    async def _check_user(self, user: dict):
        tid = int(user["telegram_id"])
        lock = self._user_locks.setdefault(tid, asyncio.Lock())
        async with lock:
            courses = await self._run_db(db.get_user_courses, tid, True)
            if not courses:
                await self._discard_session_async(tid, logout=False)
                with self._state_lock:
                    self._auth_required.discard(tid)
                    self._auth_alerted.discard(tid)
                await self._run_db(db.update_last_check, tid)
                return

            with self._state_lock:
                auth_needed = tid in self._auth_required
                waf_until = self._waf_backoff_until.get(tid, 0.0)
                transient_until = self._transient_backoff_until.get(tid, 0.0)

            if auth_needed:
                await self._notify_auth_stopped(tid)
                return

            now_mono = time.monotonic()
            if now_mono < waf_until or now_mono < transient_until:
                return

            # H7: Check whether the user is due BEFORE calling _ensure_logged_in (and no unconditional 120s keepalive!)
            if not self._is_user_due(user):
                return

            scraper = self._get_session(user)
            loop = asyncio.get_running_loop()

            async def _ensure_logged_in(force_fresh: bool = False):
                nonlocal scraper
                if force_fresh:
                    await self._discard_session_async(tid, logout=False)
                    scraper = self._get_session(user)
                if scraper.logged_in:
                    return True
                await self._respect_request_cooldown(scraper)
                ok_login = await loop.run_in_executor(None, scraper.login)
                if ok_login:
                    with self._state_lock:
                        self._auth_alerted.discard(tid)
                        self._transient_backoff_until.pop(tid, None)
                    return True
                if scraper.waf_blocked:
                    logger.warning(f"WAF challenge during login for {tid}; pausing checks for {self.WAF_BACKOFF_SECONDS}s")
                    with self._state_lock:
                        self._waf_backoff_until[tid] = time.monotonic() + self.WAF_BACKOFF_SECONDS
                    return False
                if scraper.credentials_invalid:
                    logger.warning(f"Invalid credentials for {tid}: {scraper.error_message}")
                    with self._state_lock:
                        self._auth_required.add(tid)
                    await self._notify_auth_stopped(
                        tid,
                        "بيانات تسجيل الدخول إلى نظام الجامعة لم تعد صالحة. أعد تسجيل الدخول من الزر أدناه؛ موادك المراقبة ستبقى محفوظة.",
                    )
                    return False
                # Transient server/network error (e.g. university Moodle restart/maintenance/timeout)
                logger.warning(f"Transient login failure for {tid}: {scraper.error_message}; retrying in {self.TRANSIENT_BACKOFF_SECONDS}s")
                with self._state_lock:
                    self._transient_backoff_until[tid] = time.monotonic() + self.TRANSIENT_BACKOFF_SECONDS
                return False

            if not await _ensure_logged_in():
                return

            await self._respect_request_cooldown(scraper)
            logger.info(f"Checking user {tid}")
            count, complete = await self._scan_user(user, courses, scraper)
            if self._last_scan_waf.get(tid) or scraper.waf_blocked:
                logger.warning(f"WAF challenge during scan for {tid}; pausing checks for {self.WAF_BACKOFF_SECONDS}s")
                with self._state_lock:
                    self._waf_backoff_until[tid] = time.monotonic() + self.WAF_BACKOFF_SECONDS
                return
            if self._last_scan_expired.get(tid) or scraper.session_expired:
                logger.info(f"Session expired during scan for {tid}; auto-relogging in and retrying once")
                if not await _ensure_logged_in(force_fresh=True):
                    return
                await self._respect_request_cooldown(scraper)
                count, complete = await self._scan_user(user, courses, scraper)
                if self._last_scan_waf.get(tid) or scraper.waf_blocked:
                    with self._state_lock:
                        self._waf_backoff_until[tid] = time.monotonic() + self.WAF_BACKOFF_SECONDS
                    return
                if self._last_scan_expired.get(tid) or scraper.session_expired:
                    with self._state_lock:
                        self._transient_backoff_until[tid] = time.monotonic() + self.TRANSIENT_BACKOFF_SECONDS
                    return
            if complete:
                with self._state_lock:
                    self._incomplete_failures.pop(tid, None)
                    self._transient_backoff_until.pop(tid, None)
                await self._run_db(db.update_last_check, tid)
                try:
                    await self._run_db(
                        db.append_sync_log,
                        tid,
                        "فحص دوري ناجح",
                        f"تم فحص {len(courses)} مقررات" + (f" · {count} تحديث جديد" if count > 0 else " · لا تحديثات جديدة"),
                    )
                except Exception:
                    pass
            else:
                # H7: Exponential backoff on complete=False (capped, resets on success)
                with self._state_lock:
                    failures = self._incomplete_failures.get(tid, 0) + 1
                    self._incomplete_failures[tid] = failures
                    delay = min(
                        self.INCOMPLETE_BACKOFF_BASE * (2 ** (failures - 1)),
                        self.INCOMPLETE_BACKOFF_MAX,
                    )
                    self._transient_backoff_until[tid] = time.monotonic() + delay
                logger.warning(
                    f"Scan incomplete for {tid} (consecutive={failures}); backing off for {delay}s"
                )

    def _get_session(self, user: dict) -> LMSScraper:
        tid = int(user["telegram_id"])
        username = user["lms_username"]
        password = user["lms_password"]
        with self._state_lock:
            current = self._sessions.get(tid)
            if current and current[0] == username and current[1] == password:
                return current[2]
            old_scraper = current[2] if current else None
            scraper = LMSScraper(username, password)
            self._sessions[tid] = (username, password, scraper)
        if old_scraper is not None:
            self._close_scraper_sync(old_scraper, logout=False)
        return scraper

    async def resume_user(self, telegram_id: int, scraper: LMSScraper = None):
        """Resume monitoring with the fresh LMS session created by the login flow."""
        tid = int(telegram_id)
        lock = self._user_locks.setdefault(tid, asyncio.Lock())
        async with lock:
            await self._discard_session_async(tid, logout=False)
            with self._state_lock:
                self._clear_user_state_locked(tid)
                if scraper:
                    self._sessions[tid] = (scraper.username, scraper.password, scraper)

    async def stop_user(self, telegram_id: int):
        """Clean up active scraper session and cache when a user logs out."""
        await self.invalidate_user_session_async(telegram_id, logout=True)

    async def _notify_auth_stopped(self, telegram_id: int, message: str = None):
        tid = int(telegram_id)
        with self._state_lock:
            if tid in self._auth_alerted:
                return True
        text = message or (
            "⚠️ انتهت جلسة نظام الجامعة وتوقفت مراقبة المواد مؤقتاً.\n\n"
            "أعد تسجيل الدخول من الزر أدناه. ستبقى المواد المراقبة وإعداداتها محفوظة، "
            "وسيتابع البوت الفحص بعد نجاح الدخول."
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔑 إعادة تسجيل الدخول", callback_data="login_start")
        ]])
        sent = await self._send(tid, text, keyboard=keyboard, plain=True)
        if sent:
            with self._state_lock:
                self._auth_alerted.add(tid)
        return sent

    @staticmethod
    def _is_auto_downloadable_resource(event: dict) -> bool:
        """Allow automatic file download ONLY for Moodle /mod/resource/ files on the LMS host.

        Explicitly excludes /mod/url/ (SSRF/redirect risk) and non-file modules (/mod/page/, /mod/folder/, etc.).
        """
        if not event.get("is_new") or event.get("content_type") != "resource":
            return False
        url = (event.get("url") or "").strip()
        if not url:
            return False
        parts = urlsplit(url)
        allowed_host = urlsplit(LMS_BASE_URL).netloc.lower()
        if parts.scheme.lower() != "https" or parts.netloc.lower() != allowed_host:
            return False
        path_low = parts.path.lower()
        if "/mod/url/" in path_low:
            return False
        return "/mod/resource/" in path_low

    async def _send_document_html(
        self, tid: int, filename: str, file_bytes: bytes, caption_html: str
    ) -> bool:
        """Send a document with HTML caption, respecting RetryAfter and Forbidden."""
        for attempt in range(4):
            try:
                await self.bot.send_document(
                    chat_id=tid,
                    document=io.BytesIO(file_bytes),
                    filename=filename,
                    caption=caption_html,
                    parse_mode=ParseMode.HTML,
                )
                return True
            except RetryAfter as exc:
                if attempt >= 3:
                    return False
                raw_retry = exc.retry_after
                delay = (raw_retry.total_seconds() if hasattr(raw_retry, "total_seconds") else float(raw_retry)) + 0.5
                await asyncio.sleep(delay)
            except Forbidden:
                await self._on_forbidden(tid)
                return False
            except TelegramError as exc:
                logger.warning(f"Document HTML send failed for {tid}; retrying plain: {exc}")
                try:
                    await self.bot.send_document(
                        chat_id=tid,
                        document=io.BytesIO(file_bytes),
                        filename=filename,
                        caption=caption_html,
                        parse_mode=None,
                    )
                    return True
                except Forbidden:
                    await self._on_forbidden(tid)
                    return False
                except Exception:
                    return False
        return False

    async def _scan_user(self, user: dict, courses: list, scraper: LMSScraper, bypass_cache: bool = False):
        tid = int(user["telegram_id"])
        sent_count = 0
        complete = True
        events = []
        self._last_scan_waf[tid] = False
        self._last_scan_expired[tid] = False

        # Scan courses strictly sequentially per user (thread-safe on LMSScraper and polite to NetScaler WAF)
        for course in courses:
            if self._last_scan_waf[tid] or self._last_scan_expired[tid] or scraper.session_expired or scraper.waf_blocked:
                complete = False
                break
            try:
                course_events, course_complete, sess_expired, waf_blocked = await self._check_course_result(
                    tid, user, scraper, course, bypass_cache=bypass_cache
                )
            except Exception as e:
                logger.error(f"Error checking course {course.get('course_id')} for {tid}: {e}")
                course_events, course_complete, sess_expired, waf_blocked = [], False, False, False

            if waf_blocked:
                self._last_scan_waf[tid] = True
                complete = False
                break
            if sess_expired:
                self._last_scan_expired[tid] = True
                complete = False
                break
            events.extend(course_events)
            complete = complete and course_complete

        loop = asyncio.get_running_loop()
        for chunk_events, message in self._build_change_chunks(events):
            # H6: NEVER persist notified/seen state before _send returns True!
            sent = await self._send(tid, message, plain=True)
            if sent:
                for event in chunk_events:
                    if event["is_new"]:
                        await self._run_db(self._mark_new_seen, event)
                    else:
                        await self._run_db(self._save_change, event)
                sent_count += len(chunk_events)

                # Direct document dispatch ONLY for newly added /mod/resource/ files (bounded count & size)
                if user.get("notify_resource", 1):
                    new_resources = [e for e in chunk_events if self._is_auto_downloadable_resource(e)]
                    for res_ev in new_resources[:self.MAX_AUTO_DOWNLOAD_COUNT]:
                        try:
                            course_ref = f"{LMS_BASE_URL}/course/view.php?id={res_ev['course_id']}"
                            file_info = await loop.run_in_executor(
                                None,
                                lambda u=res_ev["url"], ru=res_ev.get("raw_url"), ref=course_ref: scraper.download_resource(
                                    u,
                                    max_mb=self.MAX_AUTO_DOWNLOAD_MB,
                                    raw_url=ru,
                                    referer=ref,
                                ),
                            )
                            if file_info:
                                filename, file_bytes = file_info
                                caption_html = (
                                    f"📎 <b>ملف جديد:</b> {escape_html(res_ev['name'])}\n"
                                    f"📖 <b>المادة:</b> {escape_html(res_ev['course_name'])}"
                                )
                                doc_ok = await self._send_document_html(tid, filename, file_bytes, caption_html)
                                if doc_ok:
                                    logger.info(f"Direct file delivered: {filename} to {tid}")
                                    await asyncio.sleep(0.5)
                        except Exception as dl_err:
                            logger.warning(f"Direct file send error for {tid}: {dl_err}")
            else:
                complete = False
        return sent_count, complete

    async def _is_course_seeded(self, tid: int, cid: int) -> bool:
        fn = getattr(db, "is_course_seeded", None)
        if not callable(fn):
            return False
        try:
            return bool(await self._run_db(fn, tid, cid))
        except Exception as exc:
            logger.debug(f"is_course_seeded fallback for ({tid}, {cid}): {exc}")
            return False

    async def _mark_course_seeded(self, tid: int, cid: int) -> None:
        fn = getattr(db, "mark_course_seeded", None)
        if not callable(fn):
            return
        try:
            await self._run_db(fn, tid, cid)
        except Exception as exc:
            logger.debug(f"mark_course_seeded skipped for ({tid}, {cid}): {exc}")

    async def _check_course(self, tid, user, scraper, course, bypass_cache: bool = False):
        events, ok, _, _ = await self._check_course_result(
            tid, user, scraper, course, bypass_cache=bypass_cache
        )
        return events, ok

    async def _check_course_result(self, tid, user, scraper, course, bypass_cache: bool = False):
        loop = asyncio.get_running_loop()
        cid = course["course_id"]
        cname = course["course_name"]

        now = time.monotonic()
        content = None
        cache_key = (tid, cid)

        if not bypass_cache:
            with self._state_lock:
                cached = self._course_cache.get(cache_key)
            if cached and (now - cached[0] < self.COURSE_CACHE_TTL):
                content = cached[1]
                logger.debug(f"Using course cache for course {cid} (user {tid})")

        if content is None:
            fetch_res = await loop.run_in_executor(
                None, scraper.fetch_course_content, cid
            )
            if isinstance(fetch_res, FetchResult):
                if fetch_res.waf_blocked:
                    logger.warning(f"WAF blocked LMS fetch for user {tid}, course {cid}: {fetch_res.error}")
                    return [], False, False, True
                if fetch_res.session_expired:
                    logger.info(f"Session expired during LMS fetch for user {tid}, course {cid}")
                    return [], False, True, False
                if not fetch_res.ok:
                    logger.warning(f"Incomplete LMS content response for user {tid}, course {cid}: {fetch_res.error}")
                    return [], False, False, False
                content = fetch_res.content
            else:
                # Defensive fallback if an external caller passed a mock returning a raw dict
                if not getattr(scraper, "last_fetch_ok", True):
                    logger.warning(f"Incomplete LMS content response for user {tid}, course {cid}")
                    return [], False, bool(getattr(scraper, "session_expired", False)), bool(getattr(scraper, "waf_blocked", False))
                content = fetch_res
            with self._state_lock:
                self._course_cache[cache_key] = (now, content)

        # H6: Use explicit courses.is_seeded instead of len(existing_*) == 0
        course_seeded = await self._is_course_seeded(tid, cid)
        is_first_seed = not course_seeded
        all_sends_ok = True

        assignments = content.get("assignments", [])
        quizzes = content.get("quizzes", [])
        if self.reminder_engine:
            await self._run_db(self.reminder_engine.register_assignments, tid, cid, assignments + quizzes)

        # Auto-detect solved/completed assignments & quizzes from LMS and update DB
        now_ts = int(time.time())
        for task_item in assignments + quizzes:
            if task_item.get("is_submitted"):
                t_type = task_item.get("type", "assign")
                existing_seen = await self._run_db(db.get_seen_content, tid, task_item["id"], t_type)
                was_previously_unsubmitted = existing_seen is not None and not bool(existing_seen.get("is_submitted", 0))
                if was_previously_unsubmitted:
                    # Only notify for recent/active tasks (do not spam retroactive alerts for tasks expired > 48h ago)
                    due_ts = _parse_due_date(task_item.get("due_date") or (existing_seen.get("due_date") if existing_seen else ""))
                    if due_ts and due_ts < now_ts - 172800:
                        await self._run_db(db.mark_assignment_submitted, tid, task_item["id"], 1)
                        continue
                    t_label = "الواجب" if t_type == "assign" else "الاختبار/المهمة"
                    done_msg = (
                        f"✅ <b>تم رصد إنجاز {escape_html(t_label)} تلقائياً في LMS!</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"📖 المادة: <b>{escape_html(cname)}</b>\n"
                        f"📝 المهمة: <b>{escape_html(task_item.get('name', ''))}</b>\n"
                        f"🔕 <i>تم تحديث حالتها إلى (منجز) وإيقاف التذكيرات الزمنية الخاصة بها تلقائياً.</i>"
                    )
                    sent = await self._send(tid, done_msg)
                    if sent:
                        await self._run_db(db.mark_assignment_submitted, tid, task_item["id"], 1)
                    else:
                        all_sends_ok = False
                else:
                    await self._run_db(db.mark_assignment_submitted, tid, task_item["id"], 1)

        events = []

        for item in assignments:
            event = await self._run_db(
                self._collect_change, tid, cid, cname, item, "assign", user.get("notify_assign", 1)
            )
            if event:
                events.append(event)

        for item in quizzes:
            event = await self._run_db(
                self._collect_change, tid, cid, cname, item, "quiz", user.get("notify_quiz", 1)
            )
            if event:
                events.append(event)

        for item in content.get("resources", []):
            event = await self._run_db(
                self._collect_change, tid, cid, cname, item, "resource", user.get("notify_resource", 1)
            )
            if event:
                events.append(event)

        # 4. Gradebook & Quiz Marks Radar
        try:
            grades_list = content.get("grades", [])
            quiz_cmids = {str(q["id"]).replace("quiz_", "") for q in quizzes}
            is_first_grade_seed = is_first_seed

            def _same_grade_val(v1: str, v2: str) -> bool:
                s1 = " ".join(str(v1 or "").split()).strip()
                s2 = " ".join(str(v2 or "").split()).strip()
                if s1 == s2:
                    return True
                if not s1 or not s2:
                    return False
                try:
                    return abs(float(s1) - float(s2)) < 1e-6
                except ValueError:
                    return s1.casefold() == s2.casefold()

            for g in grades_list:
                item_id = g["item_id"]
                item_name = g.get("item_name", "تقييم")
                grade_val = (g.get("grade_val") or "").strip()
                grade_max = (g.get("grade_max") or "").strip()
                pct = (g.get("percentage") or "").strip()
                fb = (g.get("feedback") or "").strip()
                g_url = g.get("url", "")
                g_cmid = str(item_id).replace("mod_", "")
                is_quiz_grade = bool(g.get("is_quiz")) or g_cmid in quiz_cmids or "/mod/quiz/" in g_url

                old_g = await self._run_db(db.get_grade, tid, cid, item_id)
                old_val = (old_g.get("grade_val") or "").strip() if old_g else ""

                should_notify = (
                    not is_first_grade_seed
                    and bool(grade_val)
                    and not _same_grade_val(grade_val, old_val)
                    and (
                        bool(user.get("notify_grade", 1))
                        or (is_quiz_grade and bool(user.get("notify_quiz", 1)))
                    )
                )

                if should_notify:
                    if is_quiz_grade:
                        header_line = (
                            "🔄 <b>تم تحديث علامة الكويز في LMS!</b>"
                            if old_val else
                            "🎓 <b>تم إصدار علامة الكويز في LMS!</b>"
                        )
                        item_label = "الكويز"
                        link_label = "فتح الكويز في LMS"
                    else:
                        header_line = "🎯 <b>تم رصد علامة جديدة في LMS!</b>"
                        item_label = "التقييم"
                        link_label = "عرض سجل العلامات"

                    g_lines = [
                        header_line,
                        "━━━━━━━━━━━━━━━━━━━━━━",
                        f"📖 المادة: <b>{escape_html(cname)}</b>",
                        f"📝 {item_label}: <b>{escape_html(item_name)}</b>",
                        f"🏆 العلامة: <code>{escape_html(grade_val)}</code>"
                        + (f" <i>(المدى: {escape_html(grade_max)})</i>" if grade_max else ""),
                    ]
                    if old_val:
                        g_lines.append(f"↩️ <i>(العلامة السابقة: <code>{escape_html(old_val)}</code>)</i>")
                    if pct:
                        g_lines.append(f"📊 النسبة: <code>{escape_html(pct)}</code>")
                    if fb:
                        g_lines.append(f"💬 ملاحظات الدكتور: <i>{escape_html(fb[:300])}</i>")
                    if g_url:
                        g_lines.append(f"🔗 {html_link(link_label, g_url)}")

                    # H6: Persist notified grade ONLY AFTER _send returns True
                    sent = await self._send(tid, "\n".join(g_lines))
                    if sent:
                        await self._run_db(
                            db.upsert_grade, tid, cid, item_id, item_name, grade_val, grade_max, pct, fb, g_url
                        )
                    else:
                        all_sends_ok = False
                else:
                    await self._run_db(
                        db.upsert_grade, tid, cid, item_id, item_name, grade_val, grade_max, pct, fb, g_url
                    )
        except Exception as ge:
            logger.warning(f"Error checking grades for course {cid} (user {tid}): {ge}")

        # 5. Course Announcements & Forum Radar (gated by courses.is_seeded, NOT len(existing_anns) == 0)
        try:
            announcements_list = content.get("announcements", [])
            is_first_ann_seed = is_first_seed

            for ann in announcements_list:
                discuss_id = ann["discuss_id"]
                existing_ann = await self._run_db(db.get_announcement, tid, discuss_id)
                if existing_ann is not None:
                    continue

                body_text = ""
                author = ann.get("author", "")
                posted_date = ann.get("posted_date", "")

                if not is_first_ann_seed and ann.get("url"):
                    details = await loop.run_in_executor(None, scraper.get_announcement_details, ann["url"])
                    body_text = details.get("body", "")
                    author = author or details.get("author", "")
                    posted_date = posted_date or details.get("date", "")

                if not is_first_ann_seed and user.get("notify_announcement", 1):
                    a_lines = [
                        "📢 <b>إعلان جديد من مدرس المساق!</b>",
                        "━━━━━━━━━━━━━━━━━━━━━━",
                        f"📖 المادة: <b>{escape_html(cname)}</b>",
                        f"📌 الموضوع: <b>{escape_html(ann.get('title', 'إعلان'))}</b>",
                    ]
                    if author:
                        a_lines.append(f"👨‍🏫 الناشر: {escape_html(author)}")
                    if posted_date:
                        a_lines.append(f"🕒 التاريخ: <code>{escape_html(posted_date)}</code>")
                    if body_text:
                        a_lines.append(f"\n📝 <b>نص الإعلان:</b>\n{escape_html(body_text[:600])}")
                    if ann.get("url"):
                        a_lines.append(f"\n🔗 {html_link('فتح الإعلان في LMS', ann['url'])}")

                    # H6: Save announcement ONLY AFTER _send returns True
                    sent = await self._send(tid, "\n".join(a_lines))
                    if sent:
                        await self._run_db(
                            db.upsert_announcement,
                            tid,
                            cid,
                            discuss_id,
                            ann.get("forum_name", "منتدى المقرر"),
                            ann.get("title", "إعلان جديد"),
                            author,
                            posted_date,
                            body_text,
                            ann.get("url", ""),
                        )
                    else:
                        all_sends_ok = False
                else:
                    await self._run_db(
                        db.upsert_announcement,
                        tid,
                        cid,
                        discuss_id,
                        ann.get("forum_name", "منتدى المقرر"),
                        ann.get("title", "إعلان جديد"),
                        author,
                        posted_date,
                        body_text,
                        ann.get("url", ""),
                    )
        except Exception as ane:
            logger.warning(f"Error checking announcements for course {cid} (user {tid}): {ane}")

        # 6. Attendance & Absence Radar (gated by courses.is_seeded, NOT old_att is None)
        try:
            attendance_list = content.get("attendance", [])
            is_first_att_seed = is_first_seed

            for att in attendance_list:
                att_id = att["att_id"]
                old_att = await self._run_db(db.get_attendance, tid, cid, att_id)

                old_sessions_map = {}
                if old_att and old_att.get("sessions_json"):
                    try:
                        for s in json.loads(old_att["sessions_json"]):
                            if s.get("date"):
                                old_sessions_map[s["date"]] = s.get("status", "")
                    except Exception:
                        pass

                new_sessions = att.get("sessions", [])
                changed_sessions = []
                if not is_first_att_seed and user.get("notify_attendance", 1):
                    for s in new_sessions:
                        s_date = s.get("date", "")
                        s_status = s.get("status", "")
                        if not s_date or not s_status:
                            continue
                        prev_st = old_sessions_map.get(s_date)
                        if prev_st != s_status:
                            changed_sessions.append((s, prev_st))

                att_sent_ok = True
                if changed_sessions:
                    for s, prev_st in changed_sessions:
                        cat = LMSScraper._parse_attendance_status_category(s.get("status", ""))
                        if cat == "present":
                            st_badge = "✅ حاضر (Present)"
                        elif cat == "absent":
                            st_badge = "❌ غائب (Absent)"
                        elif cat == "late":
                            st_badge = "⏳ متأخر (Late)"
                        elif cat == "excused":
                            st_badge = "📝 معذور (Excused)"
                        else:
                            st_badge = f"📌 {escape_html(s.get('status', ''))}"

                        att_lines = [
                            "📋 <b>تحديث سجل الحضور والغياب في LMS!</b>",
                            "━━━━━━━━━━━━━━━━━━━━━━",
                            f"📖 المادة: <b>{escape_html(cname)}</b>",
                            f"📅 تاريخ المحاضرة: <code>{escape_html(s.get('date', ''))}</code>",
                            f"📌 الحالة المسجلة: <b>{st_badge}</b>",
                        ]
                        if prev_st:
                            att_lines.append(f"🔄 <i>(تغيرت من: {escape_html(prev_st)})</i>")
                        if s.get("points"):
                            att_lines.append(f"🎯 النقاط: <code>{escape_html(s['points'])}</code>")
                        if s.get("remarks"):
                            att_lines.append(f"💬 ملاحظات: <i>{escape_html(s['remarks'])}</i>")

                        att_lines.append(
                            f"\n📊 <b>ملخص حضورك في المادة:</b>\n"
                            f"✅ حاضر: <code>{att.get('present_count', 0)}</code>  |  "
                            f"❌ غائب: <code>{att.get('absent_count', 0)}</code>  |  "
                            f"📈 النسبة: <code>{escape_html(att.get('percentage_str') or '—')}</code>"
                        )
                        if att.get("url"):
                            att_lines.append(f"🔗 {html_link('فتح سجل الحضور في LMS', att['url'])}")
                        if not await self._send(tid, "\n".join(att_lines)):
                            att_sent_ok = False
                            all_sends_ok = False

                # H6: Only persist attendance snapshot if no notification failed
                if att_sent_ok:
                    await self._run_db(
                        db.upsert_attendance,
                        tid,
                        cid,
                        att_id,
                        taken_sessions=att.get("taken_sessions", 0),
                        total_sessions=att.get("total_sessions", 0),
                        present_count=att.get("present_count", 0),
                        absent_count=att.get("absent_count", 0),
                        late_count=att.get("late_count", 0),
                        excused_count=att.get("excused_count", 0),
                        points_str=att.get("points_str", ""),
                        percentage_str=att.get("percentage_str", ""),
                        sessions_json=json.dumps(new_sessions, ensure_ascii=False),
                        url=att.get("url", ""),
                    )
        except Exception as atte:
            logger.warning(f"Error checking attendance for course {cid} (user {tid}): {atte}")

        if is_first_seed:
            await self._mark_course_seeded(tid, cid)

        return events, all_sends_ok, False, False

    @staticmethod
    def _normalize_name(value: str) -> str:
        s = " ".join(str(value or "").split()).strip().casefold()
        # Moodle switches default forum name between English and Arabic depending on session language
        if s in ("announcements", "الإعلانات", "الاعلانات", "إعلانات", "اعلانات"):
            return "__default_announcements__"
        return s

    @staticmethod
    def _same_due_date(old_due: str, new_due: str) -> bool:
        o = " ".join(str(old_due or "").split()).strip()
        n = " ".join(str(new_due or "").split()).strip()
        if o.casefold() == n.casefold():
            return True
        if not o or not n:
            return False
        ts_o = _parse_due_date(o)
        ts_n = _parse_due_date(n)
        if ts_o is not None and ts_n is not None:
            return abs(ts_o - ts_n) < 120
        return False

    def _collect_change(self, tid, cid, cname, item, content_type, enabled):
        content_id = str(item["id"])
        name = (item.get("name") or "").strip()
        url = (item.get("url") or "").strip()
        due = (item.get("due_date") or "").strip()
        new_sub = bool(item.get("is_submitted", False))
        existing = db.get_seen_content(tid, content_id, content_type)
        now_ts = int(time.time())

        if existing:
            old_name = (existing.get("content_name") or "").strip()
            old_url = (existing.get("content_url") or "").strip()
            old_due = (existing.get("due_date") or "").strip()
            old_sub = bool(existing.get("is_submitted", 0))

            effective_sub = 1 if (old_sub or new_sub) else 0
            effective_name = name or old_name
            # Never erase a known due_date with an empty string on a transient scrape miss
            effective_due = due or old_due

            # 1. Already-seen resources: silently refresh metadata/clean URLs, never spam repeat summaries
            if content_type == "resource":
                if effective_name != old_name or url != old_url:
                    db.update_seen_content(tid, content_id, content_type, effective_name, url, effective_due)
                return None

            # 2. Already-completed assignments & quizzes: silently sync metadata, never notify again
            if effective_sub == 1:
                if effective_name != old_name or url != old_url or effective_due != old_due or not old_sub:
                    db.update_seen_content(
                        tid, content_id, content_type,
                        effective_name, url, effective_due, is_submitted=1,
                    )
                return None

            # 3. Check for genuine, actionable changes on OPEN/UNSUBMITTED assignments & quizzes
            changes = []
            if name and self._normalize_name(old_name) != self._normalize_name(name):
                changes.append(
                    f"تغيّر الاسم: «{self._one_line(old_name)}» ← «{self._one_line(name)}»"
                )

            if due and not self._same_due_date(old_due, due):
                new_due_ts = _parse_due_date(due)
                old_due_ts = _parse_due_date(old_due) if old_due else None
                # Only notify about deadline discovery/changes if the new deadline is still in the future
                is_future_deadline = (new_due_ts is None) or (new_due_ts > now_ts)
                if is_future_deadline:
                    if old_due:
                        changes.append(
                            f"تغيّر موعد التسليم: «{self._one_line(old_due)}» ← "
                            f"«{self._one_line(due)}»"
                        )
                    elif old_due_ts is None:
                        changes.append(f"تم تحديد موعد التسليم: «{self._one_line(due)}»")

            # Note: Never trigger notifications for URL-only parameter changes on the same cmid
            if not changes:
                if effective_name != old_name or url != old_url or effective_due != old_due:
                    db.update_seen_content(
                        tid, content_id, content_type,
                        effective_name, url, effective_due, is_submitted=effective_sub,
                    )
                return None

            if not enabled:
                db.update_seen_content(
                    tid, content_id, content_type,
                    effective_name, url, effective_due, is_submitted=effective_sub,
                )
                return None

            return {
                "telegram_id": tid,
                "course_id": cid,
                "course_name": cname,
                "content_id": content_id,
                "content_type": content_type,
                "name": effective_name,
                "url": url,
                "raw_url": item.get("raw_url", ""),
                "due": effective_due,
                "is_submitted": effective_sub,
                "is_new": False,
                "changes": changes,
            }

        # Brand new item (existing is None)
        is_sub_val = 1 if new_sub else 0
        # Do not notify about newly-discovered assignments/quizzes that are ALREADY submitted or ALREADY past due
        if content_type in ("assign", "quiz"):
            due_ts = _parse_due_date(due) if due else None
            if is_sub_val == 1 or (due_ts is not None and due_ts < now_ts):
                db.mark_seen(tid, cid, content_id, content_type, name, url, due, is_submitted=is_sub_val)
                return None

        if not enabled:
            db.mark_seen(tid, cid, content_id, content_type, name, url, due, is_submitted=is_sub_val)
            return None

        return {
            "telegram_id": tid,
            "course_id": cid,
            "course_name": cname,
            "content_id": content_id,
            "content_type": content_type,
            "name": name,
            "url": url,
            "raw_url": item.get("raw_url", ""),
            "due": due,
            "is_submitted": is_sub_val,
            "is_new": True,
            "changes": [],
        }

    @staticmethod
    def _one_line(value):
        return " ".join(str(value or "غير محدد").split())

    def _build_change_chunks(self, events, max_chars=3400):
        if not events:
            return []

        labels = {"assign": "واجب", "quiz": "كويز", "resource": "مورد"}
        grouped = {}
        for event in events:
            grouped.setdefault(
                (event["course_id"], event["course_name"]), []
            ).append(event)

        chunks = []
        current_events = []
        current_lines = []
        current_course = None

        for (course_id, course_name), course_events in grouped.items():
            for event in course_events:
                course_key = (course_id, course_name)
                item_label = labels.get(event["content_type"], "محتوى")
                status = "🆕 جديد" if event["is_new"] else "✏️ تحديث"
                block = []
                if current_course != course_key:
                    block.append(f"📚 المادة: {self._one_line(course_name)}")
                block.append(
                    f"{status} {item_label}: {self._one_line(event['name'])}"
                )
                if event["is_new"]:
                    block.append(f"   التغيير: إضافة {item_label} جديد للمادة")
                else:
                    block.extend(f"   • {change}" for change in event["changes"])
                if event["is_new"] and event["due"]:
                    block.append(f"   • موعد التسليم: {self._one_line(event['due'])}")
                if event["url"]:
                    block.append(f"   🔗 {event['url']}")

                proposed = "\n".join(current_lines + block)
                if current_events and len(proposed) > max_chars:
                    chunks.append((current_events, "\n".join(current_lines)))
                    current_events = []
                    current_lines = []
                    current_course = None
                    if not block or not block[0].startswith("📚 المادة:"):
                        block.insert(0, f"📚 المادة: {self._one_line(course_name)}")

                current_lines.extend(block)
                current_events.append(event)
                current_course = course_key

        if current_events:
            chunks.append((current_events, "\n".join(current_lines)))

        total = len(events)
        return [
            (
                chunk_events,
                f"🔔 ملخص تغييرات المواد ({index}/{len(chunks)})\n"
                f"عدد التغييرات: {total}\n\n{body}",
            )
            for index, (chunk_events, body) in enumerate(chunks, start=1)
        ]

    @staticmethod
    def _mark_new_seen(event):
        db.mark_seen(
            event["telegram_id"], event["course_id"], event["content_id"],
            event["content_type"], event["name"], event["url"], event["due"],
            is_submitted=event.get("is_submitted", 0),
        )

    @staticmethod
    def _save_change(event):
        if not event["is_new"]:
            db.update_seen_content(
                event["telegram_id"], event["content_id"], event["content_type"],
                event["name"], event["url"], event["due"],
                is_submitted=event.get("is_submitted", 0),
            )

    async def _on_forbidden(self, chat_id: int) -> None:
        """Disable notifications in DB when Telegram returns Forbidden (user blocked the bot)."""
        fn = getattr(db, "set_user_notifications_enabled", None)
        if callable(fn):
            try:
                await self._run_db(fn, int(chat_id), False)
            except Exception as exc:
                logger.warning(f"Failed to disable notifications after Forbidden for {chat_id}: {exc}")

    async def _send(
        self,
        chat_id: int,
        text: str,
        keyboard: InlineKeyboardMarkup = None,
        plain: bool = False,
    ) -> bool:
        html_text = escape_html(text) if plain else text
        ok = await send_html_message(
            self.bot,
            chat_id,
            html_text,
            reply_markup=keyboard,
            disable_web_page_preview=True,
            on_forbidden=self._on_forbidden,
        )
        if ok:
            await asyncio.sleep(0.3)
        return ok

    async def force_check(self, telegram_id: int):
        user = await self._run_db(db.get_user, telegram_id)
        if not user:
            return 0, "المستخدم غير مسجّل"

        courses = await self._run_db(db.get_user_courses, telegram_id, True)
        if not courses:
            return 0, "لا توجد مواد مراقَبة"

        tid = int(telegram_id)
        lock = self._user_locks.setdefault(tid, asyncio.Lock())
        async with lock:
            scraper = self._get_session(user)
            loop = asyncio.get_running_loop()
            with self._state_lock:
                needs_relogin = (not scraper.logged_in) or (tid in self._auth_required)
            if needs_relogin:
                await self._discard_session_async(tid, logout=False)
                scraper = self._get_session(user)
                ok = await loop.run_in_executor(None, scraper.login)
                if not ok:
                    if scraper.waf_blocked:
                        with self._state_lock:
                            self._waf_backoff_until[tid] = time.monotonic() + self.WAF_BACKOFF_SECONDS
                        return 0, scraper.error_message or "جدار حماية موقع الجامعة طلب تحققاً أمنياً مؤقتاً؛ حاول بعد عدة دقائق"
                    if scraper.credentials_invalid:
                        with self._state_lock:
                            self._auth_required.add(tid)
                        return 0, "بيانات تسجيل الدخول إلى LMS غير صالحة؛ أعد تسجيل الدخول من البوت"
                    return 0, scraper.error_message or "فشل تسجيل الدخول إلى LMS"
            with self._state_lock:
                self._auth_required.discard(tid)
                self._auth_alerted.discard(tid)
                # Invalidate course cache for this user's courses to ensure fresh data
                for c in courses:
                    self._course_cache.pop((tid, c["course_id"]), None)
            count, complete = await self._scan_user(user, courses, scraper, bypass_cache=True)
            if self._last_scan_waf.get(tid) or scraper.waf_blocked:
                with self._state_lock:
                    self._waf_backoff_until[tid] = time.monotonic() + self.WAF_BACKOFF_SECONDS
                return count, "جدار حماية موقع الجامعة طلب تحققاً أمنياً مؤقتاً؛ تم إيقاف الفحص مؤقتاً"
            if self._last_scan_expired.get(tid) or scraper.session_expired:
                await self._discard_session_async(tid, logout=False)
                scraper = self._get_session(user)
                ok = await loop.run_in_executor(None, scraper.login)
                if not ok:
                    return count, scraper.error_message or "انتهت جلسة LMS وتعذّر تجديدها مؤقتاً"
                count, complete = await self._scan_user(user, courses, scraper, bypass_cache=True)
                if self._last_scan_expired.get(tid) or scraper.session_expired:
                    return count, "انتهت جلسة LMS؛ حاول مجدداً بعد قليل"
            if complete:
                with self._state_lock:
                    self._incomplete_failures.pop(tid, None)
                    self._transient_backoff_until.pop(tid, None)
                await self._run_db(db.update_last_check, tid)
                try:
                    await self._run_db(
                        db.append_sync_log,
                        tid,
                        "فحص فوري (تيليجرام)",
                        f"تم فحص {len(courses)} مقررات" + (f" · {count} تحديث جديد" if count > 0 else " · لا تحديثات جديدة"),
                    )
                except Exception:
                    pass
                return count, "ok"
            return count, "تعذر إكمال الفحص؛ ستتم إعادة المحاولة"
