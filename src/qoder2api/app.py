import argparse
import collections
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .auth import SessionContext, create_session, load_local_session
from .bridge import complete_openai_response, stream_openai_response
from .config import load_config, save_config
from .database import get_db
from .env import env_bool
from .accounts import (
    db_load_accounts,
    db_get_settings,
    db_set_settings,
    import_current_auth,
    get_active_session,
    rotate_next_account,
    batch_import_accounts,
)
from .registrar import get_registrar_status, start_registration, stop_registration
from .tokens import (
    refresh_all_account_tokens,
    refresh_one_account,
    get_account_quota,
    get_all_accounts_quota,
    start_refresh_loop,
)
from .regions import configured_region, normalize_region
from .models import benchmark_model, discover_models
from .telemetry import api_key_fingerprint, get_api_key_usage, get_model_metrics, record_request

BASE_DIR = os.path.dirname(__file__)
INDEX_HTML = Path(BASE_DIR) / "static" / "index.html"
CONSOLE_HTML = Path(BASE_DIR) / "static" / "console.html"
DOCS_HTML = Path(BASE_DIR) / "static" / "docs.html"

app = FastAPI(title="qoder2api-python")
app.mount("/assets", StaticFiles(directory=os.path.join(BASE_DIR, "static", "assets")), name="assets")

_session: SessionContext | None = None
_local_auth_error: str | None = None

logs_queue = collections.deque(maxlen=150)


def add_log(msg: str, level: str = "INFO") -> None:
    timestamp = datetime.now().strftime("%H:%M:%S")
    formatted = f"[{timestamp}] [{level}] {msg}"
    logs_queue.append(formatted)
    print(formatted)


def _safe_record_request(**kwargs: Any) -> None:
    """Metrics must never make an otherwise successful request fail."""
    try:
        record_request(**kwargs)
    except Exception as exc:
        add_log(f"Request telemetry unavailable: {exc}", "WARNING")


def _status_code(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None)


def _incoming_api_key(authorization: str | None) -> str | None:
    if authorization and authorization.strip().lower().startswith("bearer "):
        value = authorization.split(" ", 1)[1].strip()
        return value or None
    return None


def _openai_error(message: str, error_type: str, code: str | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"message": message, "type": error_type}
    if code:
        error["code"] = code
    return {"error": error}


def _public_error_type(status_code: int) -> str:
    return {
        400: "invalid_request_error",
        401: "authentication_error",
        403: "permission_error",
        404: "invalid_request_error",
        408: "timeout_error",
        429: "rate_limit_error",
        500: "server_error",
        502: "upstream_error",
        503: "server_error",
        504: "upstream_timeout",
    }.get(status_code, "api_error")


@app.exception_handler(RequestValidationError)
async def public_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    if request.url.path.startswith("/v1/"):
        return JSONResponse(
            status_code=400,
            content=_openai_error("Invalid request body", "invalid_request_error", "invalid_request"),
        )
    return JSONResponse(status_code=422, content={"detail": "Invalid request body"})


@app.exception_handler(HTTPException)
async def public_http_error(request: Request, exc: HTTPException) -> JSONResponse:
    if request.url.path.startswith("/v1/"):
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed"
        return JSONResponse(
            status_code=exc.status_code,
            content=_openai_error(detail, _public_error_type(exc.status_code)),
            headers=exc.headers,
        )
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)


@app.exception_handler(Exception)
async def public_unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    if request.url.path.startswith("/v1/"):
        add_log(f"Unhandled provider error: {_safe_upstream_detail(exc)}", "ERROR")
        return JSONResponse(
            status_code=502,
            content=_openai_error("Qoder upstream request failed", "upstream_error"),
        )
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


def _require_public_api_key(authorization: str | None, config: dict[str, Any]) -> str | None:
    incoming_key = _incoming_api_key(authorization)
    strict = bool(config.get("auth_required")) or config.get("provider_mode") == "new_api"
    if not strict:
        return incoming_key

    accepted = []
    provider_key = str(config.get("provider_api_key") or "").strip()
    if provider_key:
        accepted.append(provider_key)
    else:
        accepted.extend(str(value).strip() for value in config.get("allowed_keys", []) if str(value).strip())
    if not accepted and config.get("provider_mode") == "new_api":
        raise HTTPException(status_code=503, detail="Provider API key is not configured")
    if not incoming_key or incoming_key not in accepted:
        raise HTTPException(status_code=401, detail="Invalid or missing API Key")
    return incoming_key


def _validate_chat_request(payload: dict[str, Any]) -> tuple[str, bool]:
    model = payload.get("model", "lite")
    if not isinstance(model, str) or not model.strip() or len(model.strip()) > 160:
        raise HTTPException(status_code=400, detail="model must be a non-empty string")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=400, detail="messages must be a non-empty array")
    if any(not isinstance(message, dict) for message in messages):
        raise HTTPException(status_code=400, detail="messages must contain objects")
    stream = payload.get("stream", False)
    if not isinstance(stream, bool):
        raise HTTPException(status_code=400, detail="stream must be a boolean")
    if "tools" in payload and payload["tools"] is not None and not isinstance(payload["tools"], list):
        raise HTTPException(status_code=400, detail="tools must be an array")
    return model.strip(), stream


def _safe_upstream_detail(exc: Exception, fallback: str = "Qoder upstream request failed") -> str:
    status_code = _upstream_status(exc)
    if status_code in {401, 403}:
        return "Qoder upstream authentication failed"
    if status_code == 429:
        return "Qoder upstream rate limit or quota exceeded"
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return "Qoder upstream request timed out"
    return fallback


def _upstream_status(exc: Exception, default: int = 502) -> int:
    if isinstance(exc, HTTPException):
        return exc.status_code
    status_code = _status_code(exc)
    if status_code:
        return status_code
    match = re.search(r"HTTP\s+(\d{3})", str(exc), re.IGNORECASE)
    return int(match.group(1)) if match else default


# Add initial logs
add_log("Qoder2API Python Bridge initialized.")



def check_gateway_token(x_gateway_token: str | None = Header(default=None)):
    config = load_config()
    gateway_token = config.get("gateway_token", "admin")
    if not x_gateway_token or x_gateway_token != gateway_token:
        raise HTTPException(status_code=401, detail="Unauthorized gateway access")


@app.post("/ui/verify")
async def verify_gateway(payload: dict[str, Any]) -> dict[str, Any]:
    token = payload.get("token", "").strip()
    config = load_config()
    if token == config.get("gateway_token", "admin"):
        return {"status": "ok"}
    raise HTTPException(status_code=401, detail="Invalid Gateway Token")


async def get_session() -> SessionContext:
    global _local_auth_error
    data = db_load_accounts()
    if not data["accounts"]:
        # Try importing environment PAT if available
        pat = os.getenv("QODER_PAT", "").strip()
        if pat:
            add_log("No accounts stored. Importing QODER_PAT from environment...")
            try:
                sess = await create_session(pat, configured_region())
                with get_db() as conn:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO accounts (
                            uid, name, user_type, security_oauth_token, refresh_token, machine_id,
                            enabled, last_status, last_error, region
                        ) VALUES (?, ?, ?, ?, ?, ?, 1, 'ok', NULL, ?)
                        """,
                        (sess.identity.uid, sess.identity.name or "Environment PAT", sess.identity.user_type,
                         sess.identity.security_oauth_token, sess.identity.refresh_token, sess.machine_id, sess.region)
                    )
                db_set_settings("active_uid", sess.identity.uid)
                add_log(f"Imported environment PAT as account: {sess.identity.name}")
                _local_auth_error = None
            except Exception as exc:
                add_log(f"Failed to import environment PAT: {exc}", "ERROR")

        data = db_load_accounts()
        if not data["accounts"]:
            add_log("No accounts stored. Attempting to auto-import current local Qoder auth session...")
            try:
                await import_current_auth()
                add_log("Auto-imported current local Qoder session successfully.")
                _local_auth_error = None
            except Exception as exc:
                _local_auth_error = str(exc)
                add_log(f"Auto-import of local session failed: {exc}", "WARNING")

    try:
        return get_active_session()
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"No active session available: {exc}. Please configure/import an account first."
        )


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    if not env_bool("QODER_ENABLE_LANDING", True):
        raise HTTPException(status_code=404, detail="Landing page is disabled")
    return HTMLResponse(INDEX_HTML.read_text(encoding="utf-8"))


@app.get("/console", response_class=HTMLResponse)
async def console() -> HTMLResponse:
    return HTMLResponse(CONSOLE_HTML.read_text(encoding="utf-8"))


@app.get("/documents", response_class=HTMLResponse)
async def documents() -> HTMLResponse:
    if not env_bool("QODER_ENABLE_DOCUMENTS", True):
        raise HTTPException(status_code=404, detail="Documents page is disabled")
    return HTMLResponse(DOCS_HTML.read_text(encoding="utf-8"))


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Process liveness endpoint; it deliberately does not call Qoder."""
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> JSONResponse:
    """Readiness endpoint for a New API upstream channel or load balancer."""
    config = load_config()
    accounts = db_load_accounts().get("accounts", [])
    enabled_accounts = sum(1 for account in accounts if account.get("enabled", True))
    provider_key_configured = bool(
        str(config.get("provider_api_key") or "").strip()
        or any(str(value).strip() for value in config.get("allowed_keys", []))
    )
    checks = {
        "provider_key": provider_key_configured if config.get("provider_mode") == "new_api" else True,
        "enabled_accounts": enabled_accounts > 0,
    }
    ready = all(checks.values())
    body = {
        "status": "ready" if ready else "not_ready",
        "provider_mode": config.get("provider_mode", "standalone"),
        "checks": checks,
        "enabled_accounts": enabled_accounts,
    }
    return JSONResponse(status_code=200 if ready else 503, content=body)


@app.get("/ui/status")
async def status(verify: None = Depends(check_gateway_token)) -> dict[str, Any]:
    global _local_auth_error
    try:
        await get_session()
    except Exception:
        pass

    data = db_load_accounts()
    active_uid = data.get("active_uid")
    active_acc = None
    for acc in data["accounts"]:
        if acc["uid"] == active_uid:
            active_acc = acc
            break

    if active_acc is not None:
        return {
            "ready": True,
            "mode": "accounts",
            "username": active_acc["name"],
            "uid": active_acc["uid"],
            "user_type": active_acc["user_type"],
            "error": None,
            "accounts_count": len(data["accounts"])
        }
    return {
        "ready": False,
        "mode": "none",
        "username": None,
        "uid": None,
        "user_type": None,
        "error": _local_auth_error,
        "accounts_count": len(data["accounts"])
    }


@app.get("/ui/accounts")
async def get_accounts(verify: None = Depends(check_gateway_token)) -> dict[str, Any]:
    return db_load_accounts()


@app.post("/ui/accounts/import")
async def import_account(verify: None = Depends(check_gateway_token)) -> dict[str, Any]:
    try:
        acc = await import_current_auth()
        add_log(f"Imported local Qoder session account: {acc['name']}")
        return {"status": "ok", "account": acc}
    except Exception as exc:
        add_log(f"Failed to import local session account: {exc}", "ERROR")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/ui/accounts/batch-import")
async def batch_import(payload: dict[str, Any], verify: None = Depends(check_gateway_token)) -> dict[str, Any]:
    """批量导入注册机导出的 JSON：{"accounts": [{user_id, token, refresh_token, ...}]}。"""
    records = payload.get("accounts") or payload.get("records") or []
    if not isinstance(records, list) or not records:
        raise HTTPException(status_code=400, detail="accounts 数组为空")
    result = batch_import_accounts(records)
    add_log(f"Batch imported {result['imported']} accounts (skipped {result['skipped']})")
    return {"status": "ok", **result}


@app.post("/ui/accounts/select")
async def select_account(payload: dict[str, Any], verify: None = Depends(check_gateway_token)) -> dict[str, Any]:
    uid = payload.get("uid")
    if not uid:
        raise HTTPException(status_code=400, detail="uid is required")
    with get_db() as conn:
        res = conn.execute("SELECT uid FROM accounts WHERE uid = ?", (uid,)).fetchone()
        if not res:
            raise HTTPException(status_code=404, detail="Account not found")
    db_set_settings("active_uid", uid)
    add_log(f"Selected active account UID: {uid}")
    return {"status": "ok"}


@app.post("/ui/accounts/toggle")
async def toggle_account(payload: dict[str, Any], verify: None = Depends(check_gateway_token)) -> dict[str, Any]:
    uid = payload.get("uid")
    enabled = bool(payload.get("enabled", True))
    if not uid:
        raise HTTPException(status_code=400, detail="uid is required")
    enabled_val = 1 if enabled else 0
    with get_db() as conn:
        res = conn.execute("UPDATE accounts SET enabled = ? WHERE uid = ?", (enabled_val, uid))
        if res.rowcount == 0:
            raise HTTPException(status_code=404, detail="Account not found")
    add_log(f"Account toggle enabled={enabled} for UID: {uid}")
    return {"status": "ok"}


@app.post("/ui/accounts/refresh-tokens")
async def refresh_account_tokens(verify: None = Depends(check_gateway_token)) -> dict[str, Any]:
    """手动触发：刷新所有账号的 token（drt- → deviceToken/refresh）。"""
    result = refresh_all_account_tokens()
    add_log(f"Token refresh: ok={result['ok']} failed={result['failed']} total={result['total']}")
    return {"status": "ok", **result}


@app.get("/ui/accounts/quota")
async def accounts_quota(verify: None = Depends(check_gateway_token)) -> dict[str, Any]:
    """查看所有启用账号的限额（GET /api/v2/quota/usage）。"""
    return get_all_accounts_quota()


@app.delete("/ui/accounts/{uid}")
async def delete_account(uid: str, verify: None = Depends(check_gateway_token)) -> dict[str, Any]:
    with get_db() as conn:
        res = conn.execute("DELETE FROM accounts WHERE uid = ?", (uid,))
        if res.rowcount == 0:
            raise HTTPException(status_code=404, detail="Account not found")
            
    active_uid = db_get_settings("active_uid")
    if active_uid == uid:
        data = db_load_accounts()
        new_active = data["accounts"][0]["uid"] if data["accounts"] else None
        if new_active:
            db_set_settings("active_uid", new_active)
        else:
            with get_db() as conn:
                conn.execute("DELETE FROM settings WHERE key = 'active_uid'")
    add_log(f"Deleted account UID: {uid}")
    return {"status": "ok"}


@app.get("/ui/logs")
async def get_logs(verify: None = Depends(check_gateway_token)) -> list[str]:
    return list(logs_queue)


@app.get("/ui/models")
async def get_models(verify: None = Depends(check_gateway_token)) -> dict[str, Any]:
    """Return the regional model catalog plus recent gateway timings."""
    try:
        # Avoid triggering PAT/local-session auto-import just to render a page.
        sess = get_active_session()
    except Exception:
        sess = None
    region = normalize_region(sess.region if sess else configured_region())
    models, source = await discover_models(sess, region)
    metrics = get_model_metrics()
    for model in models:
        model["metrics"] = metrics["by_model"].get(model["id"], {})
    return {
        "region": region,
        "source": source,
        "models": models,
        "metrics": metrics,
        "active_uid": sess.identity.uid if sess else None,
        "active_name": sess.identity.name if sess else None,
    }


@app.post("/ui/models/benchmark")
async def benchmark_models(payload: dict[str, Any] | None = None, verify: None = Depends(check_gateway_token)) -> dict[str, Any]:
    """Benchmark selected models on the active account with a short prompt."""
    payload = payload or {}
    try:
        sess = get_active_session()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"No active account available: {exc}") from exc

    requested = payload.get("models")
    if requested is None:
        requested = []
    if not isinstance(requested, list):
        raise HTTPException(status_code=400, detail="models must be an array")
    models: list[str] = []
    for value in requested:
        model = str(value or "").strip()
        if model and model not in models:
            models.append(model[:160])
    if not models:
        catalog, _ = await discover_models(sess, sess.region)
        models = [str(item["id"]) for item in catalog[:6]]
    if len(models) > 8:
        raise HTTPException(status_code=400, detail="最多一次测试 8 个模型")

    prompt = str(payload.get("prompt") or "Reply with exactly: OK").strip()[:240]
    if not prompt:
        prompt = "Reply with exactly: OK"
    results = []
    for model in models:
        results.append(await benchmark_model(sess, model, prompt))
    ok_count = sum(1 for result in results if result.get("ok"))
    add_log(f"Model benchmark completed: {ok_count}/{len(results)} succeeded on {sess.identity.uid}")
    return {
        "status": "ok",
        "region": sess.region,
        "account_uid": sess.identity.uid,
        "account_name": sess.identity.name,
        "results": results,
        "metrics": get_model_metrics(),
    }


@app.get("/ui/api-keys/usage")
async def api_keys_usage(window_hours: int = 24, verify: None = Depends(check_gateway_token)) -> dict[str, Any]:
    return get_api_key_usage(window_hours)


@app.post("/ui/registrar/start")
async def registrar_start(payload: dict[str, Any] | None = None, verify: None = Depends(check_gateway_token)) -> dict[str, Any]:
    """启动注册机（无限循环：parents 个母线程 × 每批 3 个子任务，直到调用 stop）。

    body 可选：{"parents": 2}  —— 母线程数（1-6），每母线程 3 子任务并发。
    """
    payload = payload or {}
    try:
        parents = int(payload.get("parents", 2))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="parents 参数无效")
    return start_registration(parents=parents)


@app.post("/ui/registrar/stop")
async def registrar_stop(verify: None = Depends(check_gateway_token)) -> dict[str, Any]:
    """请求停止：当前批次完成后停止，返回本次注册统计。"""
    return stop_registration()


@app.get("/ui/registrar/status")
async def registrar_status(verify: None = Depends(check_gateway_token)) -> dict[str, Any]:
    """查询注册机任务状态（stage / logs / result）。"""
    return get_registrar_status()


@app.get("/ui/config")
async def get_ui_config(verify: None = Depends(check_gateway_token)) -> dict[str, Any]:
    return load_config()


@app.post("/ui/config")
async def post_ui_config(payload: dict[str, Any], verify: None = Depends(check_gateway_token)) -> dict[str, Any]:
    save_config(payload)
    add_log("API Key configuration updated.")
    return {"status": "ok"}


@app.post("/ui/session")
async def set_session(payload: dict[str, Any], verify: None = Depends(check_gateway_token)) -> dict[str, Any]:
    global _local_auth_error
    pat = str(payload.get("pat") or os.getenv("QODER_PAT", "")).strip()
    if not pat:
        raise HTTPException(status_code=400, detail="PAT is required")
    try:
        add_log("Attempting to save session from PAT...")
        region = normalize_region(payload.get("region") or configured_region())
        sess = await create_session(pat, region)
        
        # Insert or update in SQLite
        with get_db() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO accounts (
                    uid, name, user_type, security_oauth_token, refresh_token, machine_id,
                    enabled, last_status, last_error, region
                ) VALUES (?, ?, ?, ?, ?, ?, 1, 'ok', ?, ?)
                """,
                (sess.identity.uid, sess.identity.name or "PAT Account", sess.identity.user_type,
                 sess.identity.security_oauth_token, sess.identity.refresh_token, sess.machine_id, None, sess.region)
            )
            
        db_set_settings("active_uid", sess.identity.uid)
        
        add_log(f"Session saved from PAT. User: {sess.identity.name}")
        _local_auth_error = None
        return {"ready": True, "id": sess.identity.uid, "name": sess.identity.name, "user_type": sess.identity.user_type}
    except Exception as exc:
        msg = f"Failed to authenticate with provided PAT: {exc}"
        add_log(msg, "ERROR")
        raise HTTPException(status_code=502, detail=msg) from exc


def is_quota_error(exc: Exception) -> bool:
    """判断是否为 quota/限流类错误（429 / quota / rate limit）。
    这类错误需先查询真实限额确认，不能直接跳过账户。"""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429
    if isinstance(exc, RuntimeError):
        msg = str(exc).lower()
        return any(k in msg for k in ("http 429", "quota", "rate limit", "insufficient"))
    return False


def is_account_error(exc: Exception) -> bool:
    """判断是否'账号级'错误（token 无效/限额/服务端拒绝）。只有这类才应跳过账户。

    网络/流中断/超时（如 httpx.ReadError 的 incomplete chunk read）是临时性问题，
    换账户也无效，不应触发 rotate。
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (401, 403, 429)
    if isinstance(exc, httpx.HTTPError):
        return False  # 连接/超时/读错误等网络问题
    if isinstance(exc, RuntimeError):
        msg = str(exc).lower()
        if any(code in msg for code in ("http 401", "http 403", "http 429")):
            return True
        for kw in ("unauthorized", "invalid token", "quota", "rate limit",
                   "insufficient", "personal token", "credit"):
            if kw in msg:
                return True
    return False


@app.get("/v1/models")
async def public_models(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """OpenAI-compatible model listing for clients and the console."""
    config = load_config()
    _require_public_api_key(authorization, config)
    try:
        sess = get_active_session()
    except Exception:
        sess = None
    region = normalize_region(sess.region if sess else configured_region())
    models, _ = await discover_models(sess, region)
    created = int(time.time())
    return {
        "object": "list",
        "data": [
            {
                "id": model["id"],
                "object": "model",
                "created": created,
                "owned_by": f"qoder-{region}",
            }
            for model in models
        ],
    }


@app.api_route("/v1/responses", methods=["GET", "POST", "DELETE"])
async def unsupported_responses(authorization: str | None = Header(default=None)) -> None:
    _require_public_api_key(authorization, load_config())
    raise HTTPException(status_code=404, detail="The QoderGateway provider supports Chat Completions only")


@app.api_route("/responses/compact", methods=["POST"])
async def unsupported_compact(authorization: str | None = Header(default=None)) -> None:
    _require_public_api_key(authorization, load_config())
    raise HTTPException(status_code=404, detail="The QoderGateway provider does not support remote compaction")


@app.post("/v1/chat/completions")
async def chat_completions(
    payload: dict[str, Any],
    authorization: str | None = Header(default=None),
    x_request_id: str | None = Header(default=None, alias="X-Request-ID"),
):
    config = load_config()
    incoming_key = _require_public_api_key(authorization, config)
    model, stream = _validate_chat_request(payload)
    request_id = (x_request_id or "").strip()[:160] or uuid.uuid4().hex
    api_key_hash = api_key_fingerprint(incoming_key)
    request_source = "new_api" if config.get("provider_mode") == "new_api" else "proxy"
    messages_count = len(payload.get("messages", []))
    add_log(f"Incoming completion request: model={model}, stream={stream}, messages={messages_count}")
    accounts_data = db_load_accounts()
    enabled_count = sum(1 for acc in accounts_data["accounts"] if acc.get("enabled", True))
    max_retries = max(1, enabled_count)
    last_quota_error = False
    
    for attempt in range(max_retries):
        attempt_started = time.perf_counter()
        sess: SessionContext | None = None
        try:
            sess = await get_session()
            add_log(f"Request routing via account: {sess.identity.name} ({sess.identity.uid})")
            if stream:
                usage_state: dict[str, Any] = {}
                gen = stream_openai_response(payload, sess, usage_state)
                first_received_at: float | None = None
                try:
                    first_item = await gen.__anext__()
                    first_received_at = time.perf_counter()
                except StopAsyncIteration:
                    first_item = None

                async def stream_success_wrapper(first, g):
                    success = False
                    stream_error: Exception | None = None
                    try:
                        if first is not None:
                            yield first
                        async for chunk in g:
                            yield chunk
                        success = True
                    except Exception as exc:
                        stream_error = exc
                        raise
                    finally:
                        _safe_record_request(
                            request_id=request_id,
                            model=str(model),
                            account_uid=sess.identity.uid,
                            region=sess.region,
                            source=request_source,
                            success=success,
                            total_ms=(time.perf_counter() - attempt_started) * 1000,
                            ttft_ms=((first_received_at - attempt_started) * 1000) if first_received_at else None,
                            status_code=200 if success else _upstream_status(stream_error) if stream_error else None,
                            error=_safe_upstream_detail(stream_error) if stream_error else None,
                            api_key_hash=api_key_hash,
                            prompt_tokens=(usage_state.get("usage") or {}).get("prompt_tokens"),
                            completion_tokens=(usage_state.get("usage") or {}).get("completion_tokens"),
                            total_tokens=(usage_state.get("usage") or {}).get("total_tokens"),
                            tokens_estimated=bool(usage_state.get("estimated")),
                        )

                add_log(f"Streaming response initiated (Attempt {attempt+1}/{max_retries}).")
                return StreamingResponse(
                    stream_success_wrapper(first_item, gen),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no",
                        "X-Request-ID": request_id,
                    },
                )
            else:
                add_log(f"Generating full completion response (Attempt {attempt+1}/{max_retries})...")
                resp = await complete_openai_response(payload, sess)
                add_log("Completion request finished successfully.")
                usage = resp.get("usage") or {}
                estimated = bool(resp.pop("_qodergate_usage_estimated", False))
                _safe_record_request(
                    request_id=request_id,
                    model=str(model),
                    account_uid=sess.identity.uid,
                    region=sess.region,
                    source=request_source,
                    success=True,
                    total_ms=(time.perf_counter() - attempt_started) * 1000,
                    status_code=200,
                    api_key_hash=api_key_hash,
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    total_tokens=usage.get("total_tokens"),
                    tokens_estimated=estimated,
                )
                return JSONResponse(content=resp, headers={"X-Request-ID": request_id})
        except Exception as exc:
            current_uid = sess.identity.uid if sess is not None else "unknown"
            last_quota_error = is_quota_error(exc)
            safe_detail = _safe_upstream_detail(exc)
            _safe_record_request(
                request_id=request_id,
                model=str(model),
                account_uid=sess.identity.uid if sess is not None else None,
                region=sess.region if sess is not None else configured_region(),
                source=request_source,
                success=False,
                total_ms=(time.perf_counter() - attempt_started) * 1000,
                status_code=_upstream_status(exc),
                error=safe_detail,
                api_key_hash=api_key_hash,
            )
            if is_account_error(exc):
                if is_quota_error(exc):
                    # quota 类错误：先发一次请求确认是否真正 exceeded，而不是直接跳过
                    q = get_account_quota(current_uid)
                    if q.get("ok"):
                        quota = q["quota"]
                        truly_exceeded = bool(quota.get("isQuotaExceeded")) or (quota.get("userQuota") or {}).get("remaining", 1) <= 0
                        if not truly_exceeded:
                            add_log(f"Quota check on {current_uid}: NOT exceeded (remaining={quota.get('userQuota', {}).get('remaining')}), not rotating.", "WARNING")
                            raise HTTPException(status_code=502, detail=safe_detail)
                        add_log(f"Quota confirmed exceeded for {current_uid}: {exc}. Rotating...", "WARNING")
                    else:
                        # 限额查询失败：无法确认，保守不跳过账户
                        add_log(f"Quota check failed for {current_uid} ({q.get('error')}), not rotating.", "WARNING")
                        raise HTTPException(status_code=502, detail=safe_detail)
                else:
                    add_log(f"Account-level error on {current_uid}: {exc}. Rotating to next account...", "WARNING")
                try:
                    rotate_next_account(current_uid, str(exc))
                except Exception as e:
                    add_log(f"Failed to rotate account: {e}", "ERROR")
                    status_code = 429 if is_quota_error(exc) else 502
                    raise HTTPException(status_code=status_code, detail=safe_detail)
            else:
                add_log(f"Transient error on account {current_uid}: {exc}. Not rotating account.", "WARNING")
                status_code = 504 if isinstance(exc, (httpx.TimeoutException, TimeoutError)) else 502
                raise HTTPException(status_code=status_code, detail=safe_detail)
                
    raise HTTPException(
        status_code=429 if last_quota_error else 502,
        detail="Qoder upstream quota exceeded" if last_quota_error else "Request failed on all available accounts.",
    )


def main() -> None:
    import uvicorn

    start_refresh_loop()  # 启动 token 定时刷新线程（每 6 小时）

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.getenv("QODER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("QODER_PORT", "5050")))
    args = parser.parse_args()
    uvicorn.run("qoder2api.app:app", host=args.host, port=args.port, reload=False)

if __name__ == "__main__":
    main()
