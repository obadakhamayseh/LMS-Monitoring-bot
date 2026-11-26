"""Structured JSON logging and thread-safe runtime observability state (F8)."""

from __future__ import annotations

import json
import logging
import logging.handlers
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

import config

_STANDARD_LOG_RECORD_ATTRS: frozenset[str] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


def _ts_to_utc_iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return (
        datetime.fromtimestamp(float(ts), tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


class JsonLogFormatter(logging.Formatter):
    """Single-line JSON log formatter with UTC ISO-8601 timestamps."""

    def format(self, record: logging.LogRecord) -> str:
        ts = (
            datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        payload: dict[str, Any] = {
            "timestamp": ts,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload["exc_info"] = record.exc_text

        for key, value in record.__dict__.items():
            if key in _STANDARD_LOG_RECORD_ATTRS or key.startswith("_"):
                continue
            if key in payload:
                continue
            payload[key] = value

        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(
    log_file: str = "bot.log",
    log_format: Optional[str] = None,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 3,
) -> logging.handlers.RotatingFileHandler:
    """Configure root logging with RotatingFileHandler + StreamHandler.

    Uses ``JsonLogFormatter`` when ``log_format == "json"`` (or ``config.LOG_FORMAT``),
    otherwise falls back to readable text formatting. Returns the RotatingFileHandler
    so callers (e.g. ``bot.py``) can keep a direct reference to it.
    """
    resolved_format = (
        (log_format if log_format is not None else getattr(config, "LOG_FORMAT", "json")) or "json"
    ).strip().lower()

    formatter: logging.Formatter
    if resolved_format == "json":
        formatter = JsonLogFormatter()
    else:
        formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
        delay=True,
    )
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    for existing in list(root_logger.handlers):
        root_logger.removeHandler(existing)
        try:
            existing.close()
        except Exception:
            pass

    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(stream_handler)
    root_logger.addHandler(file_handler)

    return file_handler


class ObservabilityState:
    """Thread-safe runtime metrics and health/staleness state for monitoring & /health."""

    def __init__(self, *, started_at: Optional[float] = None) -> None:
        self._lock = threading.Lock()
        self._started_at: float = float(started_at if started_at is not None else time.time())
        self._monitor_running: bool = False
        self._last_successful_check_at: Optional[float] = None
        self._waf_blocked: bool = False
        self._waf_until: float = 0.0
        self._total_checks: int = 0
        self._error_count: int = 0
        self._last_error: str = ""
        self._api_requests_total: int = 0
        self._scrape_requests_total: int = 0
        self._requests_saved_total: int = 0
        self._stale_alerted: bool = False

    def reset(self, *, started_at: Optional[float] = None) -> None:
        """Reset all counters and timestamps (primarily for isolated tests)."""
        with self._lock:
            self._started_at = float(started_at if started_at is not None else time.time())
            self._monitor_running = False
            self._last_successful_check_at = None
            self._waf_blocked = False
            self._waf_until = 0.0
            self._total_checks = 0
            self._error_count = 0
            self._last_error = ""
            self._api_requests_total = 0
            self._scrape_requests_total = 0
            self._requests_saved_total = 0
            self._stale_alerted = False

    def record_check_success(
        self,
        metrics: Optional[dict[str, Any]] = None,
        *,
        now_ts: Optional[float] = None,
    ) -> None:
        now = float(now_ts if now_ts is not None else time.time())
        with self._lock:
            self._last_successful_check_at = now
            self._total_checks += 1
            self._stale_alerted = False
            if self._waf_blocked and self._waf_until <= now:
                self._waf_blocked = False
                self._waf_until = 0.0
            if isinstance(metrics, dict):
                self._api_requests_total += max(
                    0, int(metrics.get("api_requests", metrics.get("api_requests_total", 0)) or 0)
                )
                self._scrape_requests_total += max(
                    0, int(metrics.get("scrape_requests", metrics.get("scrape_requests_total", 0)) or 0)
                )
                self._requests_saved_total += max(
                    0, int(metrics.get("requests_saved", metrics.get("requests_saved_total", 0)) or 0)
                )

    def record_check_error(self, err_msg: str = "") -> None:
        with self._lock:
            self._total_checks += 1
            self._error_count += 1
            self._last_error = str(err_msg or "")[:500]

    def record_waf_state(self, blocked: bool, until_ts: float = 0.0) -> None:
        with self._lock:
            self._waf_blocked = bool(blocked)
            self._waf_until = float(until_ts or 0.0) if blocked else 0.0

    def set_monitor_running(self, running: bool) -> None:
        with self._lock:
            self._monitor_running = bool(running)

    def _is_stale_unlocked(self, threshold_seconds: int, now: float) -> bool:
        if not self._monitor_running:
            return False
        threshold = max(1, int(threshold_seconds))
        reference = (
            self._last_successful_check_at
            if self._last_successful_check_at is not None
            else self._started_at
        )
        return (now - reference) >= threshold

    def is_stale(
        self,
        threshold_seconds: int = 3600,
        *,
        now_ts: Optional[float] = None,
    ) -> bool:
        now = float(now_ts if now_ts is not None else time.time())
        with self._lock:
            return self._is_stale_unlocked(threshold_seconds, now)

    def mark_stale_alerted(self, alerted: bool = True) -> None:
        with self._lock:
            self._stale_alerted = bool(alerted)

    def should_send_stale_alert(
        self,
        threshold_seconds: int = 3600,
        *,
        now_ts: Optional[float] = None,
    ) -> bool:
        now = float(now_ts if now_ts is not None else time.time())
        with self._lock:
            return self._is_stale_unlocked(threshold_seconds, now) and not self._stale_alerted

    def snapshot(self, *, now_ts: Optional[float] = None) -> dict[str, Any]:
        now = float(now_ts if now_ts is not None else time.time())
        with self._lock:
            error_rate = (
                round(self._error_count / self._total_checks, 4)
                if self._total_checks > 0
                else 0.0
            )
            waf_active = self._waf_blocked and (self._waf_until <= 0 or self._waf_until > now)
            return {
                "monitor_running": self._monitor_running,
                "last_successful_check_at": _ts_to_utc_iso(self._last_successful_check_at),
                "waf_blocked": waf_active,
                "waf_until": self._waf_until if waf_active else 0.0,
                "total_checks": self._total_checks,
                "error_count": self._error_count,
                "error_rate": error_rate,
                "last_error": self._last_error,
                "api_requests_total": self._api_requests_total,
                "scrape_requests_total": self._scrape_requests_total,
                "requests_saved_total": self._requests_saved_total,
                "stale_alerted": self._stale_alerted,
                "uptime_seconds": round(max(0.0, now - self._started_at), 2),
            }


_OBSERVABILITY = ObservabilityState()


def get_observability() -> ObservabilityState:
    """Return the process-wide thread-safe ObservabilityState singleton."""
    return _OBSERVABILITY
