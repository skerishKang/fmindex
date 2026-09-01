"""Integration Slice 1 tests — all tests run offline without network."""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Ensure package is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fmindex.market.bridge import MarketBridge, MarketRecord, Tick, Candle
from fmindex.fmkorea.parser import FMKoreaParser, ParsedPost, Comment
from fmindex.llm.provider import MockLLMProvider, SentimentResult, create_provider
from fmindex.fmindex_calc import FMIndexCalculator, PostWithSentiment, HourlyFMIndex
from fmindex.market_join import MarketSentimentJoiner, JoinedRecord, OvernightEvaluation
from fmindex.dashboard.server import generate_dashboard_data, write_dashboard_files, filter_by_period
from fmindex.pipeline import run_pipeline_once

KST = timezone(timedelta(hours=9))
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "fmkorea"


# --------------------------------------------------------------------------- #
# IRRELEVANT_FMINDEX_ALGORITHM_REMOVED                                         #
# --------------------------------------------------------------------------- #


class TestIrrelevantAlgorithmRemoved:
    def test_package_has_no_string_search_fmindex(self):
        """String-search FM-Index data structure must not be exported."""
        import fmindex
        assert not hasattr(fmindex, "FMIndex")
        assert not hasattr(fmindex, "bwt_from_suffix_array")
        assert not hasattr(fmindex, "inverse_bwt")
        assert not hasattr(fmindex, "build_suffix_array")

    def test_package_version_is_0_1_0(self):
        import fmindex
        assert fmindex.__version__ == "0.1.0"

    def test_algorithm_modules_deleted(self):
        """The string-search algorithm modules must be physically removed."""
        pkg_dir = Path(__file__).resolve().parents[1] / "fmindex"
        assert not (pkg_dir / "bwt.py").exists()
        assert not (pkg_dir / "index.py").exists()
        assert not (Path(__file__).resolve().parents[1] / "demo.py").exists()
        assert not (Path(__file__).resolve().parents[1] / "tests" / "test_fmindex.py").exists()


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
# MARKET_INSTRUMENT_VALIDATION_PASS                                           #
# --------------------------------------------------------------------------- #


class TestMarketInstrumentValidation:
    def test_record_has_instrument_id(self, tmp_path):
        """MarketRecord includes instrument_id field."""
        data = [
            {"timestamp": "2026-08-05T09:05:00+09:00", "price": "3200.5"},
        ]
        f = tmp_path / "prices.json"
        f.write_text(json.dumps(data), encoding="utf-8")

        bridge = MarketBridge()
        records = bridge.read_records_from_path(str(f))

        assert len(records) == 1
        assert hasattr(records[0], "instrument_id")
        assert records[0].instrument_id == ""

    def test_record_has_symbol(self, tmp_path):
        """MarketRecord includes symbol field."""
        data = [
            {"timestamp": "2026-08-05T09:05:00+09:00", "price": "3200.5"},
        ]
        f = tmp_path / "prices.json"
        f.write_text(json.dumps(data), encoding="utf-8")

        bridge = MarketBridge()
        records = bridge.read_records_from_path(str(f))

        assert len(records) == 1
        assert hasattr(records[0], "symbol")

    def test_record_has_data_mode(self, tmp_path):
        """MarketRecord includes data_mode field."""
        data = [
            {"timestamp": "2026-08-05T09:05:00+09:00", "price": "3200.5"},
        ]
        f = tmp_path / "prices.json"
        f.write_text(json.dumps(data), encoding="utf-8")

        bridge = MarketBridge()
        records = bridge.read_records_from_path(str(f))

        assert len(records) == 1
        assert hasattr(records[0], "data_mode")
        assert records[0].data_mode in ("real", "sample")


# --------------------------------------------------------------------------- #
# NON_INDEX_DATA_REJECTED_PASS                                                #
# --------------------------------------------------------------------------- #


class TestNonIndexDataRejected:
    def test_individual_stock_not_shown_as_kospi(self, tmp_path):
        """Individual stock data must not be presented as the KOSPI index."""
        data = [
            {
                "timestamp": "2026-08-05T09:05:00+09:00",
                "price": "73000",
                "instrumentId": "005930",
                "symbol": "삼성전자",
            },
            {
                "timestamp": "2026-08-05T09:15:00+09:00",
                "price": "73100",
                "instrumentId": "005930",
                "symbol": "삼성전자",
            },
        ]
        f = tmp_path / "stock.json"
        f.write_text(json.dumps(data), encoding="utf-8")

        bridge = MarketBridge()
        records = bridge.read_records_from_path(str(f))

        # Individual stock records are rejected — never merged into KOSPI series
        assert records == []
        assert bridge.rejected_non_index >= 1

    def test_stock_code_detected_even_without_symbol(self, tmp_path):
        """A 6-digit Korean stock code is rejected as non-index data."""
        data = [
            {"timestamp": "2026-08-05T09:05:00+09:00", "price": "73000", "code": "005930"},
        ]
        f = tmp_path / "stock.json"
        f.write_text(json.dumps(data), encoding="utf-8")

        bridge = MarketBridge()
        records = bridge.read_records_from_path(str(f))

        assert records == []
        assert bridge.rejected_non_index >= 1

    def test_explicit_index_data_accepted(self, tmp_path):
        """Explicit KOSPI index identity is accepted."""
        data = [
            {
                "timestamp": "2026-08-05T09:05:00+09:00",
                "price": "3200.5",
                "instrumentId": "KOSPI",
                "symbol": "KOSPI",
            },
        ]
        f = tmp_path / "index.json"
        f.write_text(json.dumps(data), encoding="utf-8")

        bridge = MarketBridge()
        records = bridge.read_records_from_path(str(f))

        assert len(records) == 1
        assert records[0].instrument_id == "KOSPI"
        assert records[0].symbol == "KOSPI"


# --------------------------------------------------------------------------- #
# MULTI_SYMBOL_DATA_NOT_MERGED_PASS                                           #
# --------------------------------------------------------------------------- #


class TestMultiSymbolDataNotMerged:
    def test_multiple_stocks_not_merged_into_one_series(self, tmp_path):
        """Multiple individual stocks must never be merged into one index series."""
        data = [
            {
                "timestamp": "2026-08-05T09:05:00+09:00",
                "price": "73000",
                "instrumentId": "005930",
                "symbol": "삼성전자",
            },
            {
                "timestamp": "2026-08-05T09:10:00+09:00",
                "price": "180000",
                "instrumentId": "000660",
                "symbol": "SK하이닉스",
            },
        ]
        f = tmp_path / "multi.json"
        f.write_text(json.dumps(data), encoding="utf-8")

        bridge = MarketBridge()
        records = bridge.read_records_from_path(str(f))

        # Both are individual stocks; neither may appear as KOSPI.
        assert records == []

    def test_mixed_index_and_stock_keeps_only_index(self, tmp_path):
        """When index and stock data are mixed, only the index series is kept."""
        data = [
            {
                "timestamp": "2026-08-05T09:05:00+09:00",
                "price": "3200.5",
                "instrumentId": "KOSPI",
                "symbol": "KOSPI",
            },
            {
                "timestamp": "2026-08-05T09:15:00+09:00",
                "price": "73000",
                "instrumentId": "005930",
                "symbol": "삼성전자",
            },
        ]
        f = tmp_path / "mixed.json"
        f.write_text(json.dumps(data), encoding="utf-8")

        bridge = MarketBridge()
        records = bridge.read_records_from_path(str(f))

        assert len(records) == 1
        assert records[0].instrument_id == "KOSPI"
        assert records[0].symbol == "KOSPI"


# --------------------------------------------------------------------------- #
# CANDLE_OHLC_PRESERVED_PASS                                                  #
# --------------------------------------------------------------------------- #


class TestCandleOhlcPreserved:
    def test_candle_ohlc_not_recalculated(self, tmp_path):
        """Candle input preserves original OHLC, does not recalculate from close list."""
        data = [
            {
                "timestamp": "2026-08-05T09:05:00+09:00",
                "open": "100", "high": "110", "low": "95", "close": "105",
            },
        ]
        f = tmp_path / "candles.json"
        f.write_text(json.dumps(data), encoding="utf-8")

        bridge = MarketBridge()
        records = bridge.read_records_from_path(str(f))

        assert len(records) == 1
        assert records[0].open == 100.0
        assert records[0].high == 110.0
        assert records[0].low == 95.0
        assert records[0].close == 105.0

    def test_multi_candle_ohlc_aggregation(self, tmp_path):
        """Multiple candles in an hour: open=first, high=max, low=min, close=last."""
        data = [
            {
                "timestamp": "2026-08-05T09:05:00+09:00",
                "open": "100", "high": "110", "low": "95", "close": "105",
            },
            {
                "timestamp": "2026-08-05T09:35:00+09:00",
                "open": "105", "high": "120", "low": "98", "close": "115",
            },
        ]
        f = tmp_path / "candles.json"
        f.write_text(json.dumps(data), encoding="utf-8")

        bridge = MarketBridge()
        records = bridge.read_records_from_path(str(f))

        assert len(records) == 1
        assert records[0].open == 100.0   # first candle open
        assert records[0].high == 120.0   # max of highs
        assert records[0].low == 95.0     # min of lows
        assert records[0].close == 115.0  # last candle close


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
# UNIMPLEMENTED_PROVIDER_FAILS_PASS                                           #
# --------------------------------------------------------------------------- #


class TestUnimplementedProviderFails:
    def test_non_mock_provider_raises(self):
        """Non-mock provider names raise NotImplementedError."""
        os.environ["FMINDEX_LLM_PROVIDER"] = "openai"
        try:
            with pytest.raises(NotImplementedError):
                create_provider()
        finally:
            del os.environ["FMINDEX_LLM_PROVIDER"]

    def test_mock_provider_still_works(self):
        """Mock provider still works after the change."""
        os.environ["FMINDEX_LLM_PROVIDER"] = "mock"
        try:
            provider = create_provider()
            assert isinstance(provider, MockLLMProvider)
        finally:
            del os.environ["FMINDEX_LLM_PROVIDER"]


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
        assert r.fmIndex is not None
        assert 0 <= r.fmIndex <= 100
        assert r.has_sentiment is True
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
        assert results[0].fmIndex is not None
        assert 0 <= results[0].fmIndex <= 100


# --------------------------------------------------------------------------- #
# MISSING_SENTIMENT_IS_NULL_PASS                                              #
# --------------------------------------------------------------------------- #


class TestMissingSentimentIsNull:
    def test_no_posts_returns_null_fmindex(self):
        """When no posts exist, fmIndex is null, not 50."""
        calc = FMIndexCalculator()
        results = calc.calculate_hourly([])
        assert results == []

    def test_only_unrelated_posts_returns_null_fmindex(self):
        """When all posts are unrelated, fmIndex is null and hasSentiment is False."""
        now = datetime.now(KST).replace(minute=0, second=0, microsecond=0)
        provider = MockLLMProvider()

        posts = [
            PostWithSentiment(
                timestamp=now.isoformat(),
                sentiment=provider.analyze("오늘 저녁 메뉴 추천", "라면이나 먹을까요", []),
                commentCount=0,
            ),
        ]

        calc = FMIndexCalculator()
        results = calc.calculate_hourly(posts)

        assert len(results) == 1
        assert results[0].fmIndex is None
        assert results[0].has_sentiment is False
        assert results[0].postCount == 1
        assert results[0].confidence == 0.0


# --------------------------------------------------------------------------- #
# ZERO_FMINDEX_PRESERVED_PASS                                                 #
# --------------------------------------------------------------------------- #


class TestZeroFmIndexPreserved:
    def test_zero_fmindex_is_preserved(self):
        """fmIndex=0 is preserved as-is, not treated as missing or replaced by 50."""
        now = datetime.now(KST).replace(minute=0, second=0, microsecond=0)
        provider = MockLLMProvider()

        # Extreme negative sentiment yields avg_score=-1.0 → fmIndex = 0
        posts = []
        for _ in range(10):
            sentiment = provider.analyze("폭락 공포 매도 손실", "급락 하락 위험", [])
            assert sentiment.direction == "negative"
            posts.append(PostWithSentiment(
                timestamp=now.isoformat(), sentiment=sentiment, commentCount=0
            ))

        calc = FMIndexCalculator()
        results = calc.calculate_hourly(posts)

        assert len(results) == 1
        assert results[0].fmIndex == 0.0          # actual zero, not null
        assert results[0].has_sentiment is True    # not a missing bucket

    def test_to_dict_preserves_zero(self):
        """HourlyFMIndex.to_dict keeps 0.0 as 0.0 (not null)."""
        now = datetime.now(KST).replace(minute=0, second=0, microsecond=0)
        provider = MockLLMProvider()

        posts = []
        for _ in range(10):
            sentiment = provider.analyze("폭락 공포 매도 손실", "급락 하락 위험", [])
            posts.append(PostWithSentiment(
                timestamp=now.isoformat(), sentiment=sentiment, commentCount=0
            ))

        calc = FMIndexCalculator()
        results = calc.calculate_hourly(posts)
        d = results[0].to_dict()

        assert d["fmIndex"] == 0.0
        assert d["hasSentiment"] is True


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

    def test_utc_to_kst_alignment(self, tmp_path):
        """UTC timestamps are converted to Asia/Seoul (KST)."""
        utc_ts = "2026-08-05T00:05:00+00:00"
        data = [{"timestamp": utc_ts, "price": "100"}]
        f = tmp_path / "data.json"
        f.write_text(json.dumps(data), encoding="utf-8")

        bridge = MarketBridge()
        records = bridge.read_records_from_path(str(f))

        assert len(records) == 1
        # UTC+00:00 00:05 should become KST+09:00 09:05
        assert "+09:00" in records[0].timestamp
        assert "09:00:00" in records[0].timestamp


# --------------------------------------------------------------------------- #
# MARKET_SENTIMENT_JOIN_PASS                                                  #
# --------------------------------------------------------------------------- #


class TestMarketSentimentJoin:
    def test_join_basic(self):
        now = datetime.now(KST).replace(minute=0, second=0, microsecond=0)
        ts = now.isoformat()

        market = [
            MarketRecord(
                timestamp=ts, market="KOSPI", instrument_id="KOSPI", symbol="KOSPI",
                open=3200, high=3210,
                low=3195, close=3205, change_rate=0.15,
                source="test", observed_at=ts, data_mode="sample",
            ),
        ]

        fm = [
            HourlyFMIndex(
                timestamp=ts, fmIndex=65.0, has_sentiment=True,
                positiveRatio=0.6,
                negativeRatio=0.2, neutralRatio=0.2, postCount=10,
                commentCount=5, analyzedPostCount=8, confidence=0.85,
                methodologyVersion="fmindex-v1",
            ),
        ]

        joiner = MarketSentimentJoiner()
        joined = joiner.join(market, fm)

        assert len(joined) == 1
        assert joined[0].fmIndex == 65.0
        assert joined[0].has_sentiment is True
        assert joined[0].marketNormalized == 100.0  # first close = 100
        assert joined[0].marketChangeRate == 0.15
        assert joined[0].postCount == 10
        assert joined[0].instrument_id == "KOSPI"
        assert joined[0].symbol == "KOSPI"
        assert joined[0].data_mode == "sample"

    def test_join_normalization(self):
        """Second record normalized relative to first."""
        ts1 = "2026-08-05T09:00:00+09:00"
        ts2 = "2026-08-05T10:00:00+09:00"

        market = [
            MarketRecord(ts1, "KOSPI", "KOSPI", "KOSPI", 3000, 3010, 2990, 3000, 0, "test", ts1, "sample"),
            MarketRecord(ts2, "KOSPI", "KOSPI", "KOSPI", 3000, 3060, 2990, 3060, 2.0, "test", ts2, "sample"),
        ]

        joiner = MarketSentimentJoiner()
        joined = joiner.join(market, [])

        assert joined[0].marketNormalized == 100.0
        assert joined[1].marketNormalized == 102.0  # 3060/3000 * 100

    def test_join_missing_sentiment_is_null(self):
        """When no FM Index data exists for a timestamp, fmIndex is null."""
        ts = "2026-08-05T09:00:00+09:00"

        market = [
            MarketRecord(ts, "KOSPI", "KOSPI", "KOSPI", 3000, 3010, 2990, 3000, 0, "test", ts, "sample"),
        ]

        joiner = MarketSentimentJoiner()
        joined = joiner.join(market, [])

        assert len(joined) == 1
        assert joined[0].fmIndex is None
        assert joined[0].has_sentiment is False
        assert joined[0].postCount == 0
        assert joined[0].confidence == 0.0


# --------------------------------------------------------------------------- #
# JSON_CONTRACT_CAMELCASE_PASS                                                #
# --------------------------------------------------------------------------- #


class TestJsonContractCamelCase:
    def test_market_record_to_dict_camel_case(self):
        ts = "2026-08-05T09:00:00+09:00"
        rec = MarketRecord(
            timestamp=ts, market="KOSPI", instrument_id="KOSPI", symbol="KOSPI",
            open=3000, high=3010, low=2990, close=3000, change_rate=0.0,
            source="test", observed_at=ts, data_mode="sample",
        )
        d = rec.to_dict()

        assert d["instrumentId"] == "KOSPI"
        assert d["dataMode"] == "sample"
        assert d["observedAt"] == ts
        assert d["changeRate"] == 0.0
        # snake_case must not leak into the external JSON contract
        assert "instrument_id" not in d
        assert "data_mode" not in d
        assert "observed_at" not in d
        assert "change_rate" not in d

    def test_joined_record_to_dict_camel_case(self):
        now = datetime.now(KST).replace(minute=0, second=0, microsecond=0)
        ts = now.isoformat()

        joined = JoinedRecord(
            timestamp=ts, market="KOSPI", instrument_id="KOSPI", symbol="KOSPI",
            fmIndex=None, has_sentiment=False,
            marketNormalized=100.0, marketChangeRate=0.0,
            postCount=0, confidence=0.0, data_mode="sample",
        )
        d = joined.to_dict()

        assert d["instrumentId"] == "KOSPI"
        assert d["hasSentiment"] is False
        assert d["dataMode"] == "sample"
        assert "instrument_id" not in d
        assert "has_sentiment" not in d
        assert "data_mode" not in d

    def test_hourly_fmindex_to_dict_camel_case(self):
        now = datetime.now(KST).replace(minute=0, second=0, microsecond=0)
        ts = now.isoformat()

        fm = HourlyFMIndex(
            timestamp=ts, fmIndex=None, has_sentiment=False,
            positiveRatio=0.0, negativeRatio=0.0, neutralRatio=0.0,
            postCount=0, commentCount=0, analyzedPostCount=0,
            confidence=0.0, methodologyVersion="fmindex-v1",
        )
        d = fm.to_dict()

        assert d["hasSentiment"] is False
        assert "has_sentiment" not in d


# --------------------------------------------------------------------------- #
# OVERNIGHT_UNAVAILABLE_PASS                                                  #
# --------------------------------------------------------------------------- #


class TestOvernightUnavailable:
    def test_overnight_always_unavailable(self):
        """Overnight evaluation returns unavailable status in this slice."""
        ts = "2026-08-05T09:00:00+09:00"
        fm = [
            HourlyFMIndex(ts, 30.0, True, 0.1, 0.7, 0.2, 5, 10, 4, 0.8, "fmindex-v1"),
            HourlyFMIndex(ts, 25.0, True, 0.1, 0.8, 0.1, 3, 8, 3, 0.7, "fmindex-v1"),
        ]
        market = [
            MarketRecord(ts, "KOSPI", "KOSPI", "KOSPI", 3000, 3010, 2980, 2990, -0.33, "test", ts, "sample"),
            MarketRecord(ts, "KOSPI", "KOSPI", "KOSPI", 2990, 3000, 2970, 2980, -0.33, "test", ts, "sample"),
        ]

        joiner = MarketSentimentJoiner()
        eval_result = joiner.evaluate_overnight(fm, market, "2026-08-06")

        assert eval_result is not None
        assert eval_result.overnight["status"] == "unavailable"
        assert eval_result.overnight["reason"] == "real_session_data_not_available"


# --------------------------------------------------------------------------- #
# LOCALHOST_BIND_DEFAULT_PASS                                                 #
# --------------------------------------------------------------------------- #


class TestLocalhostBindDefault:
    def test_serve_dashboard_defaults_to_localhost(self):
        """Dashboard server defaults to 127.0.0.1 binding."""
        from fmindex.dashboard.server import serve_dashboard
        import inspect
        sig = inspect.signature(serve_dashboard)
        host_param = sig.parameters.get("host")
        assert host_param is not None
        assert host_param.default == "127.0.0.1"


# --------------------------------------------------------------------------- #
# TIMESTAMP_PERIOD_FILTER_PASS                                                #
# --------------------------------------------------------------------------- #


class TestTimestampPeriodFilter:
    def test_filter_24h(self):
        """24h filter uses timestamp, not record count."""
        now = datetime.now(KST)
        data = [
            {
                "timestamp": now.isoformat(),
                "fmIndex": 55.0,
                "marketNormalized": 100.0,
            },
            {
                "timestamp": (now - timedelta(hours=25)).isoformat(),
                "fmIndex": 50.0,
                "marketNormalized": 100.0,
            },
        ]
        filtered = filter_by_period(data, "24h")
        assert len(filtered) == 1
        assert filtered[0]["fmIndex"] == 55.0

    def test_filter_7d(self):
        """7d filter uses timestamp, not record count."""
        now = datetime.now(KST)
        data = [
            {
                "timestamp": now.isoformat(),
                "fmIndex": 55.0,
                "marketNormalized": 100.0,
            },
            {
                "timestamp": (now - timedelta(days=8)).isoformat(),
                "fmIndex": 50.0,
                "marketNormalized": 100.0,
            },
        ]
        filtered = filter_by_period(data, "7d")
        assert len(filtered) == 1

    def test_filter_30d(self):
        """30d filter uses timestamp, not record count."""
        now = datetime.now(KST)
        data = [
            {
                "timestamp": now.isoformat(),
                "fmIndex": 55.0,
                "marketNormalized": 100.0,
            },
            {
                "timestamp": (now - timedelta(days=31)).isoformat(),
                "fmIndex": 50.0,
                "marketNormalized": 100.0,
            },
        ]
        filtered = filter_by_period(data, "30d")
        assert len(filtered) == 1

    def test_filter_all(self):
        """All period returns all records."""
        now = datetime.now(KST)
        data = [
            {
                "timestamp": (now - timedelta(days=1)).isoformat(),
                "fmIndex": 55.0,
                "marketNormalized": 100.0,
            },
            {
                "timestamp": (now - timedelta(days=31)).isoformat(),
                "fmIndex": 50.0,
                "marketNormalized": 100.0,
            },
        ]
        filtered = filter_by_period(data, "all")
        assert len(filtered) == 2


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
        assert "provider" in summary
        assert "instrument" in summary
        assert "symbol" in summary
        assert "dataMode" in summary

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

    def test_pipeline_rejects_individual_stock_data(self, tmp_path):
        """Pipeline with only stock data falls back to sample KOSPI data."""
        stock_data = [
            {
                "timestamp": "2026-08-05T09:05:00+09:00",
                "price": "73000",
                "instrumentId": "005930",
                "symbol": "삼성전자",
            },
        ]
        data_file = tmp_path / "stock.json"
        data_file.write_text(json.dumps(stock_data), encoding="utf-8")

        results = run_pipeline_once(
            market_data_path=str(data_file),
            fmkorea_fixture_dir=str(FIXTURE_DIR),
            output_dir=str(tmp_path / "out"),
        )

        market_step = next(s for s in results["steps"] if s["step"] == "market_bridge")
        # Stock data is rejected; pipeline must not present it as real KOSPI.
        assert market_step["status"] in ("ok", "sample")
        assert market_step.get("rejectedNonIndex", 0) >= 1
        # Sample data mode is explicit in the summary.
        assert results["summary"]["dataMode"] == "sample"
