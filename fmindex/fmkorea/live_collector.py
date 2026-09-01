"""Low-frequency FMKorea stock-board live collector.

Collects a small, bounded set of public FMKorea stock-board pages:

- robots.txt policy check (max 1 request)
- board list (max 1 request)
- post detail pages (max 3 requests)

Concurrency is 1, requests are spaced at least 3 seconds apart, automatic
retries are 0, and 403/429/CAPTCHA abort the whole run immediately. No
cookies, no login, no proxy, no UA rotation. GET only.

Outputs:

- gitignored ``run/fmkorea-live-<date>/raw/`` — raw HTML evidence
- gitignored ``run/fmkorea-live-<date>/private/posts.jsonl`` — normalized
  posts (author/nickname fields are never stored)
- ``sanitized-report.json`` — counts and statuses only
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .live_models import LiveComment, LivePost, normalize_text, now_kst
from .live_parser import parse_live_list, parse_live_post

KST = timezone(timedelta(hours=9))

#: Honest user agent; never swapped for a browser-like UA on refusal.
USER_AGENT = "FMIndexResearch/0.1 (low-frequency public-page validation)"

#: Board URLs.
BOARD_URL = "https://www.fmkorea.com/stock"
ROBOTS_URL = "https://www.fmkorea.com/robots.txt"

#: Request budget.
MAX_ROBOTS_CALLS = 1
MAX_LIST_CALLS = 1
MAX_POST_CALLS = 3
MAX_COMMENT_CALLS = 1
MAX_CONTENT_CALLS = 5  # list + posts + optional comment page; does NOT include robots

#: Safety thresholds — enforced at construction time.
MIN_REQUEST_DELAY = 3.0
MAX_LIST_BOUNDS = 1
MAX_POSTS_BOUNDS = 3
MAX_COMMENT_BOUNDS = 1

#: CAPTCHA / security-check page markers.
_CAPTCHA_RE = ("captcha", "reCAPTCHA", "g-recaptcha", "security check", "자동입력방지")


class FMKoreaRobotsBlocked(RuntimeError):
    """robots.txt explicitly forbids /stock collection."""


class FMKoreaForbiddenError(RuntimeError):
    """HTTP 403 — abort, no retries, no alternate paths."""


class FMKoreaRateLimitError(RuntimeError):
    """HTTP 429 — abort immediately, zero retries."""


class FMKoreaCaptchaError(RuntimeError):
    """HTTP 200 but a CAPTCHA/security page — abort."""


class FMKoreaDeletedPostError(RuntimeError):
    """The post is deleted; classified, never treated as a normal post."""


class FMKoreaUnexpectedContentError(RuntimeError):
    """HTTP 200 but the body is not the expected board/post DOM."""


class FMKoreaRequestBudgetExceeded(RuntimeError):
    """The configured request budget would be exceeded."""


class FMKoreaSafetyValidationError(ValueError):
    """Constructor argument violates safety invariants."""


@dataclass
class FetchResult:
    """Classified result of a single HTTP fetch."""

    status: str  # ok | forbidden | rate_limited | deleted | captcha | unexpected_content | network_error
    html: str = ""
    http_status: Optional[int] = None


def classify_http(http_status: Optional[int], html: str = "") -> str:
    """Classify an HTTP response into a status label (contract: no mixing).

    Unknown 4xx and all 5xx are fail-closed — never classified as ``ok``.
    """
    if http_status is None:
        return "network_error"
    if 200 <= http_status < 300:
        low = html[:2000].lower()
        if any(m in low for m in _CAPTCHA_RE):
            return "captcha"
        return "ok"
    if http_status == 400:
        return "client_error"
    if http_status == 401:
        return "forbidden"
    if http_status == 403:
        return "forbidden"
    if http_status == 404:
        return "deleted"
    if http_status == 405:
        return "client_error"
    if http_status == 410:
        return "deleted"
    if http_status == 429:
        return "rate_limited"
    if http_status == 451:
        return "client_error"
    if 500 <= http_status < 600:
        return "server_error"
    # Any other unexpected status is fail-closed.
    return "client_error"


class LiveFMKoreaCollector:
    """Bounded, low-frequency FMKorea stock-board collector."""

    def __init__(
        self,
        transport: Optional[Callable[[str, Dict[str, str], str, float], FetchResult]] = None,
        request_delay: float = MIN_REQUEST_DELAY,
        max_list: int = MAX_LIST_BOUNDS,
        max_posts: int = MAX_POSTS_BOUNDS,
        max_comment: int = MAX_COMMENT_BOUNDS,
        save_raw: bool = True,
    ) -> None:
        # --- Hard safety invariants (fail-fast) ------------------------- #
        if not isinstance(request_delay, (int, float)) or request_delay < MIN_REQUEST_DELAY:
            raise FMKoreaSafetyValidationError(
                f"request_delay must be >= {MIN_REQUEST_DELAY}; got {request_delay!r}"
            )
        if not isinstance(max_list, int) or max_list < 0 or max_list > MAX_LIST_BOUNDS:
            raise FMKoreaSafetyValidationError(
                f"max_list must be 0..{MAX_LIST_BOUNDS}; got {max_list!r}"
            )
        if not isinstance(max_posts, int) or max_posts < 0 or max_posts > MAX_POSTS_BOUNDS:
            raise FMKoreaSafetyValidationError(
                f"max_posts must be 0..{MAX_POSTS_BOUNDS}; got {max_posts!r}"
            )
        if not isinstance(max_comment, int) or max_comment < 0 or max_comment > MAX_COMMENT_BOUNDS:
            raise FMKoreaSafetyValidationError(
                f"max_comment must be 0..{MAX_COMMENT_BOUNDS}; got {max_comment!r}"
            )

        self._transport = transport or self._default_transport
        self.request_delay = float(request_delay)
        self.max_list = max_list
        self.max_posts = max_posts
        self.max_comment = max_comment
        self.save_raw = save_raw

        # --- Counters --------------------------------------------------- #
        self.requests_made = 0
        self.robots_calls = 0
        self.list_calls = 0
        self.content_calls = 0
        self.robots_status = ""
        self.robots_allows_stock = True
        self.list_status = ""
        self.http_403 = 0
        self.http_429 = 0
        self.captcha_count = 0
        self.deleted_posts = 0
        self.unexpected_content = 0
        self.posts_discovered = 0
        self.posts_selected = 0
        self.posts_parsed = 0
        self.comments_parsed = 0
        self.replies_parsed = 0
        self._last_request_at = 0.0

    # -- transport ---------------------------------------------------------- #
    @staticmethod
    def _default_transport(
        url: str,
        headers: Dict[str, str],
        _body: str,
        timeout: float,
    ) -> FetchResult:
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                html = resp.read().decode("utf-8", errors="replace")
                return FetchResult(status=classify_http(resp.status, html), html=html, http_status=resp.status)
        except urllib.error.HTTPError as e:
            return FetchResult(status=classify_http(e.code), http_status=e.code)
        except (urllib.error.URLError, TimeoutError, OSError):
            return FetchResult(status="network_error")

    def _throttle(self) -> None:
        wait = self.request_delay - (time.time() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.time()

    def _headers(self) -> Dict[str, str]:
        return {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "ko-KR,ko;q=0.9",
        }

    def _fetch(self, url: str, label: str) -> FetchResult:
        self._throttle()
        result = self._transport(url, self._headers(), "", 15.0)
        self.requests_made += 1
        if result.status == "forbidden":
            self.http_403 += 1
        elif result.status == "rate_limited":
            self.http_429 += 1
        elif result.status == "captcha":
            self.captcha_count += 1
        elif result.status == "deleted":
            self.deleted_posts += 1
        elif result.status in ("unexpected_content", "server_error", "client_error"):
            self.unexpected_content += 1
        return result

    def _fetch_content(self, url: str, label: str) -> FetchResult:
        """Fetch a content URL (list/post/comment) with budget enforcement."""
        if self.content_calls >= MAX_CONTENT_CALLS:
            raise FMKoreaRequestBudgetExceeded(f"content budget exhausted before {label}")
        result = self._fetch(url, label)
        self.content_calls += 1
        return result

    # -- robots ------------------------------------------------------------- #
    def check_robots(self) -> str:
        """Check robots.txt once. Returns 'allowed' | 'blocked' | 'unresolved'."""
        if self.robots_calls >= MAX_ROBOTS_CALLS:
            raise FMKoreaRequestBudgetExceeded("robots budget exhausted")
        result = self._fetch(ROBOTS_URL, "robots")
        self.robots_calls += 1
        # Abort immediately on forbidden/rate_limited/captcha — same as any other endpoint.
        if result.status == "forbidden":
            raise FMKoreaForbiddenError("HTTP 403 on robots.txt")
        if result.status == "rate_limited":
            raise FMKoreaRateLimitError("HTTP 429 on robots.txt")
        if result.status == "captcha":
            raise FMKoreaCaptchaError("CAPTCHA on robots.txt")
        body = result.html.lower()
        self.robots_status = result.status
        if result.status != "ok" or not body:
            self.robots_allows_stock = True  # ambiguous: proceed under budget limits
            return "unresolved"
        # Parse simple User-agent/disallow rules for the /stock path.
        stock_blocked = "/stock" in body and "disallow: /stock" in body
        self.robots_allows_stock = not stock_blocked
        if stock_blocked:
            raise FMKoreaRobotsBlocked("robots.txt disallows /stock")
        return "allowed"

    # -- list --------------------------------------------------------------- #
    def fetch_list(self, raw_dir: Path) -> List[LivePost]:
        """Fetch the board list once and return post stubs."""
        if self.list_calls >= self.max_list:
            raise FMKoreaRequestBudgetExceeded("list budget exhausted")
        result = self._fetch_content(BOARD_URL, "list")
        self.list_calls += 1
        self.list_status = result.status
        if result.status == "forbidden":
            raise FMKoreaForbiddenError("HTTP 403 on board list")
        if result.status == "rate_limited":
            raise FMKoreaRateLimitError("HTTP 429 on board list")
        if result.status == "captcha":
            raise FMKoreaCaptchaError("CAPTCHA on board list")
        if result.status == "deleted":
            raise FMKoreaDeletedPostError("board list returned deleted")
        if result.status in ("client_error", "server_error", "unexpected_content", "network_error"):
            raise FMKoreaUnexpectedContentError(f"board list: {result.status}")
        if self.save_raw:
            raw_dir.mkdir(parents=True, exist_ok=True)
            (raw_dir / "list.html").write_text(result.html, encoding="utf-8")
        posts = parse_live_list(result.html)
        self.posts_discovered = len(posts)
        return posts

    # -- posts -------------------------------------------------------------- #
    def fetch_posts(self, stubs: List[LivePost], raw_dir: Path) -> List[LivePost]:
        """Fetch up to ``max_posts`` selected post detail pages."""
        selected = self._select_posts(stubs)
        self.posts_selected = len(selected)
        collected: List[LivePost] = []
        for i, stub in enumerate(selected[: self.max_posts], start=1):
            url = stub.canonicalUrl or f"{BOARD_URL}/?document_srl={stub.sourcePostId}"
            result = self._fetch_content(url, f"post-{i}")
            if result.status == "network_error":
                raise FMKoreaUnexpectedContentError(f"network error on post {stub.sourcePostId}")
            if result.status == "forbidden":
                raise FMKoreaForbiddenError(f"HTTP 403 on post detail {stub.sourcePostId}")
            if result.status == "rate_limited":
                raise FMKoreaRateLimitError(f"HTTP 429 on post detail {stub.sourcePostId}")
            if result.status == "captcha":
                raise FMKoreaCaptchaError("CAPTCHA on post detail")
            if result.status == "deleted":
                # Counter already incremented by _fetch() — do NOT increment again.
                continue
            if result.status in ("client_error", "server_error", "unexpected_content"):
                raise FMKoreaUnexpectedContentError(f"HTTP {result.http_status} on post {stub.sourcePostId}")
            if self.save_raw:
                (raw_dir / f"post-{i}.html").write_text(result.html, encoding="utf-8")
            post = parse_live_post(result.html, source_post_id=stub.sourcePostId)
            if post is None:
                self.unexpected_content += 1
                continue
            if post.deleted:
                self.deleted_posts += 1
            post.firstSeenAt = stub.firstSeenAt
            self.comments_parsed += len(post.comments)
            self.replies_parsed += sum(1 for c in post.comments if c.depth >= 1)
            collected.append(post)
        self.posts_parsed = len(collected)
        return collected

    def _select_posts(self, stubs: List[LivePost]) -> List[LivePost]:
        """Select up to max_posts normal, non-notice posts.

        Priority: regular posts, then a post with >=1 comment, then a post
        with no comments. Notices/advertisements are excluded.
        """
        notice_tokens = ("공지", "notice", "광고", "ad ")
        normal = [
            p for p in stubs
            if p.title
            and not any(t in p.title.lower() for t in notice_tokens)
        ]
        with_comments = [p for p in normal if p.commentCount > 0]
        without_comments = [p for p in normal if p.commentCount == 0]
        ordered: List[LivePost] = []
        seen: set = set()
        for pool in (normal, with_comments, without_comments):
            for p in pool:
                if p.sourcePostId in seen:
                    continue
                seen.add(p.sourcePostId)
                ordered.append(p)
                if len(ordered) >= self.max_posts:
                    return ordered
        return ordered[: self.max_posts]

    # -- dedup -------------------------------------------------------------- #
    @staticmethod
    def dedup(posts: List[LivePost]) -> Dict[str, Any]:
        """Dedup by sourcePostId, canonicalUrl, then contentHash.

        First occurrence wins for firstSeenAt; later duplicates are dropped.
        """
        by_id: Dict[str, LivePost] = {}
        by_url: Dict[str, LivePost] = {}
        by_hash: Dict[str, LivePost] = {}
        result: List[LivePost] = []
        removed = {"by_id": 0, "by_url": 0, "by_hash": 0}
        for post in posts:
            if post.sourcePostId in by_id:
                removed["by_id"] += 1
                continue
            url = post.canonicalUrl or ""
            if url and url in by_url:
                removed["by_url"] += 1
                continue
            h = post.contentHash
            if h and h in by_hash:
                removed["by_hash"] += 1
                continue
            by_id[post.sourcePostId] = post
            if url:
                by_url[url] = post
            if h:
                by_hash[h] = post
            result.append(post)
        return {"posts": result, "removed": removed}
