"""FMKorea HTML fixture parser — extracts posts and comments from saved HTML."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

KST = timezone(timedelta(hours=9))


@dataclass
class Comment:
    """A single comment on a post."""

    author: str = ""
    body: str = ""
    publishedAt: str = ""
    recommendationCount: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ParsedPost:
    """A parsed FMKorea post with metadata and comments."""

    sourcePostId: str = ""
    url: str = ""
    title: str = ""
    body: str = ""
    publishedAt: str = ""
    firstSeenAt: str = ""
    viewCount: int = 0
    recommendationCount: int = 0
    commentCount: int = 0
    comments: List[Comment] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["comments"] = [c if isinstance(c, dict) else asdict(c) for c in self.comments]
        return d


class _FMKoreaHTMLParser(HTMLParser):
    """Stateful HTML parser for FMKorea-style pages.

    This parser handles a simplified HTML structure that mirrors the
    common FMKorea (FMKorea.com / Arca.live) board layout:

    - Post list: <a class="post-link" href="..." data-id="...">title</a>
    - Post detail: <article class="post"> with <h1 class="title">, <div class="body">
    - Comments: <div class="comment"> with <span class="author">, <p class="body">
    - Metadata: <span class="views">, <span class="recommends">, <span class="comments-count">
    - Timestamp: <time datetime="..." class="published">
    """

    def __init__(self):
        super().__init__()
        self.posts: List[ParsedPost] = []
        self.current_post: Optional[ParsedPost] = None
        self.current_comment: Optional[Comment] = None
        self._capture: Optional[str] = None
        self._in_post = False
        self._in_comment = False
        self._list_links: List[Tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        cls = attrs_dict.get("class", "")

        if tag == "a" and "post-link" in cls:
            post_id = attrs_dict.get("data-id", "")
            href = attrs_dict.get("href", "")
            self._capture = "link_title"
            self._list_links.append((post_id, href))
            if self.current_post is None:
                self.current_post = ParsedPost(sourcePostId=post_id, url=href)

        elif tag == "article" and "post" in cls:
            self._in_post = True
            if self.current_post is None:
                self.current_post = ParsedPost()

        elif tag == "h1" and "title" in cls and self._in_post:
            self._capture = "title"

        elif tag == "div" and "body" in cls and self._in_post:
            if self._in_comment:
                self._capture = "comment_body"
            else:
                self._capture = "body"

        elif tag == "div" and "comment" in cls:
            self._in_comment = True
            self.current_comment = Comment()

        elif tag == "span" and "author" in cls and self._in_comment:
            self._capture = "comment_author"

        elif tag == "span" and "views" in cls:
            self._capture = "views"

        elif tag == "span" and "recommends" in cls:
            self._capture = "recommends"

        elif tag == "span" and "comments-count" in cls:
            self._capture = "comments_count"

        elif tag == "time" and "published" in cls:
            dt = attrs_dict.get("datetime", "")
            # In list mode, attach to last post; in detail mode, attach to current
            target = self.current_post
            if not target and self.posts:
                target = self.posts[-1]
            if target and dt:
                target.publishedAt = dt

    def handle_endtag(self, tag):
        if tag == "article" and self._in_post:
            self._in_post = False
            if self.current_post:
                self.posts.append(self.current_post)
                self.current_post = None

        elif tag == "a" and self._capture == "link_title":
            # List page: add post when </a> is encountered
            if self.current_post and self.current_post.title:
                self.current_post.firstSeenAt = datetime.now(KST).isoformat()
                self.posts.append(self.current_post)
            self.current_post = None
            self._capture = None
            return

        elif tag == "div" and self._in_comment:
            if self.current_comment and self.current_post:
                self.current_post.comments.append(self.current_comment)
                self.current_post.commentCount = len(self.current_post.comments)
            self._in_comment = False
            self.current_comment = None

        if self._capture and tag in ("a", "h1", "div", "span", "p", "time"):
            self._capture = None

    def handle_data(self, data):
        if not self._capture:
            return

        text = data.strip()
        if not text:
            return

        # In list mode, current_post may be None; use last post
        target = self.current_post
        if not target and self.posts:
            target = self.posts[-1]

        if self._capture == "link_title":
            if self.current_post:
                self.current_post.title = text

        elif self._capture == "title" and self.current_post:
            self.current_post.title = text

        elif self._capture == "body" and self.current_post:
            self.current_post.body = (self.current_post.body + " " + text).strip()

        elif self._capture == "comment_author" and self.current_comment:
            self.current_comment.author = text

        elif self._capture == "comment_body" and self.current_comment:
            self.current_comment.body = (self.current_comment.body + " " + text).strip()

        elif self._capture == "views" and target:
            target.viewCount = self._parse_int(text)

        elif self._capture == "recommends" and target:
            target.recommendationCount = self._parse_int(text)

        elif self._capture == "comments_count" and target:
            target.commentCount = self._parse_int(text)

    @staticmethod
    def _parse_int(text: str) -> int:
        digits = re.sub(r"[^\d]", "", text)
        return int(digits) if digits else 0


class FMKoreaParser:
    """Parser for FMKorea HTML fixtures."""

    def __init__(self, fixture_root: Optional[str] = None):
        self.fixture_root = Path(fixture_root) if fixture_root else None

    def parse_list(self, html: str) -> List[ParsedPost]:
        """Parse a post list page HTML and return post stubs."""
        parser = _FMKoreaHTMLParser()
        parser.feed(html)
        posts = []
        for post in parser.posts:
            if post.title:
                post.firstSeenAt = datetime.now(KST).isoformat()
                posts.append(post)
        return posts

    def parse_post(self, html: str) -> ParsedPost:
        """Parse a single post detail page HTML."""
        parser = _FMKoreaHTMLParser()
        parser.feed(html)
        if parser.posts:
            post = parser.posts[0]
            post.firstSeenAt = datetime.now(KST).isoformat()
            return post
        return ParsedPost(firstSeenAt=datetime.now(KST).isoformat())

    def parse_file(self, filepath: str) -> Any:
        """Parse an HTML file. Returns list for list pages, single post for detail pages."""
        path = Path(filepath)
        if not path.exists():
            raise FileNotFoundError(f"Fixture file not found: {path}")

        html = path.read_text(encoding="utf-8")
        filename = path.name.lower()

        if "list" in filename:
            return self.parse_list(html)
        else:
            return self.parse_post(html)

    def parse_fixture_dir(self, dirpath: str) -> List[ParsedPost]:
        """Parse all post fixtures in a directory."""
        dir_path = Path(dirpath)
        if not dir_path.exists():
            raise FileNotFoundError(f"Fixture directory not found: {dir_path}")

        posts: List[ParsedPost] = []
        for filepath in sorted(dir_path.glob("post-*.html")):
            if "list" in filepath.name.lower():
                continue
            try:
                html = filepath.read_text(encoding="utf-8")
                post = self.parse_post(html)
                if post.title:
                    posts.append(post)
            except Exception:
                continue

        return posts
