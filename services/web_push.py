"""Web Push notification delivery service with VAPID and expired-endpoint cleanup (F7)."""

from __future__ import annotations

import json
import logging
from typing import Any

import config

try:
    from pywebpush import WebPushException, webpush
except ImportError:  # pragma: no cover - optional at runtime if uninstalled
    WebPushException = Exception  # type: ignore[assignment,misc]
    webpush = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def is_web_push_enabled() -> bool:
    """Return True only when Web Push is enabled, VAPID keys are set, and pywebpush is installed."""
    return bool(
        getattr(config, "ENABLE_WEB_PUSH", False)
        and str(getattr(config, "VAPID_PUBLIC_KEY", "") or "").strip()
        and str(getattr(config, "VAPID_PRIVATE_KEY", "") or "").strip()
        and callable(webpush)
    )


def get_vapid_public_key() -> str:
    """Return the configured VAPID public key (stripped)."""
    return str(getattr(config, "VAPID_PUBLIC_KEY", "") or "").strip()


def send_web_push_to_user(
    db: Any,
    telegram_id: int,
    title: str,
    body: str,
    url: str = "/webapp",
) -> dict[str, Any]:
    """Send a Web Push notification to all active subscriptions of ``telegram_id``.

    Automatically removes expired (HTTP 404/410) subscriptions via
    ``db.delete_push_subscription`` and NEVER raises so Telegram delivery is never impacted.
    """
    result: dict[str, Any] = {
        "enabled": False,
        "sent": 0,
        "failed": 0,
        "expired_removed": 0,
        "skipped": False,
    }
    try:
        if not is_web_push_enabled():
            result["skipped"] = True
            return result

        result["enabled"] = True

        try:
            user = db.get_user(telegram_id)
        except Exception as exc:
            logger.warning("Web Push failed reading user %s: %s", telegram_id, exc)
            result["skipped"] = True
            return result

        if user is not None and user.get("enable_web_push") == 0:
            result["skipped"] = True
            return result

        try:
            subscriptions = db.get_push_subscriptions(telegram_id) or []
        except Exception as exc:
            logger.warning("Web Push failed reading subscriptions for user %s: %s", telegram_id, exc)
            result["skipped"] = True
            return result

        if not subscriptions:
            return result

        payload = json.dumps(
            {
                "title": str(title or ""),
                "body": str(body or ""),
                "url": str(url or "/webapp"),
            },
            ensure_ascii=False,
        )
        vapid_private_key = str(getattr(config, "VAPID_PRIVATE_KEY", "") or "").strip()
        vapid_subject = (
            str(getattr(config, "VAPID_SUBJECT", "mailto:admin@example.com") or "").strip()
            or "mailto:admin@example.com"
        )
        vapid_claims = {"sub": vapid_subject}

        for sub in subscriptions:
            endpoint = str(sub.get("endpoint") or "").strip()
            p256dh = str(sub.get("p256dh") or "").strip()
            auth = str(sub.get("auth") or "").strip()
            if not endpoint or not p256dh or not auth:
                result["failed"] += 1
                continue

            subscription_info = {
                "endpoint": endpoint,
                "keys": {
                    "p256dh": p256dh,
                    "auth": auth,
                },
            }
            try:
                assert webpush is not None
                webpush(
                    subscription_info=subscription_info,
                    data=payload,
                    vapid_private_key=vapid_private_key,
                    vapid_claims=vapid_claims,
                    timeout=10,
                )
                result["sent"] += 1
            except WebPushException as exc:
                response = getattr(exc, "response", None)
                status_code = getattr(response, "status_code", None)
                if status_code in (404, 410):
                    try:
                        db.delete_push_subscription(telegram_id, endpoint)
                        result["expired_removed"] += 1
                    except Exception as del_exc:
                        logger.warning(
                            "Web Push failed deleting expired subscription for user %s: %s",
                            telegram_id,
                            del_exc,
                        )
                    logger.info(
                        "Removed expired Web Push subscription (HTTP %s) for user %s",
                        status_code,
                        telegram_id,
                    )
                else:
                    result["failed"] += 1
                    logger.warning(
                        "Web Push delivery failed (HTTP %s) for user %s",
                        status_code,
                        telegram_id,
                    )
            except Exception as exc:
                result["failed"] += 1
                logger.warning(
                    "Unexpected Web Push error for user %s: %s",
                    telegram_id,
                    exc,
                )
    except Exception as exc:  # pragma: no cover - defensive outer guard
        logger.warning("Web Push outer error for user %s: %s", telegram_id, exc)
    return result
