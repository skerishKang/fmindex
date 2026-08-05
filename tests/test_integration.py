"""Integration Slice 1 tests — all tests run offline without network."""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Ensure package is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fmindex.market.bridge import MarketBridge, MarketRecord, RawTick
from fmindex.fmkorea.parser import FMKoreaParser, ParsedPost, Comment
from fmindex.llm.provider import MockLLMProvider, SentimentResult, create_provider
from fmindex.fmindex_calc import FMIndexCalculator, PostWithSentiment, HourlyFMIndex
from fmindex.market_join import MarketSentimentJoiner, JoinedRecord, OvernightEvaluation
from fmindex.dashboard.server import generate_dashboard_data, write_dashboard_files
from fmindex.pipeline import run_pipeline_once

KST = timezone(timedelta(hours=9))
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "fmkorea"


# --------------------------------------------------------------------------- #
# MARKET_BRIDGE_PARSE_PASS                                                    #
# --------------------------------------------------------------------------- #


class TestMarketBridgeParse:
    def test_parse_json_list(self, tmp_path):
        """Parse a JSON file with a list of price records."""
        data = [
            {"timestamp": "2026-08-05T09:05:00+09:00", "price": "3200.5"},
            {"timestamp": "2026-08-05T09:15:00+09:00", "price": "3205.0"},
            {"timestamp": "2026-08-05T09:45:00+09:00", "price": "3198.0"},
            {"timestamp": "2026-08-05T10:10:00+09:00", "price": "3210.0"},
        ]
        f = tmp_path / "prices.json"
        f.write_text(json.dumps(data), encoding="utf-8")

        bridge = MarketBridge()
        records = bridge.read_records_from_path(str(f))

        assert len(records) == 2  # 2 hour buckets: 09:00 and 10:00
        assert records[0].market == "KOSPI"
        assert records[0].open == 3200.5
        assert records[0].close == 3198.0
        assert records[0].high == 3205.0
        assert records[0].low == 3198.0

    def test_parse_csv(self, tmp_path):
        """Parse a CSV file with price records."""
        csv_content = "timestamp,price,volume\n"
        csv_content += "2026-08-05T09:05:00+09:00,3200.5,1000\n"
        csv_content += "2026-08-05T09:35:00+09:00,3210.0,2000\n"
        csv_content += "2026-08-05T10:05:00+09:00,3205.0,1500\n"

        f = tmp_path / "prices.csv"
        f.write_text(csv_content, encoding="utf-8-sig")

        bridge = MarketBridge()
        records = bridge.read_records_from_path(str(f))

        assert len(records) == 2
        assert records[0].open == 3200.5
        assert records[0].close == 3210.0

    def test_file_not_found(self):
        bridge = MarketBridge()
        with pytest.raises(FileNotFoundError):
            bridge.read_records_from_path("/nonexistent/path.json")

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.json"
        f.write_text("[]", encoding="utf-8")
        bridge = MarketBridge()
        records = bridge.read_records_from_path(str(f))
        assert records == []


# --------------------------------------------------------------------------- #
# MARKET_HOURLY_AGGREGATION_PASS                                              #
# --------------------------------------------------------------------------- #


class TestMarketHourlyAggregation:
    def test_hourly_aggregation(self, tmp_path):
        """Multiple ticks in same hour aggregate to one OHLC."""
        data = [
            {"timestamp": "2026-08-05T09:05:00+09:00", "price": "100"},
            {"timestamp": "2026-08-05T09:15:00+09:00", "price": "110"},
            {"timestamp": "2026-08-05T09:45:00+09:00", "price": "95"},
            {"timestamp": "2026-08-05T09:50:00+09:00", "price": "105"},
        ]
        f = tmp_path / "data.json"
        f.write_text(json.dumps(data), encoding="utf-8")

        bridge = MarketBridge()
        records = bridge.read_records_from_path(str(f))

        assert len(records) == 1
        rec = records[0]
        assert rec.open == 100.0
        assert rec.high == 110.0
        assert rec.low == 95.0
        assert rec.close == 105.0
        assert rec.timestamp.startswith("2026-08-05T09:00:00")

    def test_multi_hour(self, tmp_path):
        data = [
            {"timestamp": "2026-08-05T09:05:00+09:00", "price": "100"},
            {"timestamp": "2026-08-05T10:05:00+09:00", "price": "200"},
            {"timestamp": "2026-08-05T11:05:00+09:00", "price": "300"},
        ]
        f = tmp_path / "data.json"
        f.write_text(json.dumps(data), encoding="utf-8")

        bridge = MarketBridge()
        records = bridge.read_records_from_path(str(f))

        assert len(records) == 3
        assert records[0].open == 100.0
        assert records[1].open == 200.0
        assert records[2].open == 300.0


# --------------------------------------------------------------------------- #
# MARKET_DUPLICATE_GUARD_PASS                                                 #
# --------------------------------------------------------------------------- #


class TestMarketDuplicateGuard:
    def test_duplicate_timestamp_dedup(self, tmp_path):
        """Duplicate timestamps are deduplicated (keep last)."""
        data = [
            {"timestamp": "2026-08-05T09:05:00+09:00", "price": "100"},
            {"timestamp": "2026-08-05T09:05:00+09:00", "price": "110"},
            {"timestamp": "2026-08-05T09:05:00+09:00", "price": "120"},
        ]
        f = tmp_path / "data.json"
        f.write_text(json.dumps(data), encoding="utf-8")

        bridge = MarketBridge()
        records = bridge.read_records_from_path(str(f))

        assert len(records) == 1
        # Last value wins for dedup
        assert records[0].open == 120.0
        assert records[0].close == 120.0


# --------------------------------------------------------------------------- #
# FMKOREA_LIST_FIXTURE_PASS                                                   #
# --------------------------------------------------------------------------- #


class TestFMKoreaListFixture:
    def test_parse_list(self):
        parser = FMKoreaParser()
        posts = parser.parse_file(str(FIXTURE_DIR / "list-normal.html"))

        assert len(posts) >= 3
        assert all(p.title for p in posts)
        assert all(p.sourcePostId for p in posts)
        assert all(p.url for p in posts)

    def test_list_has_timestamps(self):
        parser = FMKoreaParser()
        posts = parser.parse_file(str(FIXTURE_DIR / "list-normal.html"))
        assert any(p.publishedAt for p in posts)


# --------------------------------------------------------------------------- #
# FMKOREA_POST_FIXTURE_PASS                                                   #
# --------------------------------------------------------------------------- #


class TestFMKoreaPostFixture:
    def test_parse_post_normal(self):
        parser = FMKoreaParser()
        post = parser.parse_file(str(FIXTURE_DIR / "post-normal.html"))

        assert post.title != ""
        assert post.body != ""
        assert post.publishedAt != ""
        assert post.firstSeenAt != ""
        assert post.viewCount > 0
        assert post.recommendationCount > 0

    def test_parse_post_deleted(self):
        parser = FMKoreaParser()
        post = parser.parse_file(str(FIXTURE_DIR / "post-deleted.html"))
        assert post.title != ""

    def test_parse_fixture_dir(self):
        parser = FMKoreaParser()
        posts = parser.parse_fixture_dir(str(FIXTURE_DIR))
        assert len(posts) >= 2
        assert all(p.title for p in posts)


# --------------------------------------------------------------------------- #
# FMKOREA_COMMENT_FIXTURE_PASS                                                #
# --------------------------------------------------------------------------- #


class TestFMKoreaCommentFixture:
    def test_parse_comments(self):
        parser = FMKoreaParser()
        post = parser.parse_file(str(FIXTURE_DIR / "post-comments.html"))

        assert len(post.comments) >= 2
        for comment in post.comments:
            assert comment.author != ""
            assert comment.body != ""

    def test_no_comments(self):
        parser = FMKoreaParser()
        post = parser.parse_file(str(FIXTURE_DIR / "post-no-comments.html"))
        assert len(post.comments) == 0
        assert post.commentCount == 0


# --------------------------------------------------------------------------- #
# LLM_MOCK_SCHEMA_PASS                                                        #
# --------------------------------------------------------------------------- #


class TestLLMMockSchema:
    def test_mock_analyze_positive(self):
        provider = MockLLMProvider()
        result = provider.analyze(
            "코스피 급등 상승 호재",
            "매수 신호 상한가 돌파",
            ["상승 확실합니다"],
        )
        assert result.direction in ("positive", "mixed")
        assert 0 <= result.score <= 1
        assert 0 <= result.fearGreed <= 100
        assert result.methodologyVersion == "sentiment-v1"
        assert result.model != ""

    def test_mock_analyze_negative(self):
        provider = MockLLMProvider()
        result = provider.analyze(
            "시장 폭락 공포",
            "매도 손실 위험 급락",
            ["하락 손절합니다"],
        )
        assert result.direction in ("negative", "mixed")
        assert -1 <= result.score <= 0

    def test_mock_analyze_unrelated(self):
        provider = MockLLMProvider()
        result = provider.analyze(
            "오늘 저녁 메뉴 추천",
            "라면이나 먹을까요",
            [],
        )
        assert result.direction == "unrelated"
        assert result.score == 0.0

    def test_mock_deterministic(self):
        """Same input produces same output."""
        provider = MockLLMProvider()
        r1 = provider.analyze("코스피 상승", "매수 호재", [])
        r2 = provider.analyze("코스피 상승", "매수 호재", [])
        assert r1.score == r2.score
        assert r1.direction == r2.direction

    def test_mock_schema_fields(self):
        provider = MockLLMProvider()
        result = provider.analyze("테스트", "테스트", [])
        required_fields = [
            "direction", "score", "positiveProbability", "negativeProbability",
            "neutralProbability", "sarcasm", "fearGreed", "marketTargets",
            "confidence", "model", "methodologyVersion",
        ]
        for field in required_fields:
            assert hasattr(result, field), f"Missing field: {field}"

    def test_create_provider_default_mock(self):
        provider = create_provider()
        assert isinstance(provider, MockLLMProvider)

    def test_direction_values(self):
        provider = MockLLMProvider()
        for direction in ["positive", "negative", "neutral", "mixed", "unrelated"]:
            assert direction in ["positive", "negative", "neutral", "mixed", "unrelated"]


# --------------------------------------------------------------------------- #
# FMINDEX_HOURLY_CALCULATION_PASS                                             #
# --------------------------------------------------------------------------- #


class TestFMIndexHourlyCalculation:
    def test_single_hour(self):
        now = datetime.now(KST).replace(minute=0, second=0, microsecond=0)
        provider = MockLLMProvider()

        posts = []
        for title, body in [
            ("코스피 상승 호재", "매수 급등"),
            ("시장 폭락 공포", "매도 급락 손실"),
            ("보합권 관망", "횡보 지지 저항"),
        ]:
            sentiment = provider.analyze(title, body, [])
            posts.append(PostWithSentiment(
                timestamp=now.isoformat(),
                sentiment=sentiment,
                commentCount=0,
            ))

        calc = FMIndexCalculator()
        results = calc.calculate_hourly(posts)

        assert len(results) == 1
        r = results[0]
        assert 0 <= r.fmIndex <= 100
        assert r.postCount == 3
        assert r.methodologyVersion == "fmindex-v1"
        assert r.timestamp.startswith(now.strftime("%Y-%m-%dT%H:00:00"))

    def test_multiple_hours(self):
        now = datetime.now(KST).replace(minute=0, second=0, microsecond=0)
        provider = MockLLMProvider()

        posts = []
        for i in range(4):
            ts = (now - timedelta(hours=3 - i)).isoformat()
            sentiment = provider.analyze("코스피 상승", "매수 호재", [])
            posts.append(PostWithSentiment(
                timestamp=ts, sentiment=sentiment, commentCount=0
            ))

        calc = FMIndexCalculator()
        results = calc.calculate_hourly(posts)

        assert len(results) == 4

    def test_unrelated_excluded(self):
        now = datetime.now(KST).replace(minute=0, second=0, microsecond=0)
        provider = MockLLMProvider()

        posts = [
            PostWithSentiment(
                timestamp=now.isoformat(),
                sentiment=provider.analyze("오늘 저녁 메뉴 추천", "라면이나 먹을까요", []),
                commentCount=0,
            ),
            PostWithSentiment(
                timestamp=now.isoformat(),
                sentiment=provider.analyze("코스피 상승", "매수 호재", []),
                commentCount=0,
            ),
        ]

        calc = FMIndexCalculator()
        results = calc.calculate_hourly(posts)

        assert len(results) == 1
        assert results[0].postCount == 2
        assert results[0].analyzedPostCount == 1  # unrelated excluded

    def test_empty_input(self):
        calc = FMIndexCalculator()
        results = calc.calculate_hourly([])
        assert results == []

    def test_fmindex_range(self):
        """FM Index should always be in 0-100 range."""
        now = datetime.now(KST).replace(minute=0, second=0, microsecond=0)
        provider = MockLLMProvider()

        posts = []
        for _ in range(10):
            sentiment = provider.analyze("폭락 손실 공포 매도", "급락 위험", [])
            posts.append(PostWithSentiment(
                timestamp=now.isoformat(), sentiment=sentiment, commentCount=0
            ))

        calc = FMIndexCalculator()
        results = calc.calculate_hourly(posts)

        assert len(results) == 1
        assert 0 <= results[0].fmIndex <= 100


# --------------------------------------------------------------------------- #
# TIMEZONE_ALIGNMENT_PASS                                                     #
# --------------------------------------------------------------------------- #


class TestTimezoneAlignment:
    def test_hour_truncation(self):
        calc = FMIndexCalculator()
        ts = "2026-08-05T09:35:42+09:00"
        truncated = calc._truncate_to_hour(ts)
        assert truncated == "2026-08-05T09:00:00+09:00"

    def test_naive_timestamp_gets_kst(self):
        calc = FMIndexCalculator()
        ts = "2026-08-05T09:35:42"
        truncated = calc._truncate_to_hour(ts)
        assert "+09:00" in truncated

    def test_market_bridge_timezone(self, tmp_path):
        data = [{"timestamp": "2026-08-05T09:05:00", "price": "100"}]
        f = tmp_path / "data.json"
        f.write_text(json.dumps(data), encoding="utf-8")

        bridge = MarketBridge()
        records = bridge.read_records_from_path(str(f))

        assert len(records) == 1
        assert "+09:00" in records[0].timestamp


# --------------------------------------------------------------------------- #
# MARKET_SENTIMENT_JOIN_PASS                                                  #
# --------------------------------------------------------------------------- #


class TestMarketSentimentJoin:
    def test_join_basic(self):
        now = datetime.now(KST).replace(minute=0, second=0, microsecond=0)
        ts = now.isoformat()

        market = [
            MarketRecord(
                timestamp=ts, market="KOSPI", open=3200, high=3210,
                low=3195, close=3205, changeRate=0.15,
                source="test", observedAt=ts,
            ),
        ]

        fm = [
            HourlyFMIndex(
                timestamp=ts, fmIndex=65.0, positiveRatio=0.6,
                negativeRatio=0.2, neutralRatio=0.2, postCount=10,
                commentCount=5, analyzedPostCount=8, confidence=0.85,
                methodologyVersion="fmindex-v1",
            ),
        ]

        joiner = MarketSentimentJoiner()
        joined = joiner.join(market, fm)

        assert len(joined) == 1
        assert joined[0].fmIndex == 65.0
        assert joined[0].marketNormalized == 100.0  # first close = 100
        assert joined[0].marketChangeRate == 0.15
        assert joined[0].postCount == 10

    def test_join_normalization(self):
        """Second record normalized relative to first."""
        ts1 = "2026-08-05T09:00:00+09:00"
        ts2 = "2026-08-05T10:00:00+09:00"

        market = [
            MarketRecord(ts1, "KOSPI", 3000, 3010, 2990, 3000, 0, "test", ts1),
            MarketRecord(ts2, "KOSPI", 3000, 3060, 2990, 3060, 2.0, "test", ts2),
        ]

        joiner = MarketSentimentJoiner()
        joined = joiner.join(market, [])

        assert joined[0].marketNormalized == 100.0
        assert joined[1].marketNormalized == 102.0  # 3060/3000 * 100

    def test_overnight_evaluation(self):
        ts = "2026-08-05T09:00:00+09:00"
        fm = [
            HourlyFMIndex(ts, 30.0, 0.1, 0.7, 0.2, 5, 10, 4, 0.8, "fmindex-v1"),
            HourlyFMIndex(ts, 25.0, 0.1, 0.8, 0.1, 3, 8, 3, 0.7, "fmindex-v1"),
        ]
        market = [
            MarketRecord(ts, "KOSPI", 3000, 3010, 2980, 2990, -0.33, "test", ts),
            MarketRecord(ts, "KOSPI", 2990, 3000, 2970, 2980, -0.33, "test", ts),
        ]

        joiner = MarketSentimentJoiner()
        eval_result = joiner.evaluate_overnight(fm, market, "2026-08-06")

        assert eval_result is not None
        assert eval_result.overnight["signal"] == "negative"
        assert "open" in eval_result.matches
        assert "close" in eval_result.matches


# --------------------------------------------------------------------------- #
# DASHBOARD_DATA_EXPORT_PASS                                                  #
# --------------------------------------------------------------------------- #


class TestDashboardExport:
    def test_generate_dashboard_data(self):
        joined = [
            JoinedRecord(
                timestamp="2026-08-05T09:00:00+09:00",
                market="KOSPI", fmIndex=55.0, marketNormalized=100.0,
                marketChangeRate=0.5, postCount=10, confidence=0.8,
            ).to_dict()
        ]
        overnight = {
            "tradeDate": "2026-08-06",
            "overnight": {"averageFmIndex": 30.0, "signal": "negative"},
            "kospi": {"openChangeRate": -0.5},
            "matches": {"open": True, "firstHour": True, "close": False},
        }
        summary = {"totalPosts": 10, "totalAnalyzed": 8}

        data = generate_dashboard_data(joined, overnight, summary)

        assert "joined" in data
        assert "overnight" in data
        assert "summary" in data
        assert len(data["joined"]) == 1

    def test_write_dashboard_files(self, tmp_path):
        joined = [
            JoinedRecord(
                timestamp="2026-08-05T09:00:00+09:00",
                market="KOSPI", fmIndex=55.0, marketNormalized=100.0,
                marketChangeRate=0.5, postCount=10, confidence=0.8,
            ).to_dict()
        ]

        html_path = write_dashboard_files(str(tmp_path), joined, None, None)

        assert Path(html_path).exists()
        assert (tmp_path / "api" / "data.json").exists()

        data = json.loads((tmp_path / "api" / "data.json").read_text(encoding="utf-8"))
        assert "joined" in data


# --------------------------------------------------------------------------- #
# PIPELINE_INTEGRATION_PASS                                                   #
# --------------------------------------------------------------------------- #


class TestPipelineIntegration:
    def test_pipeline_once_with_fixtures(self, tmp_path):
        """Full pipeline run with fixtures and sample data."""
        results = run_pipeline_once(
            market_data_path=None,  # will use sample
            fmkorea_fixture_dir=str(FIXTURE_DIR),
            output_dir=str(tmp_path),
        )

        assert "steps" in results
        step_names = [s["step"] for s in results["steps"]]
        assert "market_bridge" in step_names
        assert "fmkorea_parse" in step_names
        assert "llm_sentiment" in step_names
        assert "fmindex_hourly" in step_names
        assert "market_join" in step_names
        assert "dashboard_export" in step_names

        # Check output files
        assert (tmp_path / "index.html").exists()
        assert (tmp_path / "api" / "data.json").exists()
        assert (tmp_path / "pipeline_output.json").exists()

        # Check summary
        summary = results["summary"]
        assert summary["totalPosts"] > 0
        assert summary["totalAnalyzed"] > 0

    def test_pipeline_no_network(self, tmp_path):
        """Pipeline must not make network calls."""
        # This test passes if no exception is raised
        # and no network is needed (mock LLM provider)
        results = run_pipeline_once(
            market_data_path=None,
            fmkorea_fixture_dir=str(FIXTURE_DIR),
            output_dir=str(tmp_path),
        )

        for step in results["steps"]:
            assert step["status"] in ("ok", "sample"), f"Step {step['step']} failed: {step}"
