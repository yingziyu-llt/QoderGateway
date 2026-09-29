import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .env import load_dotenv

load_dotenv()

DB_PATH = Path.home() / ".qoder" / "qoder2api.db"


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_db() as conn:
        # Accounts Table
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                uid TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                user_type TEXT,
                security_oauth_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                machine_id TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                last_status TEXT DEFAULT 'ok',
                last_error TEXT,
                quota INTEGER DEFAULT 0,
                is_quota_exceeded INTEGER DEFAULT 0,
                plan TEXT,
                user_tag TEXT,
                next_reset_at INTEGER,
                region TEXT NOT NULL DEFAULT 'global'
            )
            """
        )
        
        # Allowed API Keys Table (for proxy routing auth)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS allowed_keys (
                api_key TEXT PRIMARY KEY
            )
            """
        )
        
        # Global Settings Table
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        
        # Set default gateway token if not present
        res = conn.execute("SELECT value FROM settings WHERE key = 'gateway_token'").fetchone()
        if not res:
            default_token = os.getenv("QODER_ADMIN_PASSWORD", "admin").strip() or "admin"
            conn.execute("INSERT INTO settings (key, value) VALUES ('gateway_token', ?)", (default_token,))
            
        res_auth = conn.execute("SELECT value FROM settings WHERE key = 'auth_required'").fetchone()
        if not res_auth:
            conn.execute("INSERT INTO settings (key, value) VALUES ('auth_required', '0')")

        # token_expires_at 列（幂等：已存在则忽略）
        try:
            conn.execute("ALTER TABLE accounts ADD COLUMN token_expires_at TEXT")
        except Exception:
            pass

        # Region was added after the initial schema. Existing accounts remain
        # compatible and continue to use the international endpoint by default.
        try:
            conn.execute("ALTER TABLE accounts ADD COLUMN region TEXT NOT NULL DEFAULT 'global'")
        except Exception:
            pass

        # Recent model/request telemetry. Prompt and response bodies are never
        # stored; this table only keeps timings and routing metadata.
        try:
            conn.execute(
                """
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
                )
                """
            )
            for column, definition in (
                ("request_id", "TEXT"),
                ("api_key_hash", "TEXT"),
                ("prompt_tokens", "INTEGER"),
                ("completion_tokens", "INTEGER"),
                ("total_tokens", "INTEGER"),
                ("cached_tokens", "INTEGER"),
                ("reasoning_tokens", "INTEGER"),
                ("tokens_estimated", "INTEGER NOT NULL DEFAULT 0"),
            ):
                try:
                    conn.execute(f"ALTER TABLE request_events ADD COLUMN {column} {definition}")
                except Exception:
                    pass
            conn.execute("CREATE INDEX IF NOT EXISTS idx_request_events_created_at ON request_events(created_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_request_events_model ON request_events(model, created_at DESC)")
            try:
                conn.execute("CREATE INDEX IF NOT EXISTS idx_request_events_api_key ON request_events(api_key_hash, created_at DESC)")
            except Exception:
                pass
        except Exception:
            # A read-only legacy database must not prevent the gateway from
            # starting. telemetry.py falls back to an in-process buffer.
            pass


init_db()
