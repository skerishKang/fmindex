"""Issue #14 hardened safety contract tests for FMKorea live collector."""

from pathlib import Path

import pytest

from fmindex.fmkorea.live_collector import (
    FMKoreaCaptchaError,
    FMKoreaDeletedPostError,
    FMKoreaForbiddenError,
    FMKoreaRateLimitError,
    FMKoreaRequestBudgetExceeded,
    FMKoreaSafetyValidationError,
    FMKoreaUnexpectedContentError,
    LiveFMKoreaCollector,
    FetchResult,
)
from fmindex.fmkorea.live_parser import parse_live_post
from fmindex.fmkorea.live_models import LivePost

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "fmkorea"


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeTransport:
    """Injected fake transport: (url, headers, body, timeout) -> FetchResult."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.last_headers = None

    def __call__(self, url, headers, body, timeout):
        self.calls.append(url)
        self.last_headers = headers
        if self.responses:
            resp = self.responses.pop(0)
            if isinstance(resp, FetchResult):
                return resp
            return FetchResult(status="ok", html=resp, http_status=200)
        return FetchResult(status="ok", html="<html></html>", http_status=200)


class TestHttpTaxonomyFailClosed:
    """All 4xx (except 404/410) and 5xx must be fail-closed — never classified as ok."""

    def _classify(self, code, html=""):
        from fmindex.fmkorea.live_collector import classify_http
        return classify_http(code, html)

    def test_400_is_client_error(self):
        assert self._classify(400) == "client_error"

    def test_401_is_forbidden(self):
        assert self._classify(401) == "forbidden"

    def test_403_is_forbidden(self):
        assert self._classify(403) == "forbidden"

    def test_404_is_deleted(self):
        assert self._classify(404) == "deleted"

    def test_405_is_client_error(self):
        assert self._classify(405) == "client_error"

    def test_410_is_deleted(self):
        assert self._classify(410) == "deleted"

    def test_429_is_rate_limited(self):
        assert self._classify(429) == "rate_limited"

    def test_451_is_client_error(self):
        assert self._classify(451) == "client_error"

    def test_500_is_server_error(self):
        assert self._classify(500) == "server_error"

    def test_503_is_server_error(self):
        assert self._classify(503) == "server_error"

    def test_unknown_4xx_is_client_error(self):
        assert self._classify(418) == "client_error"

    def test_none_status_is_network_error(self):
        assert self._classify(None) == "network_error"

    def test_200_ok_without_captcha(self):
        assert self._classify(200, "<html><body>normal page</body></html>") == "ok"

    def test_200_with_captcha_is_captcha(self):
        assert self._classify(200, "<html><body>CAPTCHA check</body></html>") == "captcha"


class TestCountersExactlyOnce:
    """Each HTTP/result outcome must increment audit counters exactly once."""

    def test_deleted_counter_exactly_once(self, tmp_path):
        transport = FakeTransport([FetchResult(status="deleted", http_status=404)])
        collector = LiveFMKoreaCollector(transport=transport, max_posts=1)
        stubs = [LivePost(sourcePostId="12345678", canonicalUrl="/12345678", title="t")]
        collector.fetch_posts(stubs, tmp_path)
        assert collector.deleted_posts == 1

    def test_unexpected_content_counter_exactly_once(self, tmp_path):
        # When _fetch returns a pre-classified unexpected_content result,
        # the counter must increment exactly once per call.
        transport = FakeTransport([
            FetchResult(status="unexpected_content", http_status=500),
            FetchResult(status="unexpected_content", http_status=500),
        ])
        collector = LiveFMKoreaCollector(transport=transport)
        collector._fetch("http://x/y", "test")
        assert collector.unexpected_content == 1
        collector._fetch("http://x/z", "test2")
        assert collector.unexpected_content == 2

    def test_two_deleted_responses_yield_two_counters(self, tmp_path):
        transport = FakeTransport([
            FetchResult(status="deleted", http_status=404),
            FetchResult(status="deleted", http_status=404),
        ])
        collector = LiveFMKoreaCollector(transport=transport, max_posts=2)
        stubs = [
            LivePost(sourcePostId=f"1{i:06d}", canonicalUrl=f"/1{i:06d}", title=f"t{i}")
            for i in range(2)
        ]
        collector.fetch_posts(stubs, tmp_path)
        assert collector.deleted_posts == 2


class TestSafetyBoundsFailFast:
    """Constructor must reject unsafe values with FMKoreaSafetyValidationError."""

    def test_delay_zero_rejected(self):
        with pytest.raises(FMKoreaSafetyValidationError):
            LiveFMKoreaCollector(request_delay=0)

    def test_delay_one_rejected(self):
        with pytest.raises(FMKoreaSafetyValidationError):
            LiveFMKoreaCollector(request_delay=1)

    def test_delay_two_point_nine_rejected(self):
        with pytest.raises(FMKoreaSafetyValidationError):
            LiveFMKoreaCollector(request_delay=2.9)

    def test_delay_three_accepted(self):
        c = LiveFMKoreaCollector(request_delay=3.0)
        assert c.request_delay == 3.0

    def test_delay_three_point_five_accepted(self):
        c = LiveFMKoreaCollector(request_delay=3.5)
        assert c.request_delay == 3.5

    def test_negative_delay_rejected(self):
        with pytest.raises(FMKoreaSafetyValidationError):
            LiveFMKoreaCollector(request_delay=-1.0)

    def test_max_posts_zero_accepted(self):
        c = LiveFMKoreaCollector(max_posts=0)
        assert c.max_posts == 0

    def test_max_posts_three_accepted(self):
        c = LiveFMKoreaCollector(max_posts=3)
        assert c.max_posts == 3

    def test_max_posts_four_rejected(self):
        with pytest.raises(FMKoreaSafetyValidationError):
            LiveFMKoreaCollector(max_posts=4)

    def test_max_list_two_rejected(self):
        with pytest.raises(FMKoreaSafetyValidationError):
            LiveFMKoreaCollector(max_list=2)

    def test_max_comment_two_rejected(self):
        with pytest.raises(FMKoreaSafetyValidationError):
            LiveFMKoreaCollector(max_comment=2)


class TestRobotsContentBudgetSeparation:
    """Robots calls must NOT count toward content_calls budget."""

    def test_robots_does_not_consume_content_budget(self, tmp_path):
        transport = FakeTransport([
            FetchResult(status="ok", html="User-agent: *\nDisallow:\n", http_status=200),
            FetchResult(status="ok", html="<html></html>", http_status=200),
        ])
        collector = LiveFMKoreaCollector(transport=transport, max_posts=1)
        collector.check_robots()
        assert collector.robots_calls == 1
        assert collector.content_calls == 0  # robots call did not consume content budget
        # Now fetch the list — content_calls should become 1
        stubs = collector.fetch_list(tmp_path)
        assert collector.content_calls == 1

    def test_content_budget_separate_from_robots_budget(self):
        from fmindex.fmkorea.live_collector import MAX_CONTENT_CALLS, MAX_ROBOTS_CALLS
        assert MAX_CONTENT_CALLS != MAX_ROBOTS_CALLS  # different hard limits
        assert MAX_ROBOTS_CALLS == 1
        assert MAX_CONTENT_CALLS == 5


class TestRelativeTimeRejected:
    """Relative times like '3분 전' must NEVER become publishedAt."""

    def test_relative_time_not_stored_as_published_at(self):
        from fmindex.fmkorea.live_parser import parse_live_post
        post = parse_live_post(read_fixture("live-relative-time-sanitized.html"), source_post_id="999999")
        assert post is not None
        assert post.publishedAt is None or post.publishedAt == ""
        assert post.firstSeenAt  # firstSeenAt must be populated

    def test_absolute_datetime_attr_preferred(self):
        post = parse_live_post(read_fixture("live-post-sanitized.html"), source_post_id="123456780")
        assert post is not None
        assert post.publishedAt
        assert "2026-08-05" in post.publishedAt

    def test_firstseen_never_equals_published(self):
        post = parse_live_post(read_fixture("live-post-sanitized.html"), source_post_id="123456780")
        assert post.publishedAt
        assert post.firstSeenAt
        assert post.publishedAt != post.firstSeenAt


class TestHttp429ZeroRetryBudget:
    """HTTP 429 must cause exactly one transport call and abort immediately."""

    def test_429_zero_retry_single_transport_call(self, tmp_path):
        transport = FakeTransport([FetchResult(status="rate_limited", http_status=429)])
        collector = LiveFMKoreaCollector(transport=transport)
        with pytest.raises(FMKoreaRateLimitError):
            collector.fetch_list(tmp_path)
        assert len(transport.calls) == 1
        assert collector.http_429 == 1

    def test_429_content_calls_increments(self, tmp_path):
        transport = FakeTransport([FetchResult(status="rate_limited", http_status=429)])
        collector = LiveFMKoreaCollector(transport=transport)
        with pytest.raises(FMKoreaRateLimitError):
            collector.fetch_list(tmp_path)
        # content_calls counts the actual transport call even on abort
        assert collector.content_calls == 1


class TestContentTypeFailureAborts:
    """Various non-ok HTTP statuses must abort collection gracefully."""

    def test_400_aborts_list(self, tmp_path):
        transport = FakeTransport([FetchResult(status="client_error", http_status=400)])
        collector = LiveFMKoreaCollector(transport=transport)
        with pytest.raises(FMKoreaUnexpectedContentError):
            collector.fetch_list(tmp_path)

    def test_401_aborts_list(self, tmp_path):
        transport = FakeTransport([FetchResult(status="forbidden", http_status=401)])
        collector = LiveFMKoreaCollector(transport=transport)
        with pytest.raises(FMKoreaForbiddenError):
            collector.fetch_list(tmp_path)

    def test_500_aborts_list(self, tmp_path):
        transport = FakeTransport([FetchResult(status="server_error", http_status=500)])
        collector = LiveFMKoreaCollector(transport=transport)
        with pytest.raises(FMKoreaUnexpectedContentError):
            collector.fetch_list(tmp_path)

    def test_503_aborts_list(self, tmp_path):
        transport = FakeTransport([FetchResult(status="server_error", http_status=503)])
        collector = LiveFMKoreaCollector(transport=transport)
        with pytest.raises(FMKoreaUnexpectedContentError):
            collector.fetch_list(tmp_path)



class TestRobotsFailClosed:
    """Robots endpoint must abort on forbidden/rate_limited/captcha."""

    def test_robots_429_aborts(self):
        transport = FakeTransport([FetchResult(status="rate_limited", http_status=429)])
        collector = LiveFMKoreaCollector(transport=transport)
        with pytest.raises(FMKoreaRateLimitError):
            collector.check_robots()
        assert len(transport.calls) == 1
        assert collector.http_429 == 1

    def test_robots_403_aborts(self):
        transport = FakeTransport([FetchResult(status="forbidden", http_status=403)])
        collector = LiveFMKoreaCollector(transport=transport)
        with pytest.raises(FMKoreaForbiddenError):
            collector.check_robots()
        assert len(transport.calls) == 1
        assert collector.http_403 == 1

    def test_robots_captcha_aborts(self):
        transport = FakeTransport([FetchResult(status="captcha", html="<html>CAPTCHA</html>", http_status=200)])
        collector = LiveFMKoreaCollector(transport=transport)
        with pytest.raises(FMKoreaCaptchaError):
            collector.check_robots()
        assert len(transport.calls) == 1
        assert collector.captcha_count == 1

    def test_robots_network_error_unresolved(self):
        transport = FakeTransport([FetchResult(status="network_error")])
        collector = LiveFMKoreaCollector(transport=transport)
        result = collector.check_robots()
        assert result == "unresolved"


class TestMaxListBoundary:
    """max_list=0 blocks all list requests; max_list=1 allows one."""

    def test_max_list_zero_blocks_transport(self):
        transport = FakeTransport([])
        collector = LiveFMKoreaCollector(transport=transport, max_list=0)
        with pytest.raises(FMKoreaRequestBudgetExceeded):
            collector.fetch_list(Path('/tmp'))
        assert len(transport.calls) == 0
        assert collector.list_calls == 0

    def test_max_list_one_allowed_then_blocked(self):
        transport = FakeTransport(["<html></html>"])
        collector = LiveFMKoreaCollector(transport=transport, max_list=1)
        # First call should succeed
        collector.fetch_list(Path('/tmp'))
        assert collector.list_calls == 1
        assert len(transport.calls) == 1
        # Second call should be blocked before transport
        with pytest.raises(FMKoreaRequestBudgetExceeded):
            collector.fetch_list(Path('/tmp'))
        assert len(transport.calls) == 1  # no additional calls


class TestDatetimeValidation:
    """publishedAt must only accept timezone-aware ISO 8601 datetimes."""

    def test_valid_datetime_with_offset(self):
        from fmindex.fmkorea.live_parser import _validate_absolute_datetime
        result = _validate_absolute_datetime("2026-08-05T09:15:00+09:00")
        assert result == "2026-08-05T09:15:00+09:00"

    def test_valid_zulu_datetime(self):
        from fmindex.fmkorea.live_parser import _validate_absolute_datetime
        result = _validate_absolute_datetime("2026-08-05T00:15:00Z")
        assert result == "2026-08-05T00:15:00Z"

    def test_relative_korean_rejected(self):
        from fmindex.fmkorea.live_parser import _validate_absolute_datetime
        assert _validate_absolute_datetime("3분 전") is None
        assert _validate_absolute_datetime("방금") is None
        assert _validate_absolute_datetime("어제") is None
        assert _validate_absolute_datetime("오늘") is None
        assert _validate_absolute_datetime("몇 시간 전") is None

    def test_date_only_rejected(self):
        from fmindex.fmkorea.live_parser import _validate_absolute_datetime
        # Date-only without time or timezone should be rejected
        assert _validate_absolute_datetime("2026-08-05") is None

    def test_time_only_without_tz_rejected(self):
        from fmindex.fmkorea.live_parser import _validate_absolute_datetime
        # Time-only without timezone is naive -> rejected
        assert _validate_absolute_datetime("2026-08-05T09:15") is None

    def test_malformed_string_rejected(self):
        from fmindex.fmkorea.live_parser import _validate_absolute_datetime
        assert _validate_absolute_datetime("not-a-date") is None
        assert _validate_absolute_datetime("") is None
        assert _validate_absolute_datetime(None) is None

    def test_parser_accepts_valid_datetime_attr(self):
        post = parse_live_post(read_fixture("live-post-sanitized.html"), source_post_id="123456780")
        assert post is not None
        assert post.publishedAt
        assert "+09:00" in post.publishedAt

    def test_parser_rejects_relative_display_text(self):
        post = parse_live_post(read_fixture("live-relative-time-sanitized.html"), source_post_id="999999")
        assert post is not None
        assert post.publishedAt is None or post.publishedAt == ""
        assert post.firstSeenAt  # firstSeenAt should still be populated
        assert post.publishedAt != post.firstSeenAt
