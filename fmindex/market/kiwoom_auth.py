"""Kiwoom REST API authentication — read-only credential access.

Credentials are read from environment variables (``KIWOOM_APPKEY`` /
``KIWOOM_SECRETKEY`` / ``KIWOOM_BASE_URL``) or a local ``.env`` file.
The optional ``KIWOOM_65STOCK_ENV`` path allows read-only reuse of the
65stock reference repository's ``.env`` file (never modified).

Security rules enforced here:

- Secret values are never logged, printed, or returned as part of any
  error message. Only *presence* (bool) is ever reported.
- The token cache is read-only: we never write token files into either
  repository.
- Access tokens are held in memory only and never persisted.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Optional, Tuple

#: Environment variable names (matching the 65stock reference contract).
ENV_APP_KEY = "KIWOOM_APPKEY"
ENV_SECRET_KEY = "KIWOOM_SECRETKEY"
ENV_BASE_URL = "KIWOOM_BASE_URL"
#: Optional path to a .env file in the 65stock reference repository.
ENV_65STOCK_ENV = "KIWOOM_65STOCK_ENV"

DEFAULT_BASE_URL = "https://api.kiwoom.com"
MOCK_BASE_URL = "https://mockapi.kiwoom.com"

#: Access token endpoint — official contract (au10001 접근토큰발급).
TOKEN_URL = "/oauth2/token"

#: Renew the token when fewer than this many seconds remain.
TOKEN_RENEW_SKEW_SECONDS = 600


class KiwoomAuthError(RuntimeError):
    """Raised when Kiwoom credentials are missing or token issuance fails."""


def _parse_env_file(path: Path) -> Dict[str, str]:
    """Parse a simple KEY=VALUE .env file (stdlib only, no dotenv)."""
    result: Dict[str, str] = {}
    if not path or not path.is_file():
        return result
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def find_env_file() -> Optional[Path]:
    """Locate a .env file: explicit KIWOOM_65STOCK_ENV, or walk upward."""
    explicit = os.environ.get(ENV_65STOCK_ENV)
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return p
    current = Path(__file__).resolve().parents[3]  # project root (fmindex-kospi)
    for _ in range(4):
        candidate = current / ".env"
        if candidate.is_file():
            return candidate
        if current.parent == current:
            break
        current = current.parent
    return None


def load_credentials() -> Tuple[str, str, str]:
    """Return ``(app_key, secret_key, base_url)`` without logging values.

    Raises:
        KiwoomAuthError: when appkey/secretkey are not configured.
    """
    merged: Dict[str, str] = {}
    env_path = find_env_file()
    if env_path:
        merged.update(_parse_env_file(env_path))
    for key, value in os.environ.items():
        merged[key] = value

    app_key = merged.get(ENV_APP_KEY, "")
    secret_key = merged.get(ENV_SECRET_KEY, "")
    if not app_key or not secret_key:
        raise KiwoomAuthError(
            "Kiwoom credentials not configured. Set KIWOOM_APPKEY and "
            "KIWOOM_SECRETKEY (env or .env)."
        )
    base_url = merged.get(ENV_BASE_URL, "") or DEFAULT_BASE_URL
    return app_key, secret_key, base_url


def credentials_available() -> bool:
    """Return True when credentials are configured (values never exposed)."""
    try:
        app_key, secret_key, _ = load_credentials()
        return bool(app_key and secret_key)
    except KiwoomAuthError:
        return False


class KiwoomTokenManager:
    """Issues and caches the OAuth access token in memory only.

    The token is fetched from ``POST {base_url}/oauth2/token`` with
    grant_type=client_credentials. Expiry is parsed from ``expires_dt``
    (format YYYYMMDDHHMMSS). No token is ever written to disk.
    """

    def __init__(
        self,
        app_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 15.0,
    ) -> None:
        if app_key is None:
            app_key, secret_key, base_url = load_credentials()
        self.app_key = app_key or ""
        self.secret_key = secret_key or ""
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self._token: Optional[str] = None
        self._token_type: str = "bearer"
        self._expires_at: Optional[float] = None
        self._lock = Lock()

    # -- public API ------------------------------------------------------ #

    def get_token(self) -> str:
        """Return a valid access token, issuing a new one if needed."""
        with self._lock:
            if self._token and self._not_expired():
                return self._token
            self._token, self._token_type, self._expires_at = self._issue()
            return self._token

    def token_is_available(self) -> bool:
        """True when a token is cached and not expired (never logged)."""
        with self._lock:
            return bool(self._token and self._not_expired())

    # -- internals ------------------------------------------------------- #

    def _not_expired(self) -> bool:
        if self._expires_at is None:
            return False
        return time.time() < self._expires_at - TOKEN_RENEW_SKEW_SECONDS

    def _issue(self) -> Tuple[str, str, float]:
        """POST /oauth2/token and return (token, token_type, expires_at)."""
        if not self.app_key or not self.secret_key:
            raise KiwoomAuthError(
                "Kiwoom credentials not configured. Set KIWOOM_APPKEY and "
                "KIWOOM_SECRETKEY (env or .env)."
            )
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "secretkey": self.secret_key,
        }
        payload = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + TOKEN_URL,
            data=payload,
            headers={"Content-Type": "application/json;charset=UTF-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise KiwoomAuthError(f"Kiwoom token issuance failed (HTTP {e.code}).")
        except urllib.error.URLError as e:
            raise KiwoomAuthError(f"Kiwoom token issuance failed (network: {e.reason}).")
        except (json.JSONDecodeError, OSError) as e:
            raise KiwoomAuthError(f"Kiwoom token issuance failed (parse: {type(e).__name__}).")

        token = data.get("token") or data.get("access_token")
        if not token:
            raise KiwoomAuthError("Kiwoom token issuance failed: no token in response.")
        token_type = data.get("token_type", "bearer")
        expires_at = self._parse_expiry(data.get("expires_dt"))
        return token, token_type, expires_at

    @staticmethod
    def _parse_expiry(expires_dt: Any) -> Optional[float]:
        """Parse YYYYMMDDHHMMSS (or ISO) into an epoch timestamp."""
        if not expires_dt:
            return None
        text = str(expires_dt).strip()
        try:
            if len(text) == 14 and text.isdigit():
                dt = datetime.strptime(text, "%Y%m%d%H%M%S")
                return dt.replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            return None
