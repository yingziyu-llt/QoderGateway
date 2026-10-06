"""Token refresh contract tests.

These lock in the fix for CN accounts: refresh must go to the regional
OpenAPI host (``openapi.qoder.com.cn``) — never to the non-existent
``gateway.qoder.com.cn/algo/api/v3/user/refresh_token`` — and a stored PAT
must be persisted and preferred over a short-lived ``jrt-`` token.
"""

import importlib
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx


TEST_HOME = tempfile.mkdtemp(prefix="qodergate-tokens-")
os.environ["HOME"] = TEST_HOME
sys.path.insert(0, os.path.abspath("src"))

from qoder2api.database import get_db  # noqa: E402

tokens = importlib.import_module("qoder2api.tokens")
auth = importlib.import_module("qoder2api.auth")
accounts = importlib.import_module("qoder2api.accounts")


def _reset_db():
    with get_db() as conn:
        conn.execute("DELETE FROM accounts")
        conn.execute("DELETE FROM settings WHERE key = 'active_uid'")


def _fake_response(status_code=200, payload=None, text=""):
    content = json.dumps(payload).encode() if payload is not None else text.encode()
    return httpx.Response(
        status_code=status_code,
        content=content,
        headers={"Content-Type": "application/json"},
        request=httpx.Request("POST", "https://example.invalid"),
    )


def _insert(uid="uid-1", **overrides):
    values = {
        "uid": uid,
        "name": "Acct",
        "user_type": "personal_standard",
        "security_oauth_token": "jt-old",
        "refresh_token": "jrt-old",
        "machine_id": "machine-1",
        "enabled": 1,
        "region": "cn",
        "token_expires_at": None,
        "personal_access_token": None,
    }
    values.update(overrides)
    with get_db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO accounts (
                uid, name, user_type, security_oauth_token, refresh_token, machine_id,
                enabled, last_status, last_error, region, token_expires_at, personal_access_token
            ) VALUES (:uid, :name, :user_type, :security_oauth_token, :refresh_token,
                      :machine_id, :enabled, 'ok', NULL, :region, :token_expires_at,
                      :personal_access_token)
            """,
            values,
        )
    return uid


def _row(uid="uid-1"):
    with get_db() as conn:
        return dict(conn.execute("SELECT * FROM accounts WHERE uid = ?", (uid,)).fetchone())


class TimestampTests(unittest.TestCase):
    def test_iso_epoch_and_millis(self):
        self.assertEqual(
            tokens._parse_ts("2027-08-01T11:16:15Z"),
            datetime(2027, 8, 1, 11, 16, 15, tzinfo=timezone.utc).timestamp(),
        )
        self.assertEqual(tokens._parse_ts(1791300174), 1791300174.0)
        self.assertEqual(tokens._parse_ts(1791300174202), 1791300174.202)
        self.assertIsNone(tokens._parse_ts(""))
        self.assertIsNone(tokens._parse_ts(None))
        self.assertIsNone(tokens._parse_ts("not-a-date"))

    def test_expires_at_prefers_absolute_then_relative(self):
        self.assertEqual(
            tokens._expires_at_from({"expires_at": "2027-08-01T11:16:15Z"}),
            "2027-08-01T11:16:15+00:00",
        )
        soon = tokens._expires_at_from({"expires_in": 3600})
        self.assertIsNotNone(soon)
        self.assertGreater(tokens._parse_ts(soon), time.time() + 3590)
        self.assertEqual(tokens._expires_at_from({}), "")


class PatDetectionTests(unittest.TestCase):
    def test_pat_and_session_prefixes(self):
        self.assertTrue(auth.is_pat("pt-abc"))
        self.assertTrue(auth.is_pat("pat|pt-abc"))
        self.assertFalse(auth.is_pat("jrt-abc"))
        self.assertFalse(auth.is_pat(""))
        for prefix in ("dt-", "drt-", "jt-", "jrt-"):
            self.assertTrue(auth.is_session_token(prefix + "x"))
        self.assertFalse(auth.is_session_token("pt-x"))

    def test_normalize_pat(self):
        self.assertEqual(auth.normalize_pat("  pat|pt-abc  "), "pt-abc")
        self.assertEqual(auth.normalize_pat("pt-abc"), "pt-abc")


class NeedsRefreshTests(unittest.TestCase):
    def setUp(self):
        _reset_db()

    def test_missing_token_needs_refresh(self):
        self.assertTrue(tokens.account_needs_refresh({"security_oauth_token": ""}))

    def test_unknown_expiry_does_not_force_refresh(self):
        self.assertFalse(
            tokens.account_needs_refresh({"security_oauth_token": "jt-x", "token_expires_at": None})
        )

    def test_inside_skew_window_needs_refresh(self):
        soon = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
        self.assertTrue(
            tokens.account_needs_refresh({"security_oauth_token": "jt-x", "token_expires_at": soon})
        )
        later = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        self.assertFalse(
            tokens.account_needs_refresh({"security_oauth_token": "jt-x", "token_expires_at": later})
        )


class RefreshRoutingTests(unittest.TestCase):
    """The regression that made CN accounts die: wrong refresh host + lost PAT."""

    def setUp(self):
        _reset_db()

    def test_cn_job_token_refresh_uses_openapi_host(self):
        _insert(region="cn", security_oauth_token="jt-old", refresh_token="jrt-old")
        calls = []

        def fake_post(url, **kwargs):
            calls.append((url, kwargs.get("json")))
            return _fake_response(200, {"token": "jt-new", "refresh_token": "jrt-new", "expires_at": "2027-01-01T00:00:00Z"})

        with patch.object(tokens.httpx, "post", side_effect=fake_post):
            result = tokens.refresh_one_account("uid-1", force=True)

        self.assertTrue(result["ok"], result)
        self.assertEqual(len(calls), 1)
        url, body = calls[0]
        self.assertEqual(url, "https://openapi.qoder.com.cn/api/v1/jobToken/refresh")
        self.assertNotIn("gateway.qoder.com.cn", url)
        self.assertEqual(body, {"refresh_token": "jrt-old"})

        row = _row()
        self.assertEqual(row["security_oauth_token"], "jt-new")
        self.assertEqual(row["refresh_token"], "jrt-new")
        self.assertEqual(tokens._parse_ts(row["token_expires_at"]),
                         tokens._parse_ts("2027-01-01T00:00:00Z"))

    def test_stored_pat_wins_over_refresh_token(self):
        _insert(region="cn", personal_access_token="pt-root", refresh_token="jrt-old")
        calls = []

        def fake_post(url, **kwargs):
            calls.append((url, kwargs.get("json")))
            return _fake_response(200, {"token": "jt-pat", "refresh_token": "jrt-pat", "expires_at": "2027-01-01T00:00:00Z"})

        with patch.object(tokens.httpx, "post", side_effect=fake_post):
            result = tokens.refresh_one_account("uid-1", force=True)

        self.assertTrue(result["ok"], result)
        url, body = calls[0]
        self.assertEqual(url, "https://openapi.qoder.com.cn/api/v1/jobToken/exchange")
        self.assertEqual(body, {"personal_token": "pt-root"})

        row = _row()
        self.assertEqual(row["security_oauth_token"], "jt-pat")
        self.assertEqual(row["personal_access_token"], "pt-root")

    def test_pat_wrapper_prefix_is_stripped(self):
        _insert(region="cn", personal_access_token="pat|pt-root")
        calls = []

        def fake_post(url, **kwargs):
            calls.append(kwargs.get("json"))
            return _fake_response(200, {"token": "jt-pat", "refresh_token": "jrt-pat"})

        with patch.object(tokens.httpx, "post", side_effect=fake_post):
            tokens.refresh_one_account("uid-1", force=True)

        self.assertEqual(calls[0], {"personal_token": "pt-root"})
        self.assertEqual(_row()["personal_access_token"], "pt-root")

    def test_global_region_uses_global_openapi(self):
        _insert(region="global")
        calls = []

        def fake_post(url, **kwargs):
            calls.append(url)
            return _fake_response(200, {"token": "jt-new", "refresh_token": "jrt-new"})

        with patch.object(tokens.httpx, "post", side_effect=fake_post):
            tokens.refresh_one_account("uid-1", force=True)

        self.assertEqual(calls[0], "https://openapi.qoder.sh/api/v1/jobToken/refresh")

    def test_device_refresh_sends_machine_identity(self):
        _insert(region="global", refresh_token="drt-old", machine_id="machine-9")
        bodies = []

        def fake_post(url, **kwargs):
            bodies.append((url, kwargs.get("json")))
            return _fake_response(200, {"device_token": "dt-new", "refresh_token": "drt-new"})

        with patch.object(tokens.httpx, "post", side_effect=fake_post):
            result = tokens.refresh_one_account("uid-1", force=True)

        self.assertTrue(result["ok"], result)
        url, body = bodies[0]
        self.assertEqual(url, "https://openapi.qoder.sh/api/v1/deviceToken/refresh")
        self.assertEqual(body["refresh_token"], "drt-old")
        self.assertEqual(body["machine_id"], "machine-9")
        self.assertEqual(body["machine_token"], "machine-9")
        self.assertEqual(_row()["security_oauth_token"], "dt-new")

    def test_failed_refresh_records_last_error(self):
        _insert(region="cn")

        def fake_post(url, **kwargs):
            return _fake_response(401, {"errorCode": "Unauthorized"}, text="nope")

        with patch.object(tokens.httpx, "post", side_effect=fake_post):
            result = tokens.refresh_one_account("uid-1", force=True)

        self.assertFalse(result["ok"])
        row = _row()
        self.assertEqual(row["last_status"], "failed")
        self.assertIn("401", row["last_error"] or "")

    def test_no_credentials_reports_error(self):
        _insert(region="cn", refresh_token="", personal_access_token=None)
        result = tokens.refresh_one_account("uid-1", force=True)
        self.assertFalse(result["ok"])
        self.assertIn("PAT", result["error"])


class RefreshSchedulingTests(unittest.TestCase):
    def setUp(self):
        _reset_db()

    def test_only_due_accounts_are_refreshed(self):
        stale = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
        fresh = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        _insert(uid="due", token_expires_at=stale)
        _insert(uid="fresh", token_expires_at=fresh)
        refreshed = []

        def fake_refresh(uid, force=False):
            refreshed.append((uid, force))
            return {"ok": True, "uid": uid}

        with patch.object(tokens, "refresh_one_account", side_effect=fake_refresh):
            result = tokens.refresh_all_account_tokens(only_due=True)

        self.assertEqual([uid for uid, _ in refreshed], ["due"])
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["ok"], 1)

    def test_force_refreshes_everything(self):
        fresh = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        _insert(uid="fresh", token_expires_at=fresh)
        refreshed = []

        def fake_refresh(uid, force=False):
            refreshed.append((uid, force))
            return {"ok": True, "uid": uid}

        with patch.object(tokens, "refresh_one_account", side_effect=fake_refresh):
            tokens.refresh_all_account_tokens()

        self.assertEqual(refreshed, [("fresh", True)])

    def test_sleep_window_tracks_soonest_expiry(self):
        far = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        _insert(uid="far", token_expires_at=far)
        # No expiry at all -> full interval.
        self.assertEqual(tokens._next_sleep_seconds(), tokens.REFRESH_INTERVAL)

        soon = (datetime.now(timezone.utc) + timedelta(seconds=900)).isoformat()
        _insert(uid="soon", token_expires_at=soon)
        # Skew (300s) subtracted: roughly 600s, and never below the floor.
        window = tokens._next_sleep_seconds()
        self.assertGreaterEqual(window, tokens.MIN_TICK_SECONDS)
        self.assertLess(window, 700)


class ImportPersistenceTests(unittest.TestCase):
    def setUp(self):
        _reset_db()

    def test_batch_import_keeps_pat_and_region(self):
        result = accounts.batch_import_accounts([
            {
                "user_id": "uid-cn",
                "name": "CN Account",
                "pat": "pt-cn-root",
                "region": "cn",
            }
        ])
        self.assertEqual(result["imported"], 1)
        row = _row("uid-cn")
        self.assertEqual(row["personal_access_token"], "pt-cn-root")
        self.assertEqual(row["region"], "cn")

    def test_batch_import_region_defaults_to_configured(self):
        old = os.environ.get("QODER_REGION")
        os.environ["QODER_REGION"] = "cn"
        try:
            accounts.batch_import_accounts([{"user_id": "uid-x", "token": "jt-x", "refresh_token": "jrt-x"}])
            self.assertEqual(_row("uid-x")["region"], "cn")
        finally:
            if old is None:
                os.environ.pop("QODER_REGION", None)
            else:
                os.environ["QODER_REGION"] = old

    def test_import_result_hides_secrets(self):
        accounts.batch_import_accounts([{"user_id": "uid-y", "pat": "pt-secret"}])
        payload = accounts.db_load_accounts()
        for account in payload["accounts"]:
            self.assertNotIn("personal_access_token", account)
            self.assertNotIn("security_oauth_token", account)


class CreateSessionTests(unittest.TestCase):
    """PAT exchange must accept the CN response shape and remember the PAT."""

    def test_cn_exchange_normalizes_response(self):
        async def fake_exchange(personal_token, machine_id, machine_token, machine_type, region):
            self.assertEqual(personal_token, "pt-cn")
            self.assertEqual(region, "cn")
            return {
                "token": "jt-live",
                "refresh_token": "jrt-live",
                "expires_at": "2027-01-01T00:00:00Z",
                "user_id": "uid-cn",
                "user_type": "personal_standard",
            }

        with patch.object(auth, "exchange_job_token", side_effect=fake_exchange):
            import asyncio

            sess = asyncio.run(auth.create_session("pat|pt-cn", "cn"))

        self.assertEqual(sess.identity.uid, "uid-cn")
        self.assertEqual(sess.identity.security_oauth_token, "jt-live")
        self.assertEqual(sess.identity.refresh_token, "jrt-live")
        self.assertEqual(sess.identity.personal_access_token, "pt-cn")
        self.assertEqual(sess.identity.expires_at, "2027-01-01T00:00:00Z")

    def test_cn_exchange_rejects_session_tokens(self):
        import asyncio

        with self.assertRaises(ValueError):
            asyncio.run(auth.create_session("jrt-not-a-pat", "cn"))


class EnsureFreshTokenTests(unittest.TestCase):
    def setUp(self):
        _reset_db()

    def test_returns_false_when_token_is_fine(self):
        later = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        _insert(uid="uid-1", token_expires_at=later)
        with patch.object(tokens, "refresh_one_account") as refresh:
            self.assertFalse(tokens.ensure_fresh_token("uid-1"))
        refresh.assert_not_called()

    def test_returns_true_only_when_refresh_succeeded(self):
        soon = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
        _insert(uid="uid-1", token_expires_at=soon)

        with patch.object(tokens, "refresh_one_account", return_value={"ok": True, "uid": "uid-1"}):
            self.assertTrue(tokens.ensure_fresh_token("uid-1"))
        # Callers must reload the session after a True result, so a failure to
        # refresh must report False even though the token needed refreshing.
        with patch.object(tokens, "refresh_one_account", return_value={"ok": False, "error": "boom"}):
            self.assertFalse(tokens.ensure_fresh_token("uid-1"))

    def test_missing_account_is_not_an_error(self):
        self.assertFalse(tokens.ensure_fresh_token("does-not-exist"))


class GetSessionReloadTests(unittest.TestCase):
    """After a refresh the in-memory session carries the old token, so the
    request path must re-read it instead of returning the stale object."""

    def setUp(self):
        _reset_db()

    def test_returns_fresh_session_after_refresh(self):
        app_module = importlib.import_module("qoder2api.app")
        stale = type("S", (), {"identity": type("I", (), {"uid": "uid-1"})()})()
        fresh = type("S", (), {"identity": type("I", (), {"uid": "uid-1"})()})()
        seen = []

        def fake_active():
            seen.append(1)
            return stale if len(seen) == 1 else fresh

        async def run():
            with patch.object(app_module, "get_active_session", side_effect=fake_active), \
                 patch.object(app_module, "ensure_fresh_token", return_value=True):
                return await app_module.get_session()

        import asyncio

        self.assertIs(asyncio.run(run()), fresh)
        self.assertEqual(len(seen), 2)

    def test_keeps_session_when_no_refresh_needed(self):
        app_module = importlib.import_module("qoder2api.app")
        session = type("S", (), {"identity": type("I", (), {"uid": "uid-1"})()})()

        async def run():
            with patch.object(app_module, "get_active_session", return_value=session), \
                 patch.object(app_module, "ensure_fresh_token", return_value=False):
                return await app_module.get_session()

        import asyncio

        self.assertIs(asyncio.run(run()), session)


if __name__ == "__main__":
    unittest.main()
