"""Capture the raw upstream usage payload to verify prompt-cache fields.

Usage: uv run python scripts/probe_cache_usage.py [--raw FILE]

Sends the same long, stable prefix twice on each protocol path (the active
account's regional legacy endpoint and the modern OpenAI-compatible one) and
prints every upstream SSE line that mentions usage/cache/tokens.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, os.path.abspath("src"))

from qoder2api.accounts import get_active_session  # noqa: E402
from qoder2api.bridge import build_qoder_body, qoder_stream_lines  # noqa: E402

STABLE_PREFIX = (
    "You are a careful assistant. Follow the project conventions strictly. "
    "Here is a long, stable context that should be cacheable across requests: "
) + ("The quick brown fox jumps over the lazy dog. " * 200)

INTERESTING = ("usage", "cache", "token", "response_meta", "raw_usage", "rawUsage")


def req_body(tag: str) -> dict:
    return {
        "model": "lite",
        "stream": True,
        "stream_options": {"include_usage": True},
        "messages": [
            {"role": "system", "content": STABLE_PREFIX},
            {"role": "user", "content": f"Reply with exactly one word: {tag}"},
        ],
    }


async def probe(label: str, sess, tag: str) -> list[str]:
    body, model, _ = build_qoder_body(req_body(tag), sess)
    print(f"\n===== {label} (region={sess.region}, model={model}) =====")
    interesting: list[str] = []
    try:
        async for line in qoder_stream_lines(sess, body, model):
            lowered = line.lower()
            if any(key.lower() in lowered for key in INTERESTING):
                print(line[:4000])
                interesting.append(line)
    except Exception as exc:  # noqa: BLE001 - probe must survive upstream errors
        print(f"[{label}] request failed: {exc!r}")
    if not interesting:
        print(f"[{label}] no usage/cache-bearing line observed")
    return interesting


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", default=None, help="write captured lines to this JSON file")
    args = parser.parse_args()

    session = get_active_session()
    print(f"account={session.identity.name} uid={session.identity.uid} region={session.region}")
    global_session = replace(session, region="global")

    captured: dict[str, list[str]] = {}
    for round_index in (1, 2):
        captured[f"regional-{round_index}"] = await probe(f"regional round {round_index}", session, f"hit{round_index}")
        captured[f"global-{round_index}"] = await probe(f"global round {round_index}", global_session, f"hit{round_index}")

    if args.raw:
        Path(args.raw).write_text(json.dumps(captured, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\ncaptured lines written to {args.raw}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
