"""Lightweight bearer-token auth for the admin area."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))

_SECRET_FILE = DATA_DIR / "auth_secret.key"
_ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
TOKEN_TTL = 12 * 3600


def _secret() -> bytes:
    if _SECRET_FILE.exists():
        return _SECRET_FILE.read_bytes()
    _SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    s = secrets.token_bytes(32)
    _SECRET_FILE.write_bytes(s)
    return s


def admin_enabled() -> bool:
    return bool(_ADMIN_PASSWORD)


def _sign(payload_b64: str) -> str:
    return hmac.new(_secret(), payload_b64.encode(), hashlib.sha256).hexdigest()


def make_token() -> str:
    payload = {"exp": int(time.time()) + TOKEN_TTL, "n": secrets.token_hex(4)}
    p64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    return f"{p64}.{_sign(p64)}"


def verify_token(token: str) -> bool:
    if not token:
        return False
    try:
        p64, sig = token.split(".", 1)
        if not hmac.compare_digest(_sign(p64), sig):
            return False
        payload = json.loads(base64.urlsafe_b64decode(p64))
        return bool(payload.get("exp", 0) > time.time())
    except Exception:
        return False


def check_admin_password(password: str) -> bool:
    if not _ADMIN_PASSWORD:
        return False
    return hmac.compare_digest(str(password), _ADMIN_PASSWORD)
