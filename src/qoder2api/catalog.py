"""Single source of truth for the Qoder model catalog.

The console / New API model listing (``models.py``) and the upstream request
translation (``bridge.py``) both read the catalog from here.  Previously each
module hard-coded its own list, so registering a new upstream model in one
place silently left the other stale -- a listed model would then be forwarded
with the wrong protocol key and rejected upstream.

Every entry carries the protocol ``key`` the official Qoder client sends as
``x-model-key`` / ``model_config.key``.  Keys for the ``cn`` region are taken
from the Qoder CN client model cache (``configs[<id>].key``); ``global`` keys
follow the client's static catalog because no live global cache is available
locally.
"""

from __future__ import annotations

import re

from .regions import normalize_region


def normalize_model_id(name: str) -> str:
    """Map an upstream display name to the catalog id style: ``Qwen3.8-Max`` -> ``qwen3.8-max``."""
    lowered = (name or "").strip().lower()
    lowered = re.sub(r"\s+", "-", lowered)
    return re.sub(r"[^a-z0-9._\-]", "", lowered)


# Advertised catalog per region.  Order is preserved in the model listing.
_CATALOG: dict[str, list[dict[str, str]]] = {
    "global": [
        {"id": "lite", "label": "Lite", "key": "lite"},
        {"id": "auto", "label": "Auto", "key": "auto"},
        {"id": "qwen3.8-max", "label": "Qwen 3.8 Max", "key": "qmodel_preview"},
        {"id": "qwen3.7-max", "label": "Qwen 3.7 Max", "key": "qmodel_latest"},
        {"id": "qwen3.7-plus", "label": "Qwen 3.7 Plus", "key": "qmodel"},
        {"id": "deepseek-v4-pro", "label": "DeepSeek V4 Pro", "key": "dmodel"},
        {"id": "deepseek-v4-flash", "label": "DeepSeek V4 Flash", "key": "dfmodel"},
        {"id": "glm-5.2", "label": "GLM 5.2", "key": "gm51model"},
        {"id": "kimi-k3", "label": "Kimi K3", "key": "kmodel_latest"},
        {"id": "kimi-k2.7-code", "label": "Kimi K2.7 Code", "key": "kmodel"},
        {"id": "minimax-m3", "label": "MiniMax M3", "key": "mmodel"},
        {"id": "cantus", "label": "Cantus", "key": "cmodel"},
    ],
    "cn": [
        {"id": "auto", "label": "Auto", "key": "auto"},
        {"id": "lite", "label": "Lite / Auto", "key": "auto"},
        {"id": "qwen3.8-max", "label": "Qwen 3.8 Max", "key": "qmodel_38max"},
        {"id": "qwen3.8-flash", "label": "Qwen 3.8 Flash", "key": "qfmodel"},
        {"id": "qwen3.7-max", "label": "Qwen 3.7 Max", "key": "qmodel_latest"},
        {"id": "qwen3.7-plus", "label": "Qwen 3.7 Plus", "key": "qmodel"},
        {"id": "qwen3.7-flash", "label": "Qwen 3.7 Flash", "key": "q37fmodel"},
        {"id": "deepseek-v4-pro", "label": "DeepSeek V4 Pro", "key": "dmodel"},
        {"id": "deepseek-flash", "label": "DeepSeek Flash", "key": "dfmodel"},
        {"id": "glm-5.3", "label": "GLM 5.3", "key": "gmodel"},
        {"id": "glm-5.3-flash", "label": "GLM 5.3 Flash", "key": "gfmodel"},
        {"id": "glm-5.2", "label": "GLM 5.2", "key": "gm51model"},
        {"id": "kimi-k3", "label": "Kimi K3", "key": "kmodel_latest"},
        {"id": "kimi-k2.8-preview", "label": "Kimi K2.8 Preview", "key": "kmodel"},
        {"id": "minimax-m2.7", "label": "MiniMax M2.7", "key": "mmodel"},
    ],
}

# Aliases for ids that were published before, or that the client still emits.
# They resolve to a live protocol key but are not advertised any more.
_LEGACY_KEYS: dict[str, dict[str, str]] = {
    "global": {
        "qwen3.7plus": "qmodel",
    },
    "cn": {
        "qwen3.7plus": "qmodel",
        "qwen3.6-flash": "q36fmodel",
        "deepseek-v4-flash": "dfmodel",
        "kimi-k2.7-code": "kmodel",
    },
}


def catalog_entries(region: str) -> list[dict[str, str]]:
    """Return the advertised catalog rows (id/label/key) for a region."""
    return [dict(entry) for entry in _CATALOG[normalize_region(region)]]


def model_key(region: str, model: str) -> str:
    """Resolve a requested model id to the upstream protocol key.

    Unknown ids pass through unchanged so custom/renamed upstream models keep
    working without a gateway release.
    """
    normalized = normalize_region(region)
    name = (model or "").strip().lower()
    for entry in _CATALOG[normalized]:
        if entry["id"] == name:
            return entry["key"]
    legacy = _LEGACY_KEYS[normalized].get(name)
    return legacy if legacy is not None else model


def cn_model_key(model: str) -> str:
    return model_key("cn", model)


def global_model_key(model: str) -> str:
    return model_key("global", model)


def parse_model_list(data: object) -> list[dict[str, str]]:
    """Parse the client model-list payload into catalog rows.

    The endpoint answers ``{"chat": [{"key", "display_name", "enable", ...}]}``;
    disabled or key-less entries are dropped, mirroring the official client.
    """
    if not isinstance(data, dict):
        return []
    chat = data.get("chat")
    if not isinstance(chat, list):
        return []
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in chat:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        display = str(item.get("display_name") or "").strip()
        if not key or not display or not item.get("enable", True):
            continue
        model_id = normalize_model_id(display)
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        result.append({"id": model_id, "label": display, "key": key})
    return result
