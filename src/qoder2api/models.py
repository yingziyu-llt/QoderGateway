"""Model catalog, discovery and one-shot benchmark helpers."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from .bridge import build_qoder_body, qoder_stream_lines
from .env import httpx_client_kwargs, provider_mode, provider_model_ids
from .regions import configured_region, get_region, normalize_region
from .telemetry import record_request


from .auth import bearer_headers
from .bridge import build_qoder_body, qoder_stream_lines
from .catalog import catalog_entries, parse_model_list
from .env import httpx_client_kwargs, provider_mode, provider_model_ids
from .regions import configured_region, get_region, normalize_region
from .telemetry import record_request


def fallback_models(region: str) -> list[dict[str, Any]]:
    """Return the built-in catalog for a region (see ``catalog.py``)."""
    return [
        {**entry, "source": "gateway-catalog", "available": True}
        for entry in catalog_entries(region)
    ]


def provider_models(region: str) -> list[dict[str, Any]]:
    """Return the stable model catalog exposed to an upstream New API channel."""
    configured = provider_model_ids()
    if not configured:
        return fallback_models(region)
    known = {item["id"]: item for item in fallback_models(region)}
    return [
        {
            **known.get(model, {"id": model, "label": model, "key": model}),
            "source": "provider-config",
            "available": True,
        }
        for model in configured
    ]


def _models_endpoint(url: str) -> str:
    parts = urlsplit(url)
    if parts.path.endswith("/chat/completions"):
        path = parts.path[: -len("/chat/completions")] + "/models"
    else:
        path = parts.path.rsplit("/", 1)[0] + "/models"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _parse_discovered_models(data: Any) -> list[dict[str, Any]]:
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, str):
            model_id = item.strip()
            item_data: dict[str, Any] = {}
        elif isinstance(item, dict):
            model_id = str(item.get("id") or item.get("name") or "").strip()
            item_data = item
        else:
            continue
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        result.append(
            {
                "id": model_id,
                "label": str(item_data.get("display_name") or item_data.get("label") or model_id),
                "key": str(item_data.get("key") or model_id),
                "source": "upstream",
                "available": True,
            }
        )
    return result


async def _discover_model_list(sess: Any) -> list[dict[str, Any]]:
    """Query the Qoder client model-list endpoint (both regions expose it)."""
    endpoint = get_region(sess.region).model_list_url
    headers = bearer_headers(sess, endpoint, "", "application/json", {"x-model-source": "system"})
    async with httpx.AsyncClient(timeout=10, **httpx_client_kwargs()) as client:
        response = await client.get(endpoint, headers=headers)
    if response.status_code != 200:
        return []
    return [
        {**row, "source": "upstream", "available": True}
        for row in parse_model_list(response.json())
    ]


async def _discover_modern_models(sess: Any) -> list[dict[str, Any]]:
    """Fallback for the global region: the OpenAI-compatible ``/models`` route."""
    endpoint = _models_endpoint(get_region("global").modern_chat_url or "")
    async with httpx.AsyncClient(timeout=10, **httpx_client_kwargs()) as client:
        response = await client.get(
            endpoint,
            headers={
                "Authorization": f"Bearer {sess.identity.security_oauth_token}",
                "Accept": "application/json",
                "User-Agent": "qoder2api-python",
            },
        )
    if response.status_code != 200:
        return []
    return _parse_discovered_models(response.json())


async def _discover_upstream(sess: Any) -> list[dict[str, Any]]:
    """Best-effort upstream discovery; failure falls back to the static catalog."""
    for attempt in (_discover_model_list, _discover_modern_models):
        try:
            discovered = await attempt(sess)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            continue
        if discovered:
            return discovered
    return []


async def discover_models(sess: Any | None, region: str | None = None) -> tuple[list[dict[str, Any]], str]:
    """Try the regional model directory, then return a safe fallback catalog."""
    if provider_mode() == "new_api":
        return provider_models(normalize_region(region or configured_region())), "provider-config"
    resolved = normalize_region(region or (getattr(sess, "region", None) if sess is not None else None) or configured_region())
    if sess is not None:
        discovered = await _discover_upstream(sess)
        if discovered:
            return discovered, "upstream"
    return fallback_models(resolved), "gateway-catalog"


async def benchmark_model(sess: Any, model: str, prompt: str) -> dict[str, Any]:
    """Run a short real request and measure first upstream data and completion."""
    request = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "max_tokens": 64,
        "reasoning_effort": "off",
    }
    started = time.perf_counter()
    first_ms: float | None = None
    lines = 0
    try:
        body, model_name, _ = build_qoder_body(request, sess)

        async def consume() -> None:
            nonlocal first_ms, lines
            async for line in qoder_stream_lines(sess, body, model_name):
                if line.startswith("data:"):
                    lines += 1
                    if first_ms is None:
                        first_ms = (time.perf_counter() - started) * 1000

        await asyncio.wait_for(consume(), timeout=90)
        total_ms = (time.perf_counter() - started) * 1000
        ok = lines > 0
        result = {
            "model": model_name,
            "ok": ok,
            "ttft_ms": round(first_ms, 1) if first_ms is not None else None,
            "total_ms": round(total_ms, 1),
            "events": lines,
            "error": None if ok else "上游没有返回 SSE 数据",
        }
        record_request(
            model=model_name,
            account_uid=sess.identity.uid,
            region=sess.region,
            source="benchmark",
            success=ok,
            total_ms=total_ms,
            ttft_ms=first_ms,
            status_code=200 if ok else None,
            error=result["error"],
        )
        return result
    except Exception as exc:
        total_ms = (time.perf_counter() - started) * 1000
        error = str(exc)[:500]
        record_request(
            model=model,
            account_uid=sess.identity.uid,
            region=sess.region,
            source="benchmark",
            success=False,
            total_ms=total_ms,
            ttft_ms=first_ms,
            status_code=None,
            error=error,
        )
        return {
            "model": model,
            "ok": False,
            "ttft_ms": round(first_ms, 1) if first_ms is not None else None,
            "total_ms": round(total_ms, 1),
            "events": lines,
            "error": error,
        }
