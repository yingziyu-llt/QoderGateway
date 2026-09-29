"""Cache-token aggregation coverage for the request telemetry store."""
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch

sys.path.insert(0, os.path.abspath("src"))

from qoder2api import telemetry  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS request_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT,
    created_at REAL NOT NULL,
    model TEXT NOT NULL,
    account_uid TEXT,
    region TEXT,
    source TEXT NOT NULL DEFAULT 'proxy',
    success INTEGER NOT NULL DEFAULT 0,
    status_code INTEGER,
    ttft_ms REAL,
    total_ms REAL,
    error TEXT,
    api_key_hash TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    cached_tokens INTEGER,
    reasoning_tokens INTEGER,
    tokens_estimated INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS allowed_keys (api_key TEXT PRIMARY KEY);
"""


class TelemetryCacheTests(unittest.TestCase):
    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(prefix="qodergate-telemetry-", suffix=".db")
        os.close(handle)
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(SCHEMA)

        @contextmanager
        def temp_db():
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

        self.db_patch = patch.object(telemetry, "get_db", new=temp_db)
        self.db_patch.start()

    def tearDown(self):
        self.db_patch.stop()
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def record(self, **overrides):
        fields = {
            "model": "lite",
            "account_uid": "account-1",
            "region": "cn",
            "source": "new_api",
            "success": True,
            "total_ms": 10.0,
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "total_tokens": 110,
            "cached_tokens": 80,
        }
        fields.update(overrides)
        telemetry.record_request(**fields)

    def test_cache_tokens_are_stored_and_aggregated(self):
        self.record(request_id="a")
        self.record(request_id="b", cached_tokens=20)
        metrics = telemetry.get_model_metrics(24)
        summary = metrics["by_model"]["lite"]
        self.assertEqual(summary["cached_tokens"], 100)
        self.assertEqual(summary["cache_token_events"], 2)
        self.assertAlmostEqual(summary["cache_hit_rate"], 0.5, places=4)
        recent = {item["request_id"]: item for item in metrics["recent"]}
        self.assertEqual(recent["a"]["cached_tokens"], 80)
        self.assertEqual(recent["b"]["cached_tokens"], 20)

    def test_events_without_cache_data_are_excluded_from_the_rate(self):
        self.record(request_id="a", cached_tokens=None)
        metrics = telemetry.get_model_metrics(24)
        summary = metrics["by_model"]["lite"]
        self.assertIsNone(summary["cache_hit_rate"])
        self.assertEqual(summary["cache_token_events"], 0)
        self.assertEqual(summary["cached_tokens"], 0)

    def test_api_key_usage_reports_cache_totals(self):
        self.record(request_id="a", api_key_hash="key_abc")
        usage = telemetry.get_api_key_usage(24)
        self.assertEqual(usage["cached_tokens"], 80)
        entry = next(item for item in usage["keys"] if item["fingerprint"] == "key_abc")
        self.assertEqual(entry["cached_tokens"], 80)
        self.assertAlmostEqual(entry["cache_hit_rate"], 0.8, places=4)

    def test_reasoning_tokens_are_stored(self):
        self.record(request_id="a", reasoning_tokens=7)
        recent = telemetry.get_model_metrics(24)["recent"][0]
        self.assertEqual(recent["reasoning_tokens"], 7)


if __name__ == "__main__":
    unittest.main()
