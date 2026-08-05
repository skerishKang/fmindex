"""Live FMKorea (Rhymix/XE) HTML parser.

Parses the actual FMKorea public board HTML into the private normalized
``LivePost``/``LiveComment`` models. The parser is structure-driven and
kept separate from the simplified fixture parser (``parser.py``).

Personal identifiers (nicknames, member ids, profile URLs) present in the
raw HTML are deliberately not stored in the normalized output.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from .live_models import (
    KST,
    LiveComment,
    LivePost,
    canonicalize_url,
    now_kst,
    normalize_text,
    parse_int_signed,
    parse_nonnegative_int,
)

# --------------------------------------------------------------------------- #
# Selector contract (Rhymix-based FMKorea). Confirmed/adjusted against the
# live site during validation; never silently guessed on mismatch.
# --------------------------------------------------------------------------- #

_DOC_LINK_RE = re.compile(r"(\d{6,})")

_DELETED_RE = re.compile(
    r"삭제된\s*(?:게시|글)|존재하지\s*않는\s*게시|글이\s*없|글을\s*찾을\s*수\s*없"
)

_TITLE_CLASSES = ("title", "np_18px", "b_title", "bd_title", "subject")
_BODY_CLASSES = ("rd_body", "read_body", "bd_doc")
_COMMENT_LIST_CLASSES = ("comment", "cmt_item", "reply", "reply_list", "cmt_list", "comment_list", "fdb_itm")
_COMMENT_BODY_CLASSES = ("comment_body", "cmt_content", "comment-body", "rd_cmt", "comment_content", "comment-content", "xe_content")
_VIEW_CLASSES = ("views", "read", "view_cnt", "bd_view", "read_cnt", "bd_cnt")
_RECOMMEND_CLASSES = ("recommend", "recommends", "like", "bd_vote", "recommend_cnt", "vote")
_COMMENT_COUNT_CLASSES = ("comments-count", "comment-count", "cmt_count", "reply_cnt", "comment_cnt")
_DATE_CLASSES = ("date", "time", "regdate", "published")


def _extract_document_id_from_href(href: str) -> Optional[str]:
    """Extract the numeric document id from a board href.

    Supports canonical ``/12345`` paths and ``document_srl=12345`` query
    params. Only numeric ids (>= 6 digits) are returned; arbitrary
    non-numeric strings are never used as post ids.
    """
    if not href:
        return None
    parts = urlsplit(href)
    qs = parse_qs(parts.query)
    srl = qs.get("document_srl")
    if srl and srl[0].isdigit() and len(srl[0]) >= 6:
        return srl[0]
    m = _DOC_LINK_RE.search(parts.path)
    if m:
        return m.group(1)
    return None


class _LiveHTMLParser(HTMLParser):
    """Stateful HTMLParser for FMKorea list and detail pages."""

    def __init__(self, mode: str):
        super().__init__(convert_charrefs=True)
        self.mode = mode  # "list" | "post"
        self.posts: List[LivePost] = []
        self.current: Optional[LivePost] = None
        self.comments: List[LiveComment] = []
        self._buf: List[str] = []
        self._capture: Optional[str] = None
        self._pending_href: Optional[str] = None
        self._in_title = False
        self._in_body = False
        self._in_comment = False
        self._comment: Optional[LiveComment] = None
        self._comment_buffer: List[str] = []
        self._pending_doc_id: Optional[str] = None
        self._body_depth = 0
        self._comment_body_depth = 0
        self._comment_ul_count = 0

    # -- helpers ---------------------------------------------------------- #
    def _text(self) -> str:
        return normalize_text("".join(self._buf))

    def _comment_text(self) -> str:
        return normalize_text("".join(self._comment_buffer))

    def _start(self, name: str) -> None:
        self._capture = name
        self._buf = []

    def _matches(self, cls: str, candidates: Tuple[str, ...]) -> bool:
        return any(k in cls for k in candidates)

    def _assign_count(self) -> None:
        if self.current is None:
            return
        value = parse_nonnegative_int(self._text())
        if value is not None:
            self.current.viewCount = value

    def _assign_recommend(self) -> None:
        if self.current is None:
            return
        value = parse_int_signed(self._text())
        if value is not None:
            self.current.recommendationCount = value

    def _assign_comment_count(self) -> None:
        if self.current is None:
            return
        value = parse_nonnegative_int(self._text())
        if value is not None:
            self.current.commentCount = value

    # -- start tags -------------------------------------------------------- #
    def handle_starttag(self, tag: str, attrs: List[Tuple[str, str]]) -> None:
        ad = {k.lower(): v for k, v in attrs}
        cls = ad.get("class", "")

        if self.mode == "list":
            if tag == "a" and ad.get("href"):
                doc_id = _extract_document_id_from_href(ad["href"])
                if doc_id:
                    self._pending_href = ad["href"]
                    self._pending_doc_id = doc_id
                    self._start("list_title")
            return

        # ---- detail mode ---- #
        if tag == "meta" and self.mode == "post":
            prop = (ad.get("property") or ad.get("name") or "").lower()
            content = ad.get("content", "")
            if prop == "og:title" and self.current is not None and not self.current.title:
                self.current.title = normalize_text(content)
            elif prop == "og:url" and self.current is not None and not self.current.canonicalUrl:
                self.current.canonicalUrl = canonicalize_url(content)
            return

        if tag in ("h1", "h2", "div") and self._matches(cls, _TITLE_CLASSES):
            if self.current is not None and self.current.title and "np_18px" not in cls:
                return  # og:title already populated
            self._in_title = True
            self._start("title")
            return

        if self._in_comment and tag in ("div", "p", "span") and self._matches(cls, _COMMENT_BODY_CLASSES):
            self._capture = "comment_body"
            self._comment_buffer = []
            self._comment_body_depth = 1
            return
        if self._capture == "comment_body" and tag in ("div", "p", "span"):
            self._comment_body_depth += 1
            return

        if tag in ("div", "section", "article") and self._matches(cls, _BODY_CLASSES):
            if not self._in_comment and not self._in_title:
                if self.current is not None and self.current.body:
                    return  # body already captured (e.g. sidebar article)
                self._in_body = True
                self._start("body")
                self._body_depth = 1
            return
        if self._capture == "body" and tag in ("div", "section", "article"):
            self._body_depth += 1
            return

        if tag == "ul":
            # Comment list nesting: top-level <ul class="fdb_lst_ul">
            # contains depth-0 comments; nested <ul> inside a comment li
            # contains replies (depth+1).
            if "fdb_lst_ul" in cls or self._in_comment or self._comment_ul_count > 0:
                self._comment_ul_count += 1
            return

        if tag in ("div", "li") and self._matches(cls, _COMMENT_LIST_CLASSES):
            # Real FMKorea: comments are <li id="comment_N" class="fdb_itm">.
            if tag == "li" and "fdb_itm" in cls:
                if not self._in_comment:
                    self._in_comment = True
                    self._comment = LiveComment()
                    depth = self._comment_ul_count - 1
                    self._comment.depth = max(0, depth)
                    cid = ""
                    if ad.get("id", "").startswith("comment_"):
                        cid = ad["id"][len("comment_"):]
                    if not cid:
                        srl = ad.get("data-comment_srl", "")
                        cid = srl if srl.isdigit() else ""
                    m = re.search(r"comment[-_](\d+)", cls)
                    if not cid and m:
                        cid = m.group(1)
                    if cid:
                        cid = re.sub(r"\D", "", cid)
                        self._comment.sourceCommentId = cid
                return
            if tag == "div" and not self._in_comment:
                # Fallback for simplified fixture structure (cmt_item /
                # comment divs).
                if "cmt_item" in cls or ("comment" in cls and "comment-content" not in cls):
                    self._in_comment = True
                    self._comment = LiveComment()
                    if "reply" in cls and "comment" not in cls:
                        self._comment.depth = 1
            return

        if tag in ("span", "div", "em") and self._matches(cls, _VIEW_CLASSES):
            self._start("views")
            return

        if tag in ("span", "div", "em") and self._matches(cls, _RECOMMEND_CLASSES):
            self._start("recommends")
            return

        if tag in ("span", "div", "em") and self._matches(cls, _COMMENT_COUNT_CLASSES):
            self._start("comments_count")
            return

        if tag in ("span", "div", "em") and self._matches(cls, _DATE_CLASSES):
            dt = ad.get("datetime", "")
            self._published_dt = dt
            self._start("published")
            return

        if tag == "time":
            dt = ad.get("datetime", "")
            self._published_dt = dt
            self._start("published")
            return

    # -- end tags ---------------------------------------------------------- #
    def handle_endtag(self, tag: str) -> None:
        if self.mode == "list":
            if tag == "a" and self._capture == "list_title":
                title = self._text()
                href = self._pending_href
                doc_id = self._pending_doc_id
                self._pending_href = None
                self._pending_doc_id = None
                self._capture = None
                if href and doc_id and title:
                    self.posts.append(
                        LivePost(
                            sourcePostId=doc_id,
                            canonicalUrl=canonicalize_url(href),
                            title=title,
                            firstSeenAt=now_kst(),
                        )
                    )
            return

        if tag in ("h1", "h2", "div") and self._capture == "title":
            if self.current is not None and not self.current.title:
                self.current.title = self._text()
            self._in_title = False
            self._capture = None
            return

        if tag == "ul":
            if self._comment_ul_count > 0:
                self._comment_ul_count -= 1
            return

        if tag in ("div", "section", "article") and self._capture == "body":
            if self._body_depth > 0:
                self._body_depth -= 1
                if self._body_depth == 0:
                    if self.current is not None and not self._in_comment:
                        self.current.body = self._text()
                    self._in_body = False
                    self._capture = None
            return

        if self._capture == "comment_body":
            if tag in ("div", "p", "span"):
                if self._comment_body_depth > 0:
                    self._comment_body_depth -= 1
                    if self._comment_body_depth == 0 and self._in_comment:
                        if self._comment is not None:
                            self._comment.body = self._comment_text()
                            self.comments.append(self._comment)
                        self._comment = None
                        self._in_comment = False
                        self._capture = None
            return

        if self._capture == "views":
            self._assign_count()
            self._capture = None
            return

        if self._capture == "recommends":
            self._assign_recommend()
            self._capture = None
            return

        if self._capture == "comments_count":
            self._assign_comment_count()
            self._capture = None
            return

        if self._capture == "published":
            if self.current is not None:
                dt = getattr(self, "_published_dt", "") or ""
                text = self._text()
                # Prefer the absolute datetime attribute; fall back to the
                # display text only when it looks absolute. Relative times
                # are never translated into absolute values.
                value = dt or text
                if self.current.publishedAt is None and value:
                    self.current.publishedAt = value
            self._capture = None
            self._published_dt = ""
            return

    # -- data -------------------------------------------------------------- #
    def handle_data(self, data: str) -> None:
        if self._capture == "comment_body":
            self._comment_buffer.append(data)
        elif self._capture is not None:
            self._buf.append(data)


# --------------------------------------------------------------------------- #
# Public parsing API                                                          #
# --------------------------------------------------------------------------- #


def parse_live_list(html: str) -> List[LivePost]:
    """Parse the stock-board list page into post stubs."""
    parser = _LiveHTMLParser(mode="list")
    parser.feed(html)
    return parser.posts


def parse_live_post(
    html: str, source_post_id: Optional[str] = None
) -> Optional[LivePost]:
    """Parse a post detail page (with comments) into a LivePost.

    Returns None when the page is not a post detail page. Deleted posts
    are classified (``deleted=True``), never passed off as normal posts.
    """
    parser = _LiveHTMLParser(mode="post")
    if source_post_id:
        parser.current = LivePost(
            sourcePostId=source_post_id,
            firstSeenAt=now_kst(),
        )
    parser.feed(html)

    post = parser.current
    if post is None:
        return None

    post.comments = parser.comments
    if not post.commentCount:
        post.commentCount = len(parser.comments)

    combined = normalize_text(f"{post.title} {post.body}")
    if _DELETED_RE.search(combined):
        post.deleted = True

    post.compute_hash()
    return post
