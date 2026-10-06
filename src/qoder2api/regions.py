"""Qoder regional endpoints and protocol settings."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class RegionConfig:
    name: str
    openapi_url: str
    chat_url: str
    modern_chat_url: str | None
    model_list_url: str
    quota_url: str
    user_status_url: str
    web_url: str
    cosy_version: str
    data_policy: str


REGIONS = {
    "global": RegionConfig(
        name="global",
        openapi_url="https://openapi.qoder.sh",
        chat_url=(
            "https://api3.qoder.sh/algo/api/v2/service/pro/sse/"
            "agent_chat_generation?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1"
        ),
        modern_chat_url="https://api2-v2.qoder.sh/model/v1/chat/completions",
        model_list_url="https://api3.qoder.sh/algo/api/v2/model/list?Encode=1",
        quota_url="https://openapi.qoder.sh/api/v2/quota/usage",
        user_status_url="https://center.qoder.sh/algo/api/v3/user/status?Encode=1",
        web_url="https://qoder.com",
        cosy_version="0.1.43",
        data_policy="AGREE",
    ),
    "cn": RegionConfig(
        name="cn",
        openapi_url="https://openapi.qoder.com.cn",
        chat_url=(
            "https://gateway.qoder.com.cn/algo/api/v2/service/pro/sse/"
            "agent_chat_generation?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1"
        ),
        modern_chat_url=None,
        model_list_url="https://gateway.qoder.com.cn/algo/api/v2/model/list?Encode=1",
        quota_url="https://openapi.qoder.com.cn/api/v2/quota/usage",
        user_status_url="https://gateway.qoder.com.cn/algo/api/v3/user/status?Encode=1",
        web_url="https://qoder.cn",
        cosy_version="1.1.38",
        data_policy="disagree",
    ),
}


def normalize_region(value: str | None) -> str:
    """Normalize user/config input and reject unsupported regions early."""
    normalized = (value or "global").strip().lower()
    aliases = {"global": "global", "international": "global", "cn": "cn", "china": "cn"}
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise ValueError("QODER_REGION must be either 'global' or 'cn'") from exc


def configured_region() -> str:
    return normalize_region(os.getenv("QODER_REGION", "global"))


def get_region(value: str | None = None) -> RegionConfig:
    return REGIONS[normalize_region(value)]
