import base64
import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any
from pathlib import Path
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import padding, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asymmetric_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from . import encoding
from .env import httpx_client_kwargs
from .regions import configured_region, get_region, normalize_region
from .signature import APPCODE, current_date, sign


SERVER_PUBKEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDA8iMH5c02LilrsERw9t6Pv5Nc
4k6Pz1EaDicBMpdpxKduSZu5OANqUq8er4GM95omAGIOPOh+Nx0spthYA2BqGz+l
6HRkPJ7S236FZz73In/KVuLnwI8JJ2CbuJap8kvheCCZpmAWpb/cPx/3Vr/J6I17
XcW+ML9FoCI6AOvOzwIDAQAB
-----END PUBLIC KEY-----"""


# Qoder PAT prefixes. ``pt-`` is the personal access token handed to users in
# the web UI (its internal name is ``personal_token``).
PAT_PREFIXES = ("pt-",)
# Session/job token prefixes. These are *not* PATs and cannot be exchanged again.
SESSION_TOKEN_PREFIXES = ("dt-", "drt-", "jt-", "jrt-")


def normalize_pat(value: str | None) -> str:
    """Strip any ``pat|<pat>`` wrapper a stored credential may carry."""
    token = (value or "").strip()
    if token.startswith("pat|"):
        parts = token.split("|", 2)
        token = parts[1].strip() if len(parts) > 1 else ""
    return token


def is_pat(value: str | None) -> bool:
    """True when the value looks like a Qoder Personal Access Token."""
    return normalize_pat(value).startswith(PAT_PREFIXES)


def is_session_token(value: str | None) -> bool:
    """True when the value is a login/job token rather than a PAT."""
    return (value or "").strip().startswith(SESSION_TOKEN_PREFIXES)


@dataclass(frozen=True)
class AuthIdentity:
    name: str
    aid: str
    uid: str
    yx_uid: str
    organization_id: str
    organization_name: str
    user_type: str
    security_oauth_token: str
    refresh_token: str
    email: str = ""
    personal_access_token: str = ""
    expires_at: str = ""


@dataclass(frozen=True)
class SessionContext:
    temp_key: bytes
    cosy_key: str
    info: str
    identity: AuthIdentity
    machine_id: str
    machine_token: str
    machine_type: str
    region: str = "global"


def new_machine(region: str | None = None) -> tuple[str, str, str]:
    region_name = normalize_region(region)
    machine_id = str(uuid.uuid4())
    if region_name == "cn":
        # The current CN Qoder client uses the machine id as its machine token
        # and the fixed COSY machine type value.
        return machine_id, machine_id, "5"
    seed = (str(uuid.uuid4()) + str(uuid.uuid4()))[:50].encode()
    machine_token = base64.urlsafe_b64encode(seed).decode().rstrip("=")
    machine_type = uuid.uuid4().hex[:18]
    return machine_id, machine_token, machine_type


def aes_cbc_pkcs7_encrypt(plain: bytes, key: bytes) -> bytes:
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plain) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key)).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def rsa_encrypt(plain: bytes) -> bytes:
    public_key = serialization.load_pem_public_key(SERVER_PUBKEY_PEM)
    return public_key.encrypt(plain, asymmetric_padding.PKCS1v15())


def auth_payload(identity: AuthIdentity) -> bytes:
    return json.dumps(
        {
            "name": identity.name,
            "aid": identity.aid,
            "uid": identity.uid,
            "yx_uid": identity.yx_uid,
            "organization_id": identity.organization_id,
            "organization_name": identity.organization_name,
            "user_type": identity.user_type,
            "security_oauth_token": identity.security_oauth_token,
            "refresh_token": identity.refresh_token,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


def new_session(
    identity: AuthIdentity,
    machine_id: str,
    machine_token: str,
    machine_type: str,
    region: str = "global",
) -> SessionContext:
    region_name = normalize_region(region)
    if region_name == "cn":
        machine_token = machine_id
        machine_type = "5"
    temp_key = uuid.uuid4().hex[:16].encode("ascii")
    cosy_key = base64.b64encode(rsa_encrypt(temp_key)).decode()
    info = base64.b64encode(aes_cbc_pkcs7_encrypt(auth_payload(identity), temp_key)).decode()
    return SessionContext(temp_key, cosy_key, info, identity, machine_id, machine_token, machine_type, region_name)


def build_payload_b64(info: str, region: str = "global") -> str:
    payload = {
        "cosyVersion": get_region(region).cosy_version,
        "ideVersion": "",
        "info": info,
        "requestId": str(uuid.uuid4()),
        "version": "v1",
    }
    raw = json.dumps(dict(sorted(payload.items())), separators=(",", ":")).encode()
    return base64.b64encode(raw).decode()


def sign_request(payload_b64: str, cosy_key: str, cosy_date: str, body: str, path_without_algo: str) -> str:
    raw = f"{payload_b64}\n{cosy_key}\n{cosy_date}\n{body}\n{path_without_algo}"
    return hashlib.md5(raw.encode()).hexdigest()


def bearer_headers(sess: SessionContext, full_url: str, body: str, accept: str, extra_headers: dict[str, str] | None = None) -> dict[str, str]:
    region = get_region(sess.region)
    path = urlparse(full_url).path
    path_sig = path[len("/algo") :] if path.startswith("/algo") else path
    payload_b64 = build_payload_b64(sess.info, sess.region)
    date = str(int(time.time()))
    sig = sign_request(payload_b64, sess.cosy_key, date, body, path_sig)
    body_bytes = body.encode()
    headers = {
        "cosy-data-policy": region.data_policy,
        "content-type": "application/json",
        "cosy-machinetype": sess.machine_type,
        "cosy-clienttype": "5",
        "cosy-date": date,
        "cosy-user": sess.identity.uid,
        "cosy-key": sess.cosy_key,
        "accept": accept,
        "cosy-clientip": "127.0.0.1" if sess.region == "cn" else "169.254.198.161",
        "authorization": f"Bearer COSY.{payload_b64}.{sig}",
        "accept-encoding": "identity",
        "cosy-version": region.cosy_version,
        "cosy-machineid": sess.machine_id,
        "cosy-machinetoken": sess.machine_token,
        "cosy-bodyhash": hashlib.md5(body_bytes).hexdigest(),
        "cosy-bodylength": str(len(body_bytes)),
        "cosy-sigpath": path_sig,
        "cosy-machineos": "x86_64_linux",
        "cosy-organization-id": "",
        "cosy-organization-tags": "",
        "login-version": "v2",
        "x-request-id": str(uuid.uuid4()),
        "user-agent": "Go-http-client/2.0",
    }
    if accept == "text/event-stream":
        headers["cache-control"] = "no-cache"
    if extra_headers:
        headers.update(extra_headers)
    return headers


async def _exchange_job_token_modern(personal_token: str, region: str) -> dict[str, Any]:
    config = get_region(region)
    headers = {
        "content-type": "application/json",
        "accept": "application/json",
        "user-agent": "qoder2api-python",
        "cosy-version": "1.0.1",
        "cosy-clienttype": "5",
    }
    async with httpx.AsyncClient(timeout=15, **httpx_client_kwargs()) as client:
        response = await client.post(
            f"{config.openapi_url}/api/v1/jobToken/exchange",
            json={"personal_token": personal_token},
            headers=headers,
        )
        if response.status_code != 200:
            raise RuntimeError(f"jobToken HTTP {response.status_code} body={response.text}")
        data = response.json()
        access_token = str(data.get("token") or "").strip()
        if not access_token:
            raise RuntimeError("jobToken response did not contain a token")

        # The exchange response normally includes user_id, but userinfo is the
        # authoritative source for the display name and account id.
        profile: dict[str, Any] = {}
        try:
            profile_response = await client.get(
                f"{config.openapi_url}/api/v1/userinfo",
                headers={
                    "authorization": f"Bearer {access_token}",
                    "accept": "application/json",
                    "user-agent": "qoder2api-python",
                    "cosy-version": "1.0.1",
                    "cosy-clienttype": "5",
                },
            )
            if profile_response.status_code == 200:
                profile = profile_response.json()
        except httpx.HTTPError:
            pass

    user_id = str(profile.get("id") or data.get("user_id") or data.get("id") or "").strip()
    if not user_id:
        raise RuntimeError("userinfo response did not contain a user id")
    return {
        "name": profile.get("name") or profile.get("username") or data.get("name") or "",
        "id": user_id,
        "userType": data.get("user_type") or data.get("userType") or "personal_standard",
        "securityOauthToken": access_token,
        "refreshToken": str(data.get("refresh_token") or "").strip(),
        "email": profile.get("email") or "",
        "expiresAt": data.get("expires_at") or "",
    }


async def _exchange_job_token_legacy(
    personal_token: str,
    machine_id: str,
    machine_token: str,
    machine_type: str,
) -> dict[str, Any]:
    """International legacy COSY/Encode exchange (``center.qoder.sh``)."""
    inner = {
        "personalToken": personal_token,
        "securityOauthToken": "",
        "refreshToken": "",
        "needRefresh": False,
        "authInfo": {},
    }
    outer = {"payload": json.dumps(inner, ensure_ascii=False, separators=(",", ":")), "encodeVersion": "1"}
    body = encoding.encode(json.dumps(outer, ensure_ascii=False, separators=(",", ":")).encode())
    date = current_date()
    headers = {
        "cosy-machinetoken": machine_token,
        "cosy-machinetype": machine_type,
        "login-version": "v2",
        "appcode": APPCODE,
        "accept": "application/json",
        "accept-encoding": "identity",
        "cosy-version": "0.1.43",
        "cosy-clienttype": "5",
        "date": date,
        "signature": sign(date),
        "content-type": "application/json",
        "cosy-machineid": machine_id,
        "user-agent": "Go-http-client/2.0",
    }
    async with httpx.AsyncClient(timeout=15, **httpx_client_kwargs()) as client:
        response = await client.post("https://center.qoder.sh/algo/api/v3/user/jobToken?Encode=1", content=body, headers=headers)
    if response.status_code != 200:
        raise RuntimeError(f"jobToken HTTP {response.status_code} body={response.text}")
    return response.json()


async def exchange_job_token(
    personal_token: str,
    machine_id: str,
    machine_token: str,
    machine_type: str,
    region: str | None = None,
) -> dict[str, Any]:
    region_name = normalize_region(region)
    if region_name == "cn":
        return await _exchange_job_token_modern(personal_token, region_name)
    return await _exchange_job_token_legacy(personal_token, machine_id, machine_token, machine_type)


def _credential_from_exchange(data: dict[str, Any], pat: str) -> AuthIdentity:
    """Normalize either exchange response shape into an :class:`AuthIdentity`.

    The CN OpenAPI returns ``{token, refresh_token, expires_at, user_id, ...}``
    while the international legacy endpoint returns ``securityOauthToken`` /
    ``refreshToken`` (and, on newer builds, ``token`` / ``refresh_token``).
    """
    access = str(
        data.get("securityOauthToken")
        or data.get("token")
        or data.get("device_token")
        or data.get("access_token")
        or ""
    ).strip()
    refresh = str(data.get("refreshToken") or data.get("refresh_token") or "").strip()
    uid = str(data.get("id") or data.get("user_id") or data.get("uid") or "").strip()
    expires_at = data.get("expires_at") or data.get("expiresAt") or ""
    return AuthIdentity(
        name=str(data.get("name") or data.get("user_name") or data.get("username") or ""),
        aid=uid,
        uid=uid,
        yx_uid="",
        organization_id="",
        organization_name="",
        user_type=str(data.get("userType") or data.get("user_type") or "personal_standard"),
        security_oauth_token=access,
        refresh_token=refresh,
        email=str(data.get("email") or ""),
        personal_access_token=pat,
        expires_at=str(expires_at),
    )


async def create_session(personal_token: str, region: str | None = None) -> SessionContext:
    region_name = normalize_region(region or configured_region())
    personal_token = normalize_pat(personal_token)
    if not personal_token:
        raise ValueError("PAT is required")
    if is_session_token(personal_token):
        raise ValueError(
            "This is a Qoder session/job token, not a PAT. Use the pt- Personal Access Token, "
            "or use Auto Import for the local Qoder session."
        )
    machine_id, machine_token, machine_type = new_machine(region_name)
    data = await exchange_job_token(personal_token, machine_id, machine_token, machine_type, region_name)
    identity = _credential_from_exchange(data, personal_token)
    if not identity.uid:
        raise RuntimeError(f"jobToken exchange did not return a user id: {str(data)[:200]}")
    if not identity.security_oauth_token:
        raise RuntimeError(f"jobToken exchange did not return a token: {str(data)[:200]}")
    return new_session(identity, machine_id, machine_token, machine_type, region_name)


def load_local_session(region: str | None = None) -> SessionContext:
    region_name = normalize_region(region or "global")
    auth_dir = Path.home() / ".qoder" / ".auth"
    id_path = auth_dir / "id"
    if not id_path.exists():
        id_path = auth_dir / "machine_id"
    user_path = auth_dir / "user"
    if not id_path.exists() or not user_path.exists():
        raise FileNotFoundError(
            "Local Qoder auth files not found. Expected ~/.qoder/.auth/{id|machine_id,user}."
        )
    
    machine_id = id_path.read_text(encoding="utf-8").strip()
    cipher_bytes = base64.b64decode(user_path.read_text(encoding="utf-8").strip())
    
    key = machine_id[:16].encode("ascii")
    
    cipher = Cipher(algorithms.AES(key), modes.CBC(key))
    decryptor = cipher.decryptor()
    padded_plain = decryptor.update(cipher_bytes) + decryptor.finalize()
    
    unpadder = padding.PKCS7(128).unpadder()
    plain = unpadder.update(padded_plain) + unpadder.finalize()
    
    data = json.loads(plain.decode("utf-8"))
    
    identity = AuthIdentity(
        name=data.get("name", ""),
        aid=data.get("id") or data.get("aid") or data.get("uid") or "",
        uid=data.get("id") or data.get("uid") or data.get("aid") or "",
        yx_uid=data.get("yx_uid") or data.get("yxUid") or "",
        organization_id=data.get("organization_id") or data.get("organizationId") or "",
        organization_name=data.get("organization_name") or data.get("organizationName") or "",
        user_type=data.get("userType") or data.get("user_type") or "personal_standard",
        security_oauth_token=data.get("securityOauthToken") or data.get("security_oauth_token") or "",
        refresh_token=data.get("refreshToken") or data.get("refresh_token") or "",
        personal_access_token=normalize_pat(
            data.get("personal_access_token") or data.get("personalAccessToken") or ""
        ),
        expires_at=str(data.get("expires_at") or data.get("expire_time") or ""),
    )
    
    _, machine_token, machine_type = new_machine(region_name)
    return new_session(identity, machine_id, machine_token, machine_type, region_name)


async def fetch_user_status(
    user_id: str,
    machine_id: str,
    machine_token: str,
    machine_type: str,
    region: str = "global",
) -> dict[str, Any]:
    region_config = get_region(region)
    inner = {
        "userId": user_id,
        "personalToken": "",
        "securityOauthToken": "",
        "refreshToken": "",
        "needRefresh": False,
        "authInfo": {},
    }
    outer = {"payload": json.dumps(inner, ensure_ascii=False, separators=(",", ":")), "encodeVersion": "1"}
    body = encoding.encode(json.dumps(outer, ensure_ascii=False, separators=(",", ":")).encode())
    date = current_date()
    headers = {
        "cosy-machinetoken": machine_token,
        "cosy-machinetype": machine_type,
        "login-version": "v2",
        "appcode": APPCODE,
        "accept": "application/json",
        "accept-encoding": "identity",
        "cosy-version": region_config.cosy_version,
        "cosy-clienttype": "5",
        "date": date,
        "signature": sign(date),
        "content-type": "application/json",
        "cosy-machineid": machine_id,
        "user-agent": "Go-http-client/2.0",
    }
    async with httpx.AsyncClient(timeout=15, **httpx_client_kwargs()) as client:
        response = await client.post(region_config.user_status_url, content=body, headers=headers)
    if response.status_code != 200:
        raise RuntimeError(f"status HTTP {response.status_code} body={response.text}")
    return response.json()
