"""Recent request telemetry used by the model observability page.

The gateway intentionally stores timings and request metadata only. Prompt and
response content are never persisted here.
"""

from __future__ import annotations

import math
import sqlite3
import threading
import time
from collections import defaultdict, deque
from typing import Any

from .database import get_db


_memory_events: deque[dict[str, Any]] = deque(maxlen=1000)
_memory_lock = threading.Lock()


def record_request(
    *,
    model: str,
    account_uid: str | None,
    region: str | None,
    source: str,
    success: bool,
    total_ms: float | None,
    ttft_ms: float | None = None,
    status_code: int | None = None,
    error: str | None = None,
) -> None:
    """Append one request attempt and keep a bounded recent history."""
    model_name = (model or "lite").strip()[:160] or "lite"
    safe_error = (error or "")[:500] or None
    event = {
        "created_at": time.time(),
        "model": model_name,
        "account_uid": (account_uid or "")[:160] or None,
        "region": (region or "")[:16] or None,
        "source": source[:32],
        "success": 1 if success else 0,
        "status_code": status_code,
        "ttft_ms": _finite_or_none(ttft_ms),
        "total_ms": _finite_or_none(total_ms),
        "error": safe_error,
    }
    try:
        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO request_events (
                    created_at, model, account_uid, region, source, success,
                    status_code, ttft_ms, total_ms, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["created_at"], event["model"], event["account_uid"], event["region"],
                    event["source"], event["success"], event["status_code"], event["ttft_ms"],
                    event["total_ms"], event["error"],
                ),
            )
            # Keep the database small while preserving enough history for a useful
            # dashboard. This is deliberately a best-effort cleanup.
            conn.execute(
                "DELETE FROM request_events WHERE id NOT IN "
                "(SELECT id FROM request_events ORDER BY id DESC LIMIT 1000)"
            )
    except (sqlite3.Error, OSError):
        with _memory_lock:
            _memory_events.append(event)


def _finite_or_none(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    index = min(len(values) - 1, max(0, math.ceil(percentile * len(values)) - 1))
    return round(values[index], 1)


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    requests = len(rows)
    successes = sum(1 for row in rows if row["success"])
    ttft = [float(row["ttft_ms"]) for row in rows if row["ttft_ms"] is not None]
    total = [float(row["total_ms"]) for row in rows if row["total_ms"] is not None]
    last = max((float(row["created_at"]) for row in rows), default=None)
    return {
        "requests": requests,
        "successes": successes,
        "failures": requests - successes,
        "success_rate": round(successes / requests, 4) if requests else None,
        "avg_ttft_ms": round(sum(ttft) / len(ttft), 1) if ttft else None,
        "p95_ttft_ms": _percentile(ttft, 0.95),
        "avg_total_ms": round(sum(total) / len(total), 1) if total else None,
        "p95_total_ms": _percentile(total, 0.95),
        "last_used_at": last,
    }


def get_model_metrics(window_hours: int = 24) -> dict[str, Any]:
    """Return per-model and recent request metrics for the UI."""
    hours = max(1, min(int(window_hours), 24 * 30))
    cutoff = time.time() - hours * 3600
    try:
        with get_db() as conn:
            raw = conn.execute(
                """
                SELECT created_at, model, account_uid, region, source, success,
                       status_code, ttft_ms, total_ms, error
                FROM request_events
                WHERE created_at >= ?
                ORDER BY created_at DESC
                """,
                (cutoff,),
            ).fetchall()
        rows = [dict(row) for row in raw]
    except (sqlite3.Error, OSError):
        with _memory_lock:
            rows = [dict(row) for row in _memory_events if row["created_at"] >= cutoff]
        rows.sort(key=lambda row: row["created_at"], reverse=True)
    # Benchmark calls are shown in the recent feed but do not alter the
    # production traffic health numbers.
    traffic_rows = [row for row in rows if row["source"] != "benchmark"]
    benchmark_rows = [row for row in rows if row["source"] == "benchmark"]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in traffic_rows:
        grouped[row["model"]].append(row)
    by_model = {model: _summary(model_rows) for model, model_rows in grouped.items()}
    benchmark_grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in benchmark_rows:
        benchmark_grouped[row["model"]].append(row)
    benchmark_by_model = {model: _summary(model_rows) for model, model_rows in benchmark_grouped.items()}

    recent = []
    for row in rows[:50]:
        recent.append(
            {
                "created_at": row["created_at"],
                "model": row["model"],
                "account_uid": row["account_uid"],
                "region": row["region"],
                "source": row["source"],
                "success": bool(row["success"]),
                "status_code": row["status_code"],
                "ttft_ms": row["ttft_ms"],
                "total_ms": row["total_ms"],
                "error": row["error"],
            }
        )
    return {
        "window_hours": hours,
        "total_requests": len(traffic_rows),
        "total_events": len(rows),
        "by_model": by_model,
        "benchmark_by_model": benchmark_by_model,
        "recent": recent,
    }
