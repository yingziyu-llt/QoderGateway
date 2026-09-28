import os
from pathlib import Path


def load_dotenv() -> None:
    env_path = Path.cwd() / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv()


def env_bool(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def admin_password() -> str | None:
    value = os.getenv("QODER_ADMIN_PASSWORD", "").strip()
    return value or None


def provider_mode() -> str:
    value = os.getenv("QODER_PROVIDER_MODE", "standalone").strip().lower()
    return value if value in {"standalone", "new_api"} else "standalone"


def provider_api_key() -> str | None:
    value = os.getenv("QODER_PROVIDER_API_KEY", "").strip()
    return value or None


def provider_model_ids() -> list[str]:
    value = os.getenv("QODER_PROVIDER_MODELS", "")
    result: list[str] = []
    for item in value.split(","):
        model = item.strip()
        if model and model not in result:
            result.append(model[:160])
    return result


def proxy_url() -> str | None:
    value = os.getenv("QODER_PROXY", "").strip()
    return value or None


def httpx_client_kwargs() -> dict:
    proxy = proxy_url()
    return {"proxy": proxy} if proxy else {}
