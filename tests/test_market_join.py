"""Issue #15 tests — preserve post counts when sentiment is unavailable.

Validates the three-way join semantics:
  Case A: FM bucket exists and has sentiment
  Case B: FM bucket exists but no analyzable sentiment (postCount preserved)
  Case C: No FM bucket exists (postCount == 0)
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fmindex.market.bridge import MarketRecord
from fmindex.fmindex_calc import HourlyFMIndex
from fmindex.market_join import MarketSentimentJoiner, JoinedRecord

KST = timezone(timedelta(hours=9))
TS = "2026-08-05T09:00:00+09:00"


def _market(ts: str = TS) -> MarketRecord:
    return MarketRecord(
        timestamp=ts,
        market="KOSPI",
        instrument_id="KOSPI",
        symbol="KOSPI",
        open=3000.0,
        high=3010.0,
        low=2990.0,
        close=3000.0,
        change_rate=0.0,
        source="test",
        observed_at=ts,
        data_mode="sample",
    )


# --------------------------------------------------------------------------- #
# JOIN SEMANTICS                                                              #
# --------------------------------------------------------------------------- #


class TestNoPostsDistinctFromNoSentiment:
    """Case C: no FM bucket → postCount=0, hasSentiment=false, fmIndex=null."""

    def test_no_fm_bucket_yields_zero_postcount(self):
        joiner = MarketSentimentJoiner()
        joined = joiner.join([_market()], [])

        assert len(joined) == 1
        assert joined[0].postCount == 0
        assert joined[0].has_sentiment is False
        assert joined[0].fmIndex is None
        assert joined[0].confidence == 0.0


class TestPostsPresentButNoSentiment:
    """Case B: FM bucket exists but has_sentiment=False; postCount preserved."""

    def test_postcount_preserved_without_sentiment(self):
        fm = HourlyFMIndex(
            timestamp=TS,
            fmIndex=None,
            has_sentiment=False,
            positiveRatio=0.0,
            negativeRatio=0.0,
            neutralRatio=0.0,
            postCount=15,
            commentCount=7,
            analyzedPostCount=0,
            confidence=0.0,
            methodologyVersion="fmindex-v1",
        )
        joiner = MarketSentimentJoiner()
        joined = joiner.join([_market()], [fm])

        assert len(joined) == 1
        assert joined[0].fmIndex is None
        assert joined[0].has_sentiment is False
        assert joined[0].postCount == 15
        assert joined[0].confidence == 0.0

    def test_metadata_preserved_without_sentiment(self):
        fm = HourlyFMIndex(
            timestamp=TS,
            fmIndex=None,
            has_sentiment=False,
            positiveRatio=0.0,
            negativeRatio=0.0,
            neutralRatio=0.0,
            postCount=15,
            commentCount=7,
            analyzedPostCount=0,
            confidence=0.0,
            methodologyVersion="fmindex-v1",
        )
        joiner = MarketSentimentJoiner()
        joined = joiner.join([_market()], [fm])

        assert joined[0].commentCount == 7
        assert joined[0].analyzedPostCount == 0

    def test_zero_postcount_means_no_posts(self):
        """When FM bucket has postCount=0 and has_sentiment=False, postCount stays 0."""
        fm = HourlyFMIndex(
            timestamp=TS,
            fmIndex=None,
            has_sentiment=False,
            positiveRatio=0.0,
            negativeRatio=0.0,
            neutralRatio=0.0,
            postCount=0,
            commentCount=0,
            analyzedPostCount=0,
            confidence=0.0,
            methodologyVersion="fmindex-v1",
        )
        joiner = MarketSentimentJoiner()
        joined = joiner.join([_market()], [fm])

        assert joined[0].postCount == 0
        assert joined[0].has_sentiment is False
        assert joined[0].fmIndex is None


class TestSentimentPresent:
    """Case A: FM bucket exists and has sentiment; all fields propagated."""

    def test_sentiment_present_preserves_all_fields(self):
        fm = HourlyFMIndex(
            timestamp=TS,
            fmIndex=65.0,
            has_sentiment=True,
            positiveRatio=0.6,
            negativeRatio=0.2,
            neutralRatio=0.2,
            postCount=10,
            commentCount=5,
            analyzedPostCount=8,
            confidence=0.85,
            methodologyVersion="fmindex-v1",
        )
        joiner = MarketSentimentJoiner()
        joined = joiner.join([_market()], [fm])

        assert joined[0].postCount == 10
        assert joined[0].confidence == 0.85
        assert joined[0].has_sentiment is True
        assert joined[0].fmIndex == 65.0
        assert joined[0].commentCount == 5
        assert joined[0].analyzedPostCount == 8


# --------------------------------------------------------------------------- #
# JSON / DASHBOARD CONTRACT                                                   #
# --------------------------------------------------------------------------- #


class TestJoinedRecordCamelCase:
    """to_dict() must expose only camelCase keys."""

    def test_camel_case_keys_present(self):
        rec = JoinedRecord(
            timestamp=TS,
            market="KOSPI",
            instrument_id="KOSPI",
            symbol="KOSPI",
            fmIndex=None,
            has_sentiment=False,
            marketNormalized=100.0,
            marketChangeRate=0.0,
            postCount=15,
            confidence=0.0,
            data_mode="sample",
            commentCount=7,
            analyzedPostCount=0,
        )
        d = rec.to_dict()

        assert d["postCount"] == 15
        assert d["hasSentiment"] is False
        assert d["commentCount"] == 7
        assert d["analyzedPostCount"] == 0
        assert d["dataMode"] == "sample"

    def test_snake_case_not_exposed(self):
        rec = JoinedRecord(
            timestamp=TS,
            market="KOSPI",
            instrument_id="KOSPI",
            symbol="KOSPI",
            fmIndex=None,
            has_sentiment=False,
            marketNormalized=100.0,
            marketChangeRate=0.0,
            postCount=15,
            confidence=0.0,
            data_mode="sample",
        )
        d = rec.to_dict()

        for key in (
            "has_sentiment",
            "data_mode",
            "instrument_id",
            "comment_count",
            "analyzed_post_count",
        ):
            assert key not in d


class TestDashboardContract:
    """Dashboard data JSON must preserve postCount when hasSentiment=false."""

    def test_dashboard_preserves_postcount_no_sentiment(self, tmp_path):
        fm = HourlyFMIndex(
            timestamp=TS,
            fmIndex=None,
            has_sentiment=False,
            positiveRatio=0.0,
            negativeRatio=0.0,
            neutralRatio=0.0,
            postCount=15,
            commentCount=7,
            analyzedPostCount=0,
            confidence=0.0,
            methodologyVersion="fmindex-v1",
        )
        joiner = MarketSentimentJoiner()
        joined = joiner.join([_market()], [fm])
        joined_dicts = [j.to_dict() for j in joined]

        data = {
            "joined": joined_dicts,
            "overnight": {"status": "unavailable", "reason": "real_session_data_not_available"},
            "summary": {
                "totalPosts": 15,
                "totalAnalyzed": 0,
                "dataMode": "sample",
                "methodologyVersion": "fmindex-v1",
            },
        }
        js_path = tmp_path / "api" / "data.json"
        js_path.parent.mkdir(parents=True, exist_ok=True)
        js_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        loaded = json.loads(js_path.read_text(encoding="utf-8"))
        row = loaded["joined"][0]
        assert row["fmIndex"] is None
        assert row["hasSentiment"] is False
        assert row["postCount"] == 15
        assert row["commentCount"] == 7
        assert row["analyzedPostCount"] == 0
