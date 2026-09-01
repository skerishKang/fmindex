"""Normalized live FMKorea models.

These models are the private normalized contract produced by the
low-frequency live collector and consumed by the pipeline. They never
carry author/nickname/profile identifiers, IPs, emails, or contacts.

Raw site HTML may contain public nicknames; raw files must only be kept
in gitignored local evidence paths (``run/``) and never serialized into
the normalized JSONL or the product API.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

KST = timezone(timedelta(hours=9))

#: Source identifier for the FMKorea stock board.
SOURCE = "fmkorea-stock"

#: Site base for resolving relative board links.
SITE_BASE = "https://www.fmkorea.com"

#: Data mode marker for posts collected from the live site.
DATA_MODE_REAL = "real"

#: Canonical query parameters preserved on board URLs.
_BOARD_QUERY_KEYS = ("document_srl", "mid", "category")

#: Leading/trailing whitespace and invisible characters removed from text.
_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\ufeff]")


def now_kst() -> str:
    """Current KST ISO-8601 timestamp."""
    return datetime.now(KST).isoformat()


def normalize_text(text: Optional[str]) -> str:
    """Normalize text for hashing: NFC, collapse whitespace, drop zero-width."""
    if text is None:
        return ""
    text = unicodedata.normalize("NFC", str(text))
    text = _ZERO_WIDTH_RE.sub("", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def parse_int_signed(text: Any) -> Optional[int]:
    """Parse an integer that may carry a sign and thousand separators.

    Used for recommendationCount (negative/zero/positive allowed). Returns
    None when the value cannot be parsed (never silently coerced to 0).
    """
    if text is None:
        return None
    cleaned = re.sub(r"[,\s]|[가-힣]+", "", str(text))
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def parse_nonnegative_int(text: Any) -> Optional[int]:
    """Parse a non-negative integer (viewCount / commentCount)."""
    value = parse_int_signed(text)
    if value is None or value < 0:
        return None
    return value


def canonicalize_url(url: str) -> str:
    """Canonicalize a board URL.

    Keeps only ``document_srl`` / ``mid`` / ``category`` query params,
    drops tracking params, fragments, and unnecessary page params.
    Relative board paths are resolved against the site base so the result
    is always an absolute URL.
    """
    if not url:
        return ""
    if not url.startswith(("http://", "https://", "//")):
        url = f"{SITE_BASE}{url if url.startswith('/') else '/' + url}"
    if url.startswith("//"):
        url = f"https:{url}"
    parts = urlsplit(url)
    qs = parse_qs(parts.query, keep_blank_values=False)
    kept = {k: v[0] for k, v in qs.items() if k in _BOARD_QUERY_KEYS}
    if "document_srl" in kept:
        # document_srl is the primary identity; drop page/cpage params.
        kept.pop("page", None)
        kept.pop("cpage", None)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), ""))


def content_hash(
    source_post_id: str,
    title: str,
    body: str,
    comment_bodies: List[str],
) -> str:
    """SHA-256 hash of normalized content.

    Comment bodies are normalized WITHOUT author/nickname so the hash is
    stable across author changes and never embeds personal identifiers.
    """
    parts = [
        source_post_id,
        normalize_text(title),
        normalize_text(body),
    ]
    parts.extend(normalize_text(c) for c in comment_bodies)
    raw = "\x1f".join(parts)
    return f"sha256:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


@dataclass
class LiveComment:
    """A normalized live comment (author information is never stored)."""

    sourceCommentId: str = ""
    parentSourceCommentId: Optional[str] = None
    depth: int = 0
    body: str = ""
    publishedAt: str = ""
    recommendationCount: int = 0
    deleted: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sourceCommentId": self.sourceCommentId,
            "parentSourceCommentId": self.parentSourceCommentId,
            "depth": self.depth,
            "body": self.body,
            "publishedAt": self.publishedAt,
            "recommendationCount": self.recommendationCount,
            "deleted": self.deleted,
        }


@dataclass
class LivePost:
    """A normalized live FMKorea post (no personal identifiers)."""

    sourcePostId: str
    source: str = SOURCE
    canonicalUrl: str = ""
    title: str = ""
    body: str = ""
    publishedAt: Optional[str] = None
    firstSeenAt: str = ""
    viewCount: int = 0
    recommendationCount: int = 0
    commentCount: int = 0
    comments: List[LiveComment] = field(default_factory=list)
    contentHash: str = ""
    dataMode: str = DATA_MODE_REAL
    deleted: bool = False

    def compute_hash(self) -> str:
        self.contentHash = content_hash(
            self.sourcePostId,
            self.title,
            self.body,
            [c.body for c in self.comments],
        )
        return self.contentHash

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sourcePostId": self.sourcePostId,
            "source": self.source,
            "canonicalUrl": self.canonicalUrl,
            "title": self.title,
            "body": self.body,
            "publishedAt": self.publishedAt,
            "firstSeenAt": self.firstSeenAt,
            "viewCount": self.viewCount,
            "recommendationCount": self.recommendationCount,
            "commentCount": self.commentCount,
            "comments": [c.to_dict() for c in self.comments],
            "contentHash": self.contentHash,
            "dataMode": self.dataMode,
            "deleted": self.deleted,
        }
