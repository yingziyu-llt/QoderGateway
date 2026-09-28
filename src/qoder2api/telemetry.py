"""Recent request telemetry used by the model observability page.

The gateway intentionally stores timings and request metadata only. Prompt and
response content are never persisted here.
"""

from __future__ import annotations

import math
import hashlib
import sqlite3
import threading
import time
from collections import defaultdict, deque
from typing import Any

from .database import get_db


_memory_events: deque[dict[str, Any]] = deque(maxlen=1000)
_memory_lock = threading.Lock()


def api_key_fingerprint(api_key: str | None) -> str | None:
    """Return a non-reversible identifier for a gateway API key."""
    value = (api_key or "").strip()
    if not value:
        return None
    return "key_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def mask_api_key(api_key: str) -> str:
    value = api_key.strip()
    fingerprint = api_key_fingerprint(value) or "key_unknown"
    suffix = fingerprint[-6:]
    if len(value) <= 10:
        return f"{value[:3]}... [{suffix}]"
    return f"{value[:8]}...{value[-4:]} [{suffix}]"


def record_request(
    *,
    request_id: str | None = None,
    model: str,
    account_uid: str | None,
    region: str | None,
    source: str,
    success: bool,
    total_ms: float | None,
    ttft_ms: float | None = None,
    status_code: int | None = None,
    error: str | None = None,
    api_key_hash: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
    tokens_estimated: bool = False,
) -> None:
    """Append one request attempt and keep a bounded recent history."""
    model_name = (model or "lite").strip()[:160] or "lite"
    safe_error = (error or "")[:500] or None
    event = {
        "request_id": (request_id or "")[:160] or None,
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
        "api_key_hash": api_key_hash,
        "prompt_tokens": _nonnegative_int(prompt_tokens),
        "completion_tokens": _nonnegative_int(completion_tokens),
        "total_tokens": _nonnegative_int(total_tokens),
        "tokens_estimated": 1 if tokens_estimated else 0,
    }
    try:
        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO request_events (
                request_id, created_at, model, account_uid, region, source, success,
                status_code, ttft_ms, total_ms, error, api_key_hash,
                prompt_tokens, completion_tokens, total_tokens, tokens_estimated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["request_id"], event["created_at"], event["model"], event["account_uid"], event["region"],
                    event["source"], event["success"], event["status_code"], event["ttft_ms"],
                    event["total_ms"], event["error"], event["api_key_hash"],
                    event["prompt_tokens"], event["completion_tokens"], event["total_tokens"],
                    event["tokens_estimated"],
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


def _nonnegative_int(value: int | None) -> int | None:
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


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
    prompt_tokens = [int(row["prompt_tokens"]) for row in rows if row.get("prompt_tokens") is not None]
    completion_tokens = [int(row["completion_tokens"]) for row in rows if row.get("completion_tokens") is not None]
    total_tokens = [int(row["total_tokens"]) for row in rows if row.get("total_tokens") is not None]
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
        "prompt_tokens": sum(prompt_tokens),
        "completion_tokens": sum(completion_tokens),
        "total_tokens": sum(total_tokens),
        "token_events": len(total_tokens),
        "estimated_token_events": sum(1 for row in rows if row.get("tokens_estimated")),
    }


def get_model_metrics(window_hours: int = 24) -> dict[str, Any]:
    """Return per-model and recent request metrics for the UI."""
    hours = max(1, min(int(window_hours), 24 * 30))
    cutoff = time.time() - hours * 3600
    try:
        with get_db() as conn:
            raw = conn.execute(
                """
                SELECT request_id, created_at, model, account_uid, region, source, success,
                       status_code, ttft_ms, total_ms, error, api_key_hash,
                       prompt_tokens, completion_tokens, total_tokens, tokens_estimated
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
                "request_id": row.get("request_id"),
                "model": row["model"],
                "account_uid": row["account_uid"],
                "region": row["region"],
                "source": row["source"],
                "success": bool(row["success"]),
                "status_code": row["status_code"],
                "ttft_ms": row["ttft_ms"],
                "total_ms": row["total_ms"],
                "error": row["error"],
                "api_key_hash": row.get("api_key_hash"),
                "prompt_tokens": row.get("prompt_tokens"),
                "completion_tokens": row.get("completion_tokens"),
                "total_tokens": row.get("total_tokens"),
                "tokens_estimated": bool(row.get("tokens_estimated")),
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


def get_api_key_usage(window_hours: int = 24) -> dict[str, Any]:
    """Aggregate recent usage by API key and model without exposing key values."""
    metrics = get_model_metrics(window_hours)
    configured: list[dict[str, Any]] = []
    try:
        with get_db() as conn:
            configured = [dict(row) for row in conn.execute("SELECT api_key FROM allowed_keys").fetchall()]
            raw = conn.execute(
                """
                SELECT request_id, created_at, model, api_key_hash, source, success,
                       prompt_tokens, completion_tokens, total_tokens, tokens_estimated,
                       ttft_ms, total_ms
                FROM request_events
                WHERE created_at >= ?
                ORDER BY created_at DESC
                """,
                (time.time() - max(1, min(int(window_hours), 24 * 30)) * 3600,),
            ).fetchall()
        rows = [dict(row) for row in raw]
    except (sqlite3.Error, OSError):
        with _memory_lock:
            rows = [dict(row) for row in _memory_events]

    configured_map = {
        api_key_fingerprint(item["api_key"]): {
            "fingerprint": api_key_fingerprint(item["api_key"]),
            "label": mask_api_key(item["api_key"]),
            "configured": True,
        }
        for item in configured
    }
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    rows = [row for row in rows if row.get("source") != "benchmark"]
    for row in rows:
        grouped[row.get("api_key_hash") or "anonymous"].append(row)

    keys: list[dict[str, Any]] = []
    for fingerprint, key_rows in grouped.items():
        summary = _summary(key_rows)
        models: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in key_rows:
            models[row["model"]].append(row)
        summary["by_model"] = {model: _summary(model_rows) for model, model_rows in models.items()}
        info = configured_map.get(fingerprint, {"fingerprint": fingerprint, "label": "Anonymous", "configured": False})
        keys.append({**info, **summary})

    for fingerprint, info in configured_map.items():
        if not any(item["fingerprint"] == fingerprint for item in keys):
            keys.append({**info, **_summary([]), "by_model": {}})
    keys.sort(key=lambda item: (item.get("total_tokens", 0), item.get("requests", 0)), reverse=True)
    return {
        "window_hours": metrics["window_hours"],
        "total_requests": sum(item.get("requests", 0) for item in keys),
        "total_tokens": sum(item.get("total_tokens", 0) for item in keys),
        "prompt_tokens": sum(item.get("prompt_tokens", 0) for item in keys),
        "completion_tokens": sum(item.get("completion_tokens", 0) for item in keys),
        "keys": keys,
    }
