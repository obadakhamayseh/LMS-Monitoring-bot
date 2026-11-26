"""Absence risk and university deprivation threshold calculations (F6)."""

from __future__ import annotations

import math
from typing import Any

import config

_RISK_SEVERITY_ORDER: dict[str, int] = {
    "deprived": 0,
    "critical": 1,
    "warning": 2,
    "safe": 3,
}


def calculate_absence_risk(
    absent_count: int,
    total_sessions: int = 0,
    *,
    credit_hours: int = 3,
    max_absence_pct: float = 15.0,
    course_name: str = "",
) -> dict[str, Any]:
    """Calculate absence risk level and Arabic warning message for a course.

    Effective semester sessions use ``total_sessions`` once at least
    ``credit_hours * 10`` sessions are recorded; otherwise ``max(total_sessions, credit_hours * 16)``
    so partial mid-semester attendance records do not underestimate total semester sessions.
    """
    absents = max(0, int(absent_count or 0))
    recorded_total = max(0, int(total_sessions or 0))
    credits = max(1, int(credit_hours or 3))
    try:
        max_pct = float(max_absence_pct if max_absence_pct is not None else 15.0)
    except (TypeError, ValueError):
        max_pct = 15.0
    if max_pct <= 0:
        max_pct = 15.0

    effective_total = (
        recorded_total
        if recorded_total >= credits * 10
        else max(recorded_total, credits * 16)
    )
    max_allowed = max(1, int(math.floor(effective_total * max_pct / 100.0)))
    remaining = max(0, max_allowed - absents)
    absence_pct = round((absents / effective_total) * 100.0, 1) if effective_total > 0 else 0.0

    if absents >= max_allowed:
        risk_level = "deprived"
    elif remaining <= 1:
        risk_level = "critical"
    elif remaining <= 2:
        risk_level = "warning"
    else:
        risk_level = "safe"

    cname = str(course_name or "").strip()
    label = cname or "المقرر"
    if risk_level == "deprived":
        message = (
            f"🚨 تنبيه حرمان: وصلت إلى حد الغياب المسموح ({absents}/{max_allowed}) "
            f"في مادة {label} ({max_pct:g}%)"
        )
    elif risk_level in ("critical", "warning"):
        message = (
            f"⚠️ بقي لك {remaining} غيابات في مادة {label} "
            f"قبل الوصول لحد الحرمان ({max_pct:g}%)"
        )
    else:
        message = ""

    return {
        "course_name": cname,
        "absent_count": absents,
        "total_sessions": recorded_total,
        "effective_total_sessions": effective_total,
        "credit_hours": credits,
        "max_absence_pct": max_pct,
        "max_allowed": max_allowed,
        "remaining": remaining,
        "absence_pct": absence_pct,
        "risk_level": risk_level,
        "at_risk": risk_level != "safe",
        "message": message,
    }


def evaluate_user_absence_risks(db: Any, telegram_id: int) -> list[dict[str, Any]]:
    """Evaluate absence risks across all courses with attendance data for a user."""
    user = db.get_user(telegram_id)
    if not user:
        return []

    raw_pct = user.get("max_absence_pct")
    try:
        max_pct = float(raw_pct) if raw_pct is not None else float(config.DEFAULT_MAX_ABSENCE_PCT)
    except (TypeError, ValueError):
        max_pct = float(config.DEFAULT_MAX_ABSENCE_PCT)
    if max_pct <= 0:
        max_pct = float(config.DEFAULT_MAX_ABSENCE_PCT)

    courses = db.get_user_courses(telegram_id) or []
    course_map: dict[int, dict[str, Any]] = {}
    for c in courses:
        try:
            cid = int(c["course_id"])
        except (KeyError, TypeError, ValueError):
            continue
        course_map[cid] = c

    att_rows = db.get_user_attendance(telegram_id) or []
    grouped: dict[int, dict[str, Any]] = {}
    for row in att_rows:
        try:
            cid = int(row["course_id"])
        except (KeyError, TypeError, ValueError):
            continue
        bucket = grouped.setdefault(
            cid,
            {
                "course_name": str(row.get("course_name") or "").strip(),
                "absent_count": 0,
                "total_sessions": 0,
                "taken_sessions": 0,
                "present_count": 0,
                "late_count": 0,
                "excused_count": 0,
            },
        )
        bucket["absent_count"] += max(0, int(row.get("absent_count") or 0))
        bucket["taken_sessions"] += max(0, int(row.get("taken_sessions") or 0))
        row_total = max(0, int(row.get("total_sessions") or 0))
        row_taken = max(0, int(row.get("taken_sessions") or 0))
        bucket["total_sessions"] += max(row_total, row_taken)
        bucket["present_count"] += max(0, int(row.get("present_count") or 0))
        bucket["late_count"] += max(0, int(row.get("late_count") or 0))
        bucket["excused_count"] += max(0, int(row.get("excused_count") or 0))
        if not bucket["course_name"] and row.get("course_name"):
            bucket["course_name"] = str(row["course_name"]).strip()

    results: list[dict[str, Any]] = []
    for cid, agg in grouped.items():
        course_row = course_map.get(cid, {})
        cname = str(course_row.get("course_name") or agg.get("course_name") or f"مادة {cid}").strip()
        try:
            credit_hours = max(1, int(course_row.get("credit_hours") or 3))
        except (TypeError, ValueError):
            credit_hours = 3
        last_risk_level = str(course_row.get("last_risk_level") or "").strip()

        risk = calculate_absence_risk(
            agg["absent_count"],
            agg["total_sessions"],
            credit_hours=credit_hours,
            max_absence_pct=max_pct,
            course_name=cname,
        )
        risk.update(
            {
                "course_id": cid,
                "last_risk_level": last_risk_level,
                "taken_sessions": agg["taken_sessions"],
                "present_count": agg["present_count"],
                "late_count": agg["late_count"],
                "excused_count": agg["excused_count"],
            }
        )
        results.append(risk)

    results.sort(
        key=lambda r: (
            _RISK_SEVERITY_ORDER.get(r["risk_level"], 99),
            r["remaining"],
            r["course_id"],
        )
    )
    return results
