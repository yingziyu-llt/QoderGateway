"""
Token 刷新与限额查询（按账号区域选择 Qoder OpenAPI）

Qoder 客户端有两类可持久化凭据，刷新策略完全不同：

- **PAT**（`pt-…`，网页 UI 里的 Personal Access Token）：客户端每次刷新都拿
  PAT 重新走 `POST {openapi}/api/v1/jobToken/exchange`（`refreshPatCredential`
  / `Y6e()`）。只要 PAT 没过期，job token 永远可以续。
- **device token**（`dt-`/`drt-`）与 **job token**（`jt-`/`jrt-`）：走
  `POST {openapi}/api/v1/deviceToken/refresh` 或 `/api/v1/jobToken/refresh`，
  body 为 `{"refresh_token": "<drt-…|jrt-…>"}`；jrt 刷新不带 machine 字段，
  drt 刷新额外带 `machine_id` / `machine_token`。

所以刷新优先级是：**有 PAT 就用 PAT 重新兑换**，否则才用 refresh_token。

- 刷新：`POST {openapi}/api/v1/jobToken/exchange`（PAT）
        `POST {openapi}/api/v1/jobToken/refresh`（jrt-）
        `POST {openapi}/api/v1/deviceToken/refresh`（drt-）
- 限额：GET /api/v2/quota/usage
- 后台线程：按 `token_expires_at` 到期前刷新全部 enabled 账号
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from .auth import normalize_pat
from .database import get_db
from .env import httpx_client_kwargs
from .regions import get_region, normalize_region

UA = "qoder/1.1.65"
REFRESH_INTERVAL = 6 * 3600  # 兜底轮询间隔：6 小时
# 后台线程的最短休眠，避免配置异常时忙等。
MIN_TICK_SECONDS = 60
# 到期前多久视为「该刷新了」。
EXPIRY_SKEW_SECONDS = 300


def _headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": UA,
    }


def _parse_ts(value: Any) -> float | None:
    """Accept epoch seconds / epoch millis / ISO-8601 and return epoch seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
    if number <= 0:
        return None
    # 客户端把 `expire_time` 归一化成秒；这里兼容毫秒时间戳。
    if number > 1e11:
        number /= 1000.0
    return number


def _expires_at_from(d: dict[str, Any]) -> str:
    """Serialize whichever expiry field the upstream returned."""
    for key in ("expires_at", "expire_time", "expiresAt", "expireTime"):
        value = d.get(key)
        if value:
            ts = _parse_ts(value)
            if ts is not None:
                return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            return str(value)
    for key in ("expires_in", "expire_in", "expiresIn"):
        value = d.get(key)
        if value in (None, ""):
            continue
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        return datetime.fromtimestamp(time.time() + seconds, tz=timezone.utc).isoformat()
    return ""


def exchange_pat(pat: str, region: str) -> dict[str, Any]:
    """PAT → job token，与 Qoder 客户端 `exchangePersonalToken` 等价。"""
    config = get_region(region)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": UA,
        "Cosy-Version": "1.0.1",
        "Cosy-ClientType": "5",
    }
    response = httpx.post(
        f"{config.openapi_url}/api/v1/jobToken/exchange",
        json={"personal_token": normalize_pat(pat)},
        headers=headers,
        timeout=25,
        **httpx_client_kwargs(),
    )
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
    return response.json()


def _save_refreshed_tokens(
    uid: str,
    data: dict[str, Any],
    fallback_refresh: str,
    pat: str = "",
    machine_id: str | None = None,
) -> dict[str, Any]:
    new_tok = str(
        data.get("token")
        or data.get("device_token")
        or data.get("securityOauthToken")
        or ""
    ).strip()
    if not new_tok:
        return {"ok": False, "uid": uid, "error": "响应缺少 token"}
    new_rt = str(data.get("refresh_token") or data.get("refreshToken") or fallback_refresh).strip()
    expires_at = _expires_at_from(data)

    assignments = [
        "security_oauth_token = ?",
        "refresh_token = ?",
        "token_expires_at = ?",
        "last_status = 'ok'",
        "last_error = NULL",
    ]
    params: list[Any] = [new_tok, new_rt, expires_at]
    if pat:
        assignments.append("personal_access_token = ?")
        params.append(normalize_pat(pat))
    if machine_id:
        assignments.append("machine_id = ?")
        params.append(machine_id)
    params.append(uid)
    with get_db() as conn:
        conn.execute(f"UPDATE accounts SET {', '.join(assignments)} WHERE uid = ?", params)
    return {"ok": True, "uid": uid, "expires_at": expires_at}


def _fetch_account_row(uid: str) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT uid, name, refresh_token, security_oauth_token, personal_access_token, "
            "machine_id, region, token_expires_at FROM accounts WHERE uid = ?",
            (uid,),
        ).fetchone()
    return dict(row) if row else None


def refresh_refresh_token(row: dict[str, Any], region: str) -> dict[str, Any]:
    """用 refresh_token 换新（jrt- → jobToken/refresh，drt- → deviceToken/refresh）。"""
    rt = (row.get("refresh_token") or "").strip()
    if not rt:
        raise RuntimeError("无 refresh_token")
    config = get_region(region)
    if rt.startswith("drt-"):
        url = f"{config.openapi_url}/api/v1/deviceToken/refresh"
        machine_id = str(row.get("machine_id") or "").strip()
        body: dict[str, Any] = {"refresh_token": rt}
        if machine_id:
            # deviceToken 刷新必须带 machine 身份，且 CN 客户端的 machine_token
            # 就是 machine_id 本身。
            body["machine_id"] = machine_id
            body["machine_token"] = machine_id
    else:
        url = f"{config.openapi_url}/api/v1/jobToken/refresh"
        body = {"refresh_token": rt}
    response = httpx.post(url, json=body, headers=_headers(), timeout=25, **httpx_client_kwargs())
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
    return response.json()


def refresh_one_account(uid: str, force: bool = False) -> dict[str, Any]:
    """刷新单个账号。

    优先用 PAT 重新兑换（PAT 不会过期），否则回退到 refresh_token。
    ``force=False`` 时若 token 仍未临近过期则直接跳过。
    """
    row = _fetch_account_row(uid)
    if not row:
        return {"ok": False, "uid": uid, "error": "账号不存在"}

    if not force and not account_needs_refresh(row):
        return {"ok": True, "uid": uid, "name": row.get("name"), "skipped": True}

    region = normalize_region(row.get("region"))
    pat = normalize_pat(row.get("personal_access_token"))
    rt = (row.get("refresh_token") or "").strip()

    try:
        if pat:
            data = exchange_pat(pat, region)
            result = _save_refreshed_tokens(uid, data, rt, pat=pat)
        elif rt:
            data = refresh_refresh_token(row, region)
            result = _save_refreshed_tokens(uid, data, rt)
        else:
            return {"ok": False, "uid": uid, "error": "既无 PAT 也无 refresh_token"}
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        with get_db() as conn:
            conn.execute(
                "UPDATE accounts SET last_status = 'failed', last_error = ? WHERE uid = ?",
                (str(exc)[:300], uid),
            )
        return {"ok": False, "uid": uid, "name": row.get("name"), "error": str(exc)}

    result["name"] = row.get("name")
    return result


def account_needs_refresh(row: dict[str, Any], skew: int = EXPIRY_SKEW_SECONDS) -> bool:
    """token 缺失或已进入到期窗口时返回 True。"""
    token = str(row.get("security_oauth_token") or "").strip()
    if not token:
        return True
    expires_at = _parse_ts(row.get("token_expires_at"))
    if expires_at is None:
        return False
    return time.time() >= expires_at - skew


def refresh_due_accounts(skew: int = EXPIRY_SKEW_SECONDS) -> dict[str, Any]:
    """刷新所有进入到期窗口的 enabled 账号。"""
    return refresh_all_account_tokens(only_due=True, skew=skew)


def refresh_all_account_tokens(only_due: bool = False, skew: int = EXPIRY_SKEW_SECONDS) -> dict[str, Any]:
    """刷新 enabled 账号；``only_due`` 时只刷新临近过期的。"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT uid, security_oauth_token, token_expires_at FROM accounts WHERE enabled = 1"
        ).fetchall()
    candidates = [dict(r) for r in rows if not only_due or account_needs_refresh(dict(r), skew)]
    results = [refresh_one_account(r["uid"], force=not only_due) for r in candidates]
    ok = sum(1 for x in results if x.get("ok"))
    return {
        "ok": ok,
        "failed": len(results) - ok,
        "total": len(results),
        "results": results,
    }


def ensure_fresh_token(uid: str) -> bool:
    """请求前惰性刷新：仅在 token 临近过期时同步刷新一次。

    返回是否真的刷新了（调用方需据此重新读取 session，否则会继续用旧 token）。
    刷新失败不抛出。
    """
    row = _fetch_account_row(uid)
    if not row or not account_needs_refresh(row):
        return False
    result = refresh_one_account(uid)
    return bool(result.get("ok"))


def get_account_quota(uid: str) -> dict[str, Any]:
    """Query one account's regional quota endpoint."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT uid, name, security_oauth_token, region FROM accounts WHERE uid = ?", (uid,)
        ).fetchone()
    if not row:
        return {"ok": False, "uid": uid, "error": "账号不存在"}
    tok = row["security_oauth_token"] or ""
    if not tok:
        return {"ok": False, "uid": uid, "error": "无 token"}
    region = normalize_region(row["region"] if "region" in row.keys() else "global")
    try:
        r = httpx.get(
            get_region(region).quota_url,
            headers={
                "Authorization": f"Bearer {tok}",
                "Accept": "application/json",
                "User-Agent": UA,
            },
            timeout=20,
            **httpx_client_kwargs(),
        )
    except httpx.HTTPError as e:
        return {"ok": False, "uid": uid, "error": f"网络错误: {e}"}
    if r.status_code != 200:
        return {"ok": False, "uid": uid, "error": f"HTTP {r.status_code}: {r.text[:160]}"}
    return {"ok": True, "uid": uid, "name": row["name"], "quota": r.json()}


def get_all_accounts_quota() -> dict[str, Any]:
    """查询所有 enabled 账号的限额。"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT uid, name FROM accounts WHERE enabled = 1 AND security_oauth_token IS NOT NULL AND security_oauth_token != ''"
        ).fetchall()
    quotas = [get_account_quota(r["uid"]) for r in rows]
    return {"total": len(quotas), "quotas": quotas}


# ---------------------------------------------------------------------------
# 后台定时刷新
# ---------------------------------------------------------------------------
_refresh_thread: threading.Thread | None = None
_refresh_lock = threading.Lock()
_stop_event = threading.Event()

# 测试可以通过这个钩子观察后台线程行为。
_refresh_hook = None


def _next_sleep_seconds() -> float:
    """按最近一个到期时间决定休眠时长（上限 REFRESH_INTERVAL）。"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT token_expires_at FROM accounts WHERE enabled = 1 AND token_expires_at IS NOT NULL"
        ).fetchall()
    soonest: float | None = None
    for row in rows:
        ts = _parse_ts(row[0])
        if ts is None:
            continue
        soonest = ts if soonest is None else min(soonest, ts)
    if soonest is None:
        return REFRESH_INTERVAL
    remaining = (soonest - EXPIRY_SKEW_SECONDS) - time.time()
    return max(MIN_TICK_SECONDS, min(REFRESH_INTERVAL, remaining))


def _refresh_loop() -> None:
    while not _stop_event.is_set():
        # 等到期，再刷新；没有到期信息时按 REFRESH_INTERVAL 兜底。
        if _stop_event.wait(_next_sleep_seconds()):
            return
        try:
            result = refresh_due_accounts()
            if result["total"] or result["failed"]:
                _log_refresh(result)
        except Exception:
            pass


def _log_refresh(result: dict[str, Any]) -> None:
    try:
        from .app import add_log

        add_log(f"Token refresh: ok={result['ok']} failed={result['failed']} total={result['total']}")
    except Exception:
        pass


def start_refresh_loop() -> None:
    """启动后台定时刷新线程（幂等）。"""
    global _refresh_thread
    with _refresh_lock:
        _stop_event.clear()
        if _refresh_thread is None or not _refresh_thread.is_alive():
            _refresh_thread = threading.Thread(target=_refresh_loop, daemon=True)
            _refresh_thread.start()


def stop_refresh_loop() -> None:
    """停止后台刷新线程（测试用）。"""
    _stop_event.set()
