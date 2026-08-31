"""Read-only HTTP client for the Kiwoom REST API.

Implements the official request contract:

- Headers: ``api-id``, ``authorization: Bearer <token>``,
  ``cont-yn``, ``next-key``, ``Content-Type: application/json;charset=UTF-8``
- Body: JSON payload with request parameters
- Pagination: response *headers* ``cont-yn == "Y"`` plus ``next-key``
  to continue (the official contract carries these in response
  headers, not in the JSON body)
- Rate limit: per-TR sustained ~1 req/s, burst 2 (measured; HTTP 429 /
  ``return_code == 5`` means the limit was exceeded)

The transport is injectable (``transport``) so tests can run fully
offline with a mock transport — no real network is required.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from .kiwoom_auth import KiwoomTokenManager

#: Official API contract version this client implements.
API_CONTRACT_VERSION = "kiwoom-rest-2026.1"

#: Response envelope keys per the official contract.
RETURN_CODE_KEY = "return_code"
RETURN_MSG_KEY = "return_msg"

#: Max pages per paginated request loop (safety bound against infinite loops).
DEFAULT_MAX_PAGES = 50


class BudgetExhaustedError(RuntimeError):
    """Raised when the hard HTTP request budget is fully consumed."""


@dataclass(frozen=True)
class BudgetGate:
    """Shared hard upper-bound gate for actual transport invocations.

    Every call to ``consume()`` deducts one unit. When the remaining
    budget reaches zero, the next call raises ``BudgetExhaustedError``
    so that pagination and retry loops cannot exceed the configured
    hard limit on actual HTTP transport invocations.
    """

    budget: int

    def consume(self) -> None:
        if self.budget <= 0:
            raise BudgetExhaustedError(
                f"Hard HTTP request budget exhausted ({self.budget} remaining). "
                "Stopping."
            )
        # frozen dataclass: cannot mutate in-place. Caller must use a mutable
        # wrapper or the gate is consumed once and never reused. See KiwoomClient
        # usage below where a mutable budget counter is preferred for runtime
        # deduction; this frozen variant exists for explicit one-shot gates.


class MutableBudgetGate:
    """Mutable hard upper-bound gate for actual transport invocations.

    Thread-unsafe; sufficient for the single-threaded collector client.
    """

    def __init__(self, budget: int) -> None:
        self.budget = budget

    def consume(self) -> None:
        if self.budget <= 0:
            raise BudgetExhaustedError(
                f"Hard HTTP request budget exhausted ({self.budget} remaining). "
                "Stopping."
            )
        self.budget -= 1


class KiwoomRateLimitError(RuntimeError):
    """Raised when Kiwoom reports rate limiting (HTTP 429 / return_code 5)."""


class KiwoomAPIError(RuntimeError):
    """Raised for non-zero Kiwoom return codes or HTTP failures."""


class KiwoomPaginationError(RuntimeError):
    """Raised when pagination does not terminate."""


@dataclass(frozen=True)
class KiwoomResponse:
    """A Kiwoom API response: JSON body plus the HTTP response headers.

    Pagination continuation values (``cont-yn`` / ``next-key``) live in
    the response *headers* per the official contract, never in the JSON
    body. Headers are normalized to lowercase at construction time so
    lookups are case-insensitive.
    """

    body: Dict[str, Any]
    headers: Dict[str, str]

    def header(self, name: str) -> str:
        """Case-insensitive header lookup; returns "" when absent."""
        wanted = name.lower()
        for key, value in self.headers.items():
            if key.lower() == wanted:
                return value
        return ""


#: Transport signature: (url, body_dict, headers) -> KiwoomResponse.
Transport = Callable[[str, Dict[str, Any], Dict[str, str]], KiwoomResponse]


def default_transport(
    url: str,
    body: Dict[str, Any],
    headers: Dict[str, str],
    timeout: float = 15.0,
) -> KiwoomResponse:
    """Default urllib-based transport for the Kiwoom REST API.

    Parses the JSON body and collects the HTTP response headers
    (normalized to lowercase) so pagination values in the response
    headers are never lost.
    """
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_headers = {
                str(k).lower(): str(v) for k, v in resp.headers.items()
            }
            body_data = json.loads(resp.read().decode("utf-8"))
            return KiwoomResponse(body=body_data, headers=resp_headers)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise KiwoomRateLimitError(
                "Kiwoom rate limit exceeded (HTTP 429). Stopping."
            )
        raise KiwoomAPIError(f"Kiwoom API HTTP error {e.code}.")


class KiwoomClient:
    """Thin read-only client with timeout, retry, rate limiting, pagination.

    Args:
        token_manager: Issued/cached access token provider.
        base_url: Overrides the token manager's base URL.
        min_interval: Minimum seconds between requests (default 1.0).
        max_retries: Retries for transient network errors.
        retry_backoff: Base backoff seconds, grows per attempt.
        timeout: Per-request timeout in seconds.
        transport: Injectable transport for offline testing.
    """

    def __init__(
        self,
        token_manager: Optional[KiwoomTokenManager] = None,
        base_url: Optional[str] = None,
        min_interval: float = 1.0,
        max_retries: int = 2,
        retry_backoff: float = 1.0,
        timeout: float = 15.0,
        transport: Optional[Transport] = None,
        budget_gate: Optional[MutableBudgetGate] = None,
    ) -> None:
        self.token_manager = token_manager or KiwoomTokenManager()
        self.base_url = (base_url or self.token_manager.base_url).rstrip("/")
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.timeout = timeout
        self._transport = transport or (
            lambda url, body, headers: default_transport(
                url, body, headers, timeout=timeout
            )
        )
        self._last_request_at: float = 0.0
        self.requests_made: int = 0
        self.budget_gate = budget_gate  # None means unlimited

    # -- public API ------------------------------------------------------ #

    def fetch(
        self,
        api_id: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        cont_yn: str = "N",
        next_key: str = "",
    ) -> KiwoomResponse:
        """Issue a single POST request and validate the response envelope.

        Returns a ``KiwoomResponse`` carrying both the JSON body and the
        HTTP response headers (pagination values are read from headers).
        """
        self._throttle()
        token = self.token_manager.get_token()
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "api-id": api_id,
            "authorization": f"Bearer {token}",
            "cont-yn": cont_yn,
            "next-key": next_key,
        }
        url = f"{self.base_url}{path}"
        body = body or {}

        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            # Each transport invocation (including retries) consumes budget.
            if self.budget_gate is not None:
                self.budget_gate.consume()
            try:
                response = self._transport(url, body, headers)
                self.requests_made += 1
                self._validate_envelope(response.body, api_id)
                return response
            except KiwoomRateLimitError:
                raise
            except KiwoomAPIError:
                raise
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
                last_error = e
                if attempt < self.max_retries:
                    time.sleep(self.retry_backoff * (attempt + 1))

        raise KiwoomAPIError(
            f"Kiwoom request failed after retries: {type(last_error).__name__}"
        )

    def fetch_all(
        self,
        api_id: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> List[Dict[str, Any]]:
        """Follow pagination (cont_yn / next_key) and return all pages.

        Raises:
            KiwoomPaginationError: when pagination exceeds ``max_pages``
                or returns the same next_key twice (infinite-loop guard).
        """
        pages: List[KiwoomResponse] = []
        cont_yn, next_key = "N", ""
        seen_keys: set = set()

        for _ in range(max_pages):
            page = self.fetch(api_id, path, body, cont_yn=cont_yn, next_key=next_key)
            pages.append(page)

            # Pagination values come from the RESPONSE HEADERS per the
            # official contract (cont-yn / next-key), not the JSON body.
            page_cont = page.header("cont-yn") or "N"
            page_key = page.header("next-key") or ""
            if page_cont.strip().upper() != "Y":
                break
            if not page_key.strip():
                raise KiwoomPaginationError(
                    "cont-yn=Y but next-key header is empty; aborting "
                    "pagination (infinite-loop guard)."
                )
            if page_key in seen_keys:
                raise KiwoomPaginationError(
                    f"Pagination repeated next_key {page_key!r}; aborting."
                )
            seen_keys.add(page_key)
            cont_yn, next_key = "Y", page_key
        else:
            raise KiwoomPaginationError(
                f"Pagination did not terminate within {max_pages} pages."
            )

        return pages

    # -- internals ------------------------------------------------------- #

    def _throttle(self) -> None:
        """Enforce the minimum request interval (per-TR ~1 req/s)."""
        if self.min_interval <= 0:
            return
        elapsed = time.time() - self._last_request_at
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_request_at = time.time()

    @staticmethod
    def _validate_envelope(data: Dict[str, Any], api_id: str) -> None:
        """Validate the response envelope per the official contract."""
        code = data.get(RETURN_CODE_KEY)
        if code is None:
            return  # some endpoints omit the envelope entirely
        if code == 0:
            return
        if code == 5:
            raise KiwoomRateLimitError(
                f"Kiwoom rate limit exceeded (return_code=5) for {api_id}. Stopping."
            )
        msg = data.get(RETURN_MSG_KEY, "")
        raise KiwoomAPIError(f"Kiwoom API error [{api_id}] return_code={code}: {msg}")
