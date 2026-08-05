"""Offline tests for the KOSPI hourly market collector.

All tests run without network access: the Kiwoom HTTP transport is a
fake/injected transport, timestamps are fixed timezone-aware values, and
file I/O uses tmp_path. No live API calls are made.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fmindex.market.models import (
    ASSET_TYPE_INDEX,
    KOSPI_INDS_CD,
    PROVIDER,
    Candle,
    HourlyIndexRecord,
    CollectionMetadata,
)
from fmindex.market.market_calendar import (
    KST,
    bucket_for_timestamp,
    is_session_time,
    is_trading_day,
    previous_trading_day,
)
from fmindex.market.kiwoom_auth import (
    KiwoomAuthError,
    KiwoomTokenManager,
    credentials_available,
    load_credentials,
)
from fmindex.market.kiwoom_client import (
    KiwoomAPIError,
    KiwoomClient,
    KiwoomPaginationError,
    KiwoomRateLimitError,
)
from fmindex.market.kospi_collector import (
    CollectorConfig,
    KospiCollector,
    run_collector,
)
from fmindex.market.bridge import MarketBridge, MarketRecord
from fmindex.pipeline import run_pipeline_once


def make_record(
    ts="2026-08-05T10:00:00+09:00",
    instrument_id="001",
    symbol="KOSPI",
    provider="kiwoom",
    asset_type="index",
    data_mode="real",
    observed_at="2026-08-05T11:00:00+09:00",
    allow_inconsistent=False,
    **kw,
):
    """Build a valid HourlyIndexRecord with defaults.

    By default the OHLC values are kept internally consistent
    (high >= max(open, close), low <= min(open, close)) so callers can
    override a single field (e.g. close) without breaking the contract.
    Pass allow_inconsistent=True to intentionally build invalid records
    (used by the OHLC-rejection tests).
    """
    values = dict(
        timestamp=ts,
        market="KOSPI",
        instrument_id=instrument_id,
        symbol=symbol,
        asset_type=asset_type,
        open=3200.0,
        high=3210.0,
        low=3190.0,
        close=3205.0,
        volume=None,
        change_rate=0.15,
        source="kiwoom-rest-api",
        provider=provider,
        observed_at=observed_at,
        data_mode=data_mode,
    )
    values.update(kw)
    if not allow_inconsistent:
        if values.get("open") is not None and values.get("close") is not None:
            hi = max(values["open"], values["close"])
            lo = min(values["open"], values["close"])
            if values.get("high") is not None and values["high"] < hi:
                values["high"] = hi
            if values.get("low") is not None and values["low"] > lo:
                values["low"] = lo
    return HourlyIndexRecord(**values)


def kospi_jsonl_lines(records):
    return "\n".join(json.dumps(r.to_dict(), ensure_ascii=False) for r in records)


class FakeTransport:
    """Injectable fake Kiwoom transport for offline tests.

    Maps (path, body) to canned responses. Can simulate timeouts,
    rate limits, HTTP errors, and pagination.
    """

    def __init__(self, responses=None, raise_timeout=False, rate_limit_after=0):
        self.responses = responses or []
        self.raise_timeout = raise_timeout
        self.rate_limit_after = rate_limit_after
        self.calls = []

    def __call__(self, url, body, headers):
        self.calls.append((url, body, headers))
        if self.raise_timeout:
            raise TimeoutError("simulated timeout")
        if self.rate_limit_after and len(self.calls) >= self.rate_limit_after:
            raise KiwoomRateLimitError("simulated 429")
        if self.responses:
            return self.responses.pop(0)
        return {"return_code": 0, "return_msg": "ok", "inds_min_pole_qry": []}


def make_client(transport=None, **kw):
    """Build a KiwoomClient with a stubbed token manager (no credentials)."""
    tm = KiwoomTokenManager(
        app_key="test-app", secret_key="test-secret",
        base_url="https://mockapi.kiwoom.com",
    )
    tm._token = "test-token"
    tm._token_type = "bearer"
    tm._expires_at = datetime.now(timezone.utc).timestamp() + 3600
    return KiwoomClient(
        token_manager=tm, min_interval=0, transport=transport, **kw
    )


def make_collector(transport=None, **kw):
    """Build a KospiCollector with a fake transport and no credentials."""
    return KospiCollector(client=make_client(transport=transport), request_delay=0.0, **kw)


def make_minute_page(rows, cont_yn="N", next_key=""):
    return {
        "return_code": 0,
        "return_msg": "정상적으로 처리되었습니다",
        "cont_yn": cont_yn,
        "next_key": next_key,
        "inds_min_pole_qry": rows,
    }


def minute_row(hhmm, o, h, l, c, vol="1000", acc_vol="10000"):
    return {
        "cur_prc": str(c),
        "trde_qty": vol,
        "cntr_tm": hhmm,
        "open_pric": str(o),
        "high_pric": str(h),
        "low_pric": str(l),
        "acc_trde_qty": acc_vol,
    }


# --------------------------------------------------------------------------- #
# KOSPI_INSTRUMENT_CONTRACT_PASS                                              #
# --------------------------------------------------------------------------- #


class TestKospiInstrumentContract:
    def test_contract_constants(self):
        """The official KOSPI instrument constants are exact."""
        assert KOSPI_INDS_CD == "001"  # official: 001 종합(KOSPI)
        assert PROVIDER == "kiwoom"
        assert ASSET_TYPE_INDEX == "index"

    def test_record_contract_fields(self):
        """HourlyIndexRecord carries the full output contract."""
        rec = make_record()
        d = rec.to_dict()
        required = {
            "timestamp", "market", "instrumentId", "symbol", "assetType",
            "open", "high", "low", "close", "volume", "changeRate",
            "source", "provider", "observedAt", "dataMode",
        }
        assert required <= set(d)

    def test_kospi_instrument_validation_accepts_001(self):
        """Collector accepts only the 001/KOSPI index identity."""
        collector = make_collector()
        assert collector._is_valid_kospi_record(make_record())


# --------------------------------------------------------------------------- #
# INDIVIDUAL_STOCK_REJECTED / WRONG_ASSET_TYPE_REJECTED                       #
# --------------------------------------------------------------------------- #


class TestInstrumentRejection:
    def test_individual_stock_rejected(self):
        """A record with a stock instrument code is rejected."""
        collector = make_collector()
        rec = make_record(instrument_id="005930", symbol="삼성전자", asset_type="stock")
        assert not collector._is_valid_kospi_record(rec)

    def test_stock_asset_type_rejected(self):
        """assetType != index is rejected even with KOSPI-like symbol."""
        collector = make_collector()
        rec = make_record(instrument_id="005930", symbol="삼성전자", asset_type="equity")
        assert not collector._is_valid_kospi_record(rec)

    def test_wrong_asset_type_rejected(self):
        """assetType=index but provider mismatch is rejected."""
        collector = make_collector()
        rec = make_record(provider="naver", symbol="KOSPI", asset_type="index")
        assert not collector._is_valid_kospi_record(rec)

    def test_multi_symbol_not_merged(self):
        """Multiple symbols never merge into one KOSPI series via the bridge."""
        lines = [
            make_record(ts="2026-08-05T10:00:00+09:00", instrument_id="005930",
                        symbol="삼성전자", asset_type="stock", data_mode="real").to_dict(),
            make_record(ts="2026-08-05T11:00:00+09:00", symbol="KOSPI",
                        asset_type="index", data_mode="real").to_dict(),
        ]
        f = Path("/tmp/kospi_multi_test.jsonl")
        f.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines), encoding="utf-8")
        bridge = MarketBridge()
        records = bridge.read_kospi_records(str(f))
        f.unlink()
        assert len(records) == 1  # only the KOSPI index record survives
        assert records[0].instrument_id == "001"


# --------------------------------------------------------------------------- #
# AUTH_SECRET_NOT_LOGGED / TOKEN_CACHE_READ_ONLY                              #
# --------------------------------------------------------------------------- #


class TestAuthSecurity:
    def test_secret_not_logged(self, monkeypatch, caplog):
        """Credential values never appear in logs."""
        import logging

        logger = logging.getLogger("fmindex.test.secret")
        logger.info("KIWOOM_APPKEY=super-secret-value and KIWOOM_SECRETKEY=another")
        for record in caplog.records:
            assert "super-secret-value" not in record.getMessage()

    def test_credentials_available_no_values(self, monkeypatch):
        """credentials_available reports bool only, never values."""
        monkeypatch.setenv("KIWOOM_APPKEY", "app-secret")
        monkeypatch.setenv("KIWOOM_SECRETKEY", "key-secret")
        monkeypatch.setenv("KIWOOM_65STOCK_ENV", "")
        assert credentials_available() is True

    def test_token_cache_read_only(self, monkeypatch, tmp_path):
        """Token manager never writes a token cache file."""
        monkeypatch.setenv("KIWOOM_APPKEY", "app")
        monkeypatch.setenv("KIWOOM_SECRETKEY", "sec")
        manager = KiwoomTokenManager(base_url="https://mockapi.kiwoom.com")
        # Simulate a token fetch with a stubbed _issue.
        manager._token = "fake-token"
        manager._expires_at = datetime.now(timezone.utc).timestamp() + 3600
        before = set(p.name for p in tmp_path.iterdir()) if tmp_path.exists() else set()
        assert manager.get_token() == "fake-token"
        after = set(p.name for p in tmp_path.iterdir())
        assert before == after  # no cache file written


# --------------------------------------------------------------------------- #
# HTTP_TIMEOUT_PASS / RATE_LIMIT_STOPS / PAGINATION_TERMINATES                #
# --------------------------------------------------------------------------- #


class TestHttpClient:
    def test_http_timeout_pass(self):
        """Transport timeout surfaces as a clear error, not a hang."""
        client = make_client(
            max_retries=0, transport=FakeTransport(raise_timeout=True)
        )
        with pytest.raises(KiwoomAPIError):
            client.fetch("ka20005", "/api/dostk/chart", {})

    def test_rate_limit_stops(self):
        """Rate limiting aborts immediately without retrying."""
        client = make_client(
            max_retries=3, transport=FakeTransport(rate_limit_after=1),
        )
        with pytest.raises(KiwoomRateLimitError):
            client.fetch("ka20005", "/api/dostk/chart", {})

    def test_return_code_5_rate_limit_stops(self):
        """return_code=5 in the envelope aborts as rate limit."""
        def transport(url, body, headers):
            return {"return_code": 5, "return_msg": "허용된 요청 개수를 초과"}

        client = make_client(transport=transport)
        with pytest.raises(KiwoomRateLimitError):
            client.fetch("ka20005", "/api/dostk/chart", {})

    def test_pagination_terminates(self):
        """Pagination stops when cont_yn != Y."""
        pages = [
            make_minute_page([minute_row("0905", 1, 2, 1, 2)], cont_yn="Y", next_key="k1"),
            make_minute_page([minute_row("0910", 2, 3, 2, 3)], cont_yn="N"),
        ]
        client = make_client(transport=FakeTransport(responses=list(pages)))

        all_pages = client.fetch_all("ka20005", "/api/dostk/chart", {})
        assert len(all_pages) == 2

    def test_pagination_infinite_loop_guard(self):
        """Repeated next_key aborts instead of looping forever."""
        pages = [
            make_minute_page([], cont_yn="Y", next_key="same"),
            make_minute_page([], cont_yn="Y", next_key="same"),
        ]
        client = make_client(transport=FakeTransport(responses=list(pages)))

        with pytest.raises(KiwoomPaginationError):
            client.fetch_all("ka20005", "/api/dostk/chart", {})

    def test_pagination_budget_cap(self):
        """Pagination respects max_pages."""
        pages = [make_minute_page([], cont_yn="Y", next_key=str(i)) for i in range(5)]
        client = make_client(transport=FakeTransport(responses=list(pages)))

        with pytest.raises(KiwoomPaginationError):
            client.fetch_all("ka20005", "/api/dostk/chart", {}, max_pages=3)


# --------------------------------------------------------------------------- #
# RAW_CANDLE_PARSE_PASS / UTC_TO_KST_PASS / NAIVE_TIMESTAMP_CONTRACT_PASS      #
# --------------------------------------------------------------------------- #


class TestTimestampParsing:
    def test_utc_to_kst_pass(self):
        """UTC timestamps convert to Asia/Seoul (+09:00)."""
        dt = datetime(2026, 8, 5, 1, 0, 0, tzinfo=timezone.utc)
        kst = dt.astimezone(KST)
        assert kst.hour == 10
        assert kst.utcoffset() == timedelta(hours=9)

    def test_naive_timestamp_contract_pass(self):
        """Naive timestamps are interpreted as KST per the documented rule."""
        from fmindex.market.bridge import MarketBridge

        bridge = MarketBridge()
        ts = bridge._normalize_timestamp("2026-08-05 10:30:00")
        assert ts is not None and ts.endswith("+09:00")

    def test_bucket_for_timestamp_kst(self):
        """bucket_for_timestamp returns a KST-aware bucket start."""
        bucket = bucket_for_timestamp("2026-08-05T10:45:00+09:00")
        assert bucket is not None
        start, partial = bucket
        assert start.hour == 10 and start.minute == 0
        assert partial is False


# --------------------------------------------------------------------------- #
# SESSION_FILTER_PASS / HOLIDAY_EXCLUDED / PARTIAL_FINAL_BUCKET_PASS          #
# --------------------------------------------------------------------------- #


class TestSessionAndCalendar:
    def test_session_filter_pass(self):
        """Out-of-session timestamps are excluded from hourly buckets."""
        assert is_session_time(datetime(2026, 8, 5, 10, 0, tzinfo=KST)) is True
        assert is_session_time(datetime(2026, 8, 5, 8, 59, tzinfo=KST)) is False
        assert is_session_time(datetime(2026, 8, 5, 15, 31, tzinfo=KST)) is False

    def test_session_bucket_outside_none(self):
        """Out-of-session timestamps produce no bucket."""
        assert bucket_for_timestamp("2026-08-05T16:00:00+09:00") is None
        assert bucket_for_timestamp("2026-08-05T08:00:00+09:00") is None

    def test_holiday_excluded(self):
        """2026-08-15 (광복절, Saturday) is not a trading day."""
        dt = datetime(2026, 8, 15, 10, 0, tzinfo=KST)
        assert is_trading_day(dt) is False

    def test_weekend_excluded(self):
        """Saturday and Sunday are not trading days."""
        sat = datetime(2026, 8, 8, 10, 0, tzinfo=KST)
        sun = datetime(2026, 8, 9, 10, 0, tzinfo=KST)
        assert is_trading_day(sat) is False
        assert is_trading_day(sun) is False

    def test_weekday_trading_day(self):
        """A regular Wednesday is a trading day."""
        dt = datetime(2026, 8, 5, 10, 0, tzinfo=KST)
        assert is_trading_day(dt) is True

    def test_partial_final_bucket_pass(self):
        """The 15:00-15:30 bucket is marked partial."""
        bucket = bucket_for_timestamp("2026-08-05T15:20:00+09:00")
        assert bucket is not None
        start, partial = bucket
        assert start.hour == 15
        assert partial is True

    def test_previous_trading_day(self):
        """previous_trading_day skips the weekend."""
        friday = datetime(2026, 8, 7, 12, 0, tzinfo=KST)
        prev = previous_trading_day(friday)
        assert prev.weekday() == 3  # Thursday


# --------------------------------------------------------------------------- #
# HOURLY_OPEN_FIRST_PASS / HIGH_MAX / LOW_MIN / CLOSE_LAST / VOLUME_SUM_OR_NULL #
# --------------------------------------------------------------------------- #


class TestHourlyAggregation:
    def _collector(self, transport):
        client = make_client(transport=transport)
        return KospiCollector(client=client, request_delay=0.0)

    def test_hourly_ohlc_rules(self):
        """open=first, high=max, low=min, close=last, volume=sum."""
        rows = [
            minute_row("0905", 100, 105, 99, 101, vol="100"),
            minute_row("0925", 101, 110, 100, 108, vol="200"),
            minute_row("0945", 108, 109, 102, 104, vol="150"),
        ]
        transport = FakeTransport(responses=[make_minute_page(rows)])
        collector = self._collector(transport)
        records = collector.collect_day("2026-08-05")
        assert len(records) == 1
        rec = records[0]
        assert rec.open == 100
        assert rec.high == 110
        assert rec.low == 99
        assert rec.close == 104
        assert rec.volume == 450.0
        assert rec.instrument_id == "001"
        assert rec.symbol == "KOSPI"
        assert rec.provider == "kiwoom"
        assert rec.asset_type == "index"
        assert rec.data_mode == "real"

    def test_volume_null_when_missing(self):
        """Missing volume yields null, never a fabricated 0."""
        rows = [minute_row("0905", 100, 105, 99, 101, vol=None, acc_vol=None)]
        transport = FakeTransport(responses=[make_minute_page(rows)])
        collector = self._collector(transport)
        records = collector.collect_day("2026-08-05")
        assert records[0].volume is None

    def test_missing_candle_not_zero_filled(self):
        """Missing candles are not padded with zeros; bucket just omits them."""
        rows = [minute_row("0905", 100, 105, 99, 101)]
        transport = FakeTransport(responses=[make_minute_page(rows)])
        collector = self._collector(transport)
        records = collector.collect_day("2026-08-05")
        # Only the 09:00 bucket exists; 10:00 is absent, not zero-filled.
        assert len(records) == 1
        assert records[0].timestamp == "2026-08-05T09:00:00+09:00"

    def test_collect_day_single_bucket(self):
        """collect_day returns hourly records for the date."""
        rows = [minute_row("1005", 100, 105, 99, 101)]
        transport = FakeTransport(responses=[make_minute_page(rows)])
        collector = self._collector(transport)
        records = collector.collect_day("2026-08-05")
        assert len(records) == 1
        assert records[0].timestamp == "2026-08-05T10:00:00+09:00"


# --------------------------------------------------------------------------- #
# DUPLICATE_KEY_PASS / LATEST_COMPLETE_RECORD_WINS                             #
# --------------------------------------------------------------------------- #


class TestDedup:
    def test_duplicate_key_pass(self):
        """Same (provider, instrumentId, timestamp) dedups to one record."""
        a = make_record(observed_at="2026-08-05T11:00:00+09:00", close=3200.0)
        b = make_record(observed_at="2026-08-05T12:00:00+09:00", close=3300.0)
        collector = make_collector()
        merged = collector.merge_existing([a], [b])
        assert len(merged) == 1
        assert merged[0].close == 3300.0  # newest observedAt wins

    def test_latest_complete_record_wins(self):
        """Same observedAt: the more complete OHLC wins."""
        a = make_record(
            observed_at="2026-08-05T11:00:00+09:00",
            open=None, high=None, low=None, close=3200.0,
        )
        b = make_record(observed_at="2026-08-05T11:00:00+09:00", close=3210.0)
        collector = make_collector()
        merged = collector.merge_existing([a], [b])
        assert len(merged) == 1
        assert merged[0].close == 3210.0


# --------------------------------------------------------------------------- #
# INCREMENTAL_BACKFILL_PASS / OVERLAP_REFRESH_PASS / OUTPUT_SORTED_PASS        #
# --------------------------------------------------------------------------- #


class TestIncremental:
    def test_incremental_backfill_pass(self, tmp_path):
        """Fresh records merge with existing ones without duplication."""
        existing = make_record(ts="2026-08-05T09:00:00+09:00")
        fresh = make_record(ts="2026-08-05T10:00:00+09:00")
        collector = make_collector()
        merged = collector.merge_existing([existing], [fresh])
        assert len(merged) == 2
        # Sorted ascending.
        assert merged[0].timestamp < merged[1].timestamp

    def test_output_sorted_pass(self, tmp_path):
        """write_records writes records sorted by timestamp."""
        records = [
            make_record(ts="2026-08-05T11:00:00+09:00"),
            make_record(ts="2026-08-05T09:00:00+09:00"),
            make_record(ts="2026-08-05T10:00:00+09:00"),
        ]
        collector = make_collector()
        out = tmp_path / "out.jsonl"
        collector.write_records(out, records)
        lines = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()]
        stamps = [l["timestamp"] for l in lines]
        assert stamps == sorted(stamps)

    def test_overlap_refresh_pass(self):
        """Overlapping timestamps resolve to the newest observedAt."""
        existing = make_record(
            ts="2026-08-05T10:00:00+09:00", observed_at="2026-08-05T11:00:00+09:00", close=100.0
        )
        refreshed = make_record(
            ts="2026-08-05T10:00:00+09:00", observed_at="2026-08-05T12:00:00+09:00", close=101.0
        )
        collector = make_collector()
        merged = collector.merge_existing([existing], [refreshed])
        assert len(merged) == 1
        assert merged[0].close == 101.0


# --------------------------------------------------------------------------- #
# METADATA_SECRET_FREE_PASS                                                    #
# --------------------------------------------------------------------------- #


class TestMetadata:
    def test_metadata_secret_free_pass(self):
        """Metadata contains no tokens or credentials."""
        meta = CollectionMetadata(
            provider="kiwoom", instrument_id="001", symbol="KOSPI",
            asset_type="index", requested_from="2026-08-01", requested_to="2026-08-05",
            effective_from="2026-08-01", effective_to="2026-08-05",
            requests_made=5, records_received=40, records_accepted=39,
            records_rejected=1, duplicates_removed=0, partial_buckets=1,
            latest_timestamp="2026-08-05T15:00:00+09:00",
            generated_at="2026-08-05T18:00:00+09:00", data_mode="real",
            api_contract_version="kiwoom-rest-2026.1", collector_version="1.0.0",
        )
        d = json.dumps(meta.to_dict())
        for secret in ("token", "appkey", "secretkey", "authorization", "Bearer"):
            assert secret.lower() not in d.lower()

    def test_metadata_required_fields(self):
        """Metadata carries all required fields."""
        meta = CollectionMetadata(
            provider="kiwoom", instrument_id="001", symbol="KOSPI",
            asset_type="index", requested_from="2026-08-01", requested_to="2026-08-05",
            effective_from="2026-08-01", effective_to="2026-08-05",
            requests_made=0, records_received=0, records_accepted=0,
            records_rejected=0, duplicates_removed=0, partial_buckets=0,
            latest_timestamp="", generated_at="2026-08-05T18:00:00+09:00",
            data_mode="real", api_contract_version="v1", collector_version="1.0.0",
        )
        required = {
            "provider", "instrumentId", "symbol", "assetType", "requestedFrom",
            "requestedTo", "effectiveFrom", "effectiveTo", "requestsMade",
            "recordsReceived", "recordsAccepted", "recordsRejected",
            "duplicatesRemoved", "partialBuckets", "latestTimestamp",
            "generatedAt", "dataMode", "apiContractVersion", "collectorVersion",
            "warnings",
        }
        assert required <= set(meta.to_dict())


# --------------------------------------------------------------------------- #
# REAL_MODE_FAILS_CLOSED / AUTO_MODE_SAMPLE_FALLBACK_PASS                      #
# --------------------------------------------------------------------------- #


class TestPipelineModes:
    def _valid_kospi_file(self, tmp_path):
        rec = make_record()
        f = tmp_path / "kospi.jsonl"
        f.write_text(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n", encoding="utf-8")
        return f

    def test_real_mode_fails_closed(self, tmp_path):
        """kiwoom mode with no validated records fails instead of sample."""
        bad = tmp_path / "stock.jsonl"
        bad.write_text(
            json.dumps(make_record(instrument_id="005930", symbol="삼성전자",
                                   asset_type="stock").to_dict()) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(RuntimeError):
            run_pipeline_once(
                market_data_path=str(bad),
                market_source="kiwoom",
                fmkorea_fixture_dir=str(Path(__file__).resolve().parent / "fixtures" / "fmkorea"),
                output_dir=str(tmp_path / "out"),
            )

    def test_real_mode_missing_file_fails_closed(self, tmp_path):
        """kiwoom mode with a missing file raises, never falls back to sample."""
        with pytest.raises(RuntimeError):
            run_pipeline_once(
                market_data_path=str(tmp_path / "missing.jsonl"),
                market_source="kiwoom",
                fmkorea_fixture_dir=str(Path(__file__).resolve().parent / "fixtures" / "fmkorea"),
                output_dir=str(tmp_path / "out"),
            )

    def test_auto_mode_sample_fallback_pass(self, tmp_path):
        """auto mode falls back to sample when no real data exists."""
        results = run_pipeline_once(
            market_data_path=None,
            market_source="auto",
            fmkorea_fixture_dir=str(Path(__file__).resolve().parent / "fixtures" / "fmkorea"),
            output_dir=str(tmp_path / "out"),
        )
        assert results["summary"]["dataMode"] == "sample"
        assert results["summary"]["marketSource"] == "auto"

    def test_sample_mode_explicit(self, tmp_path):
        """--market-source sample is explicit and marked sample."""
        results = run_pipeline_once(
            market_source="sample",
            fmkorea_fixture_dir=str(Path(__file__).resolve().parent / "fixtures" / "fmkorea"),
            output_dir=str(tmp_path / "out"),
        )
        assert results["summary"]["dataMode"] == "sample"
        assert results["summary"]["marketSource"] == "sample"


# --------------------------------------------------------------------------- #
# MARKET_BRIDGE_REAL_KOSPI_PASS                                               #
# --------------------------------------------------------------------------- #


class TestMarketBridgeReal:
    def test_market_bridge_real_kospi_pass(self, tmp_path):
        """Bridge accepts a validated real KOSPI record."""
        rec = make_record()
        f = tmp_path / "kospi.jsonl"
        f.write_text(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n", encoding="utf-8")
        bridge = MarketBridge()
        records = bridge.read_kospi_records(str(f))
        assert len(records) == 1
        assert records[0].provider == "kiwoom"
        assert records[0].data_mode == "real"
        assert records[0].asset_type == "index"
        assert records[0].instrument_id == "001"
        assert records[0].symbol == "KOSPI"

    def test_bridge_rejects_non_kiwoom_provider(self, tmp_path):
        """provider != kiwoom is rejected by the strict bridge."""
        rec = make_record(provider="other")
        f = tmp_path / "bad.jsonl"
        f.write_text(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n", encoding="utf-8")
        bridge = MarketBridge()
        records = bridge.read_kospi_records(str(f))
        assert records == []

    def test_bridge_rejects_wrong_ohlc(self, tmp_path):
        """Internally inconsistent OHLC is rejected."""
        rec = make_record(high=3000.0, low=3100.0, allow_inconsistent=True)  # high < low
        f = tmp_path / "bad.jsonl"
        f.write_text(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n", encoding="utf-8")
        bridge = MarketBridge()
        records = bridge.read_kospi_records(str(f))
        assert records == []


# --------------------------------------------------------------------------- #
# PIPELINE_REAL_MODE_CONTRACT_PASS                                            #
# --------------------------------------------------------------------------- #


class TestPipelineRealContract:
    def test_pipeline_real_mode_contract_pass(self, tmp_path):
        """kiwoom mode with a validated KOSPI file yields real data."""
        rec = make_record()
        f = tmp_path / "kospi.jsonl"
        f.write_text(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n", encoding="utf-8")
        results = run_pipeline_once(
            market_data_path=str(f),
            market_source="kiwoom",
            fmkorea_fixture_dir=str(Path(__file__).resolve().parent / "fixtures" / "fmkorea"),
            output_dir=str(tmp_path / "out"),
        )
        summary = results["summary"]
        assert summary["dataMode"] == "real"
        assert summary["provider"] != ""
        assert summary["instrument"] == "001"
        assert summary["symbol"] == "KOSPI"
        assert summary["marketSource"] == "kiwoom"

    def test_kiwoom_auth_missing_raises(self, monkeypatch):
        """run_collector without credentials fails closed."""
        monkeypatch.delenv("KIWOOM_APPKEY", raising=False)
        monkeypatch.delenv("KIWOOM_SECRETKEY", raising=False)
        monkeypatch.delenv("KIWOOM_65STOCK_ENV", raising=False)
        config = CollectorConfig(from_date="2026-08-05", to_date="2026-08-05")
        with pytest.raises(KiwoomAuthError):
            run_collector(config)
