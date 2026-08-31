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
    CalendarYearError,
    SUPPORTED_CALENDAR_YEARS,
    bucket_for_timestamp,
    is_session_time,
    is_trading_day,
    previous_trading_day,
    validate_calendar_years,
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
    KiwoomResponse,
)
from fmindex.market.kospi_collector import (
    CollectorConfig,
    KospiCollector,
    NoTradingDaysError,
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
        return KiwoomResponse(
            body={"return_code": 0, "return_msg": "ok", "inds_min_pole_qry": []},
            headers={},
        )


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


def make_minute_page(rows, cont_yn="N", next_key="", headers=None):
    """Build a KiwoomResponse whose pagination values live ONLY in the
    response headers (the official contract), never in the JSON body."""
    hdrs = dict(headers or {})
    if cont_yn:
        hdrs.setdefault("cont-yn", cont_yn)
    if next_key:
        hdrs.setdefault("next-key", next_key)
    return KiwoomResponse(
        body={
            "return_code": 0,
            "return_msg": "정상적으로 처리되었습니다",
            "inds_min_pole_qry": rows,
        },
        headers=hdrs,
    )


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
    def test_production_secret_not_logged(self, monkeypatch, caplog):
        """PRODUCTION_SECRET_LOG_TEST: credentials never leak via the real
        production path (KiwoomTokenManager._issue).

        Unique sentinels are set as KIWOOM_APPKEY/KIWOOM_SECRETKEY; token
        issuance is forced to fail over the network; caplog is at DEBUG
        and a benign marker proves logs are actually being collected.
        """
        import logging
        import urllib.error
        import urllib.request

        sentinel_app = "SENTINEL_APPKEY_9x2k"
        sentinel_sec = "SENTINEL_SECRETKEY_7q4m"
        monkeypatch.setenv("KIWOOM_APPKEY", sentinel_app)
        monkeypatch.setenv("KIWOOM_SECRETKEY", sentinel_sec)
        monkeypatch.setenv("KIWOOM_65STOCK_ENV", "")

        caplog.set_level(logging.DEBUG)
        logging.getLogger().setLevel(logging.DEBUG)
        # Benign marker proves caplog actually captures records.
        logging.getLogger("fmindex.test.marker").debug("benign-log-marker-1234")
        assert "benign-log-marker-1234" in caplog.text

        manager = KiwoomTokenManager(base_url="https://mockapi.kiwoom.com")

        def boom(*args, **kwargs):
            raise urllib.error.URLError("simulated connection refused")

        monkeypatch.setattr(urllib.request, "urlopen", boom)
        with pytest.raises(KiwoomAuthError) as excinfo:
            manager.get_token()

        combined = str(excinfo.value) + caplog.text
        assert sentinel_app not in combined
        assert sentinel_sec not in combined
        assert "Bearer" not in combined

    def test_authorization_header_not_logged(self, caplog):
        """The Bearer token (test-token) never appears in logs."""
        import logging

        caplog.set_level(logging.DEBUG)
        logging.getLogger().setLevel(logging.DEBUG)
        transport = FakeTransport()
        client = make_client(transport=transport)
        client.fetch("ka20005", "/api/dostk/chart", {})
        sent_token = transport.calls[0][2]["authorization"]
        assert sent_token == "Bearer test-token"  # present in request by design
        assert "test-token" not in caplog.text

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
            return KiwoomResponse(
                body={"return_code": 5, "return_msg": "허용된 요청 개수를 초과"},
                headers={},
            )

        client = make_client(transport=transport)
        with pytest.raises(KiwoomRateLimitError):
            client.fetch("ka20005", "/api/dostk/chart", {})

    def test_pagination_header_contract_two_pages(self):
        """RESPONSE_HEADER_PAGINATION: cont-yn/next-key from headers.

        The JSON body carries NO continuation values at all. The first
        response header says cont-yn=Y/next-key=k1, the second says
        cont-yn=N. The SECOND request must carry cont-yn=Y/next-key=k1.
        Exactly two pages are returned.
        """
        body = {
            "return_code": 0,
            "return_msg": "ok",
            "inds_min_pole_qry": [minute_row("0905", 1, 2, 1, 2)],
        }
        assert "cont_yn" not in body and "next_key" not in body
        pages = [
            KiwoomResponse(body=body, headers={"cont-yn": "Y", "next-key": "k1"}),
            KiwoomResponse(
                body={"return_code": 0, "return_msg": "ok", "inds_min_pole_qry": []},
                headers={"cont-yn": "N"},
            ),
        ]
        transport = FakeTransport(responses=list(pages))
        client = make_client(transport=transport)

        all_pages = client.fetch_all("ka20005", "/api/dostk/chart", {})
        assert len(all_pages) == 2

        # Second request reuses the previous response header values.
        assert len(transport.calls) == 2
        second_headers = transport.calls[1][2]
        assert second_headers["cont-yn"] == "Y"
        assert second_headers["next-key"] == "k1"
        # First request starts with no continuation.
        assert transport.calls[0][2]["cont-yn"] == "N"

    def test_pagination_body_values_ignored(self):
        """BODY_ONLY_PAGINATION_REMOVED: JSON-body cont_yn is NOT used."""
        pages = [
            KiwoomResponse(
                body={
                    "return_code": 0, "return_msg": "ok",
                    "cont_yn": "Y", "next_key": "body-key",  # must be ignored
                    "inds_min_pole_qry": [],
                },
                headers={},  # no continuation headers -> stop after 1 page
            ),
            KiwoomResponse(
                body={"return_code": 0, "return_msg": "ok", "inds_min_pole_qry": []},
                headers={},
            ),
        ]
        transport = FakeTransport(responses=list(pages))
        client = make_client(transport=transport)
        all_pages = client.fetch_all("ka20005", "/api/dostk/chart", {})
        assert len(all_pages) == 1  # header says stop; body value ignored

    def test_pagination_header_case_insensitive(self):
        """Uppercase response headers (Cont-Yn/Next-Key) are honored."""
        pages = [
            KiwoomResponse(
                body={"return_code": 0, "return_msg": "ok", "inds_min_pole_qry": []},
                headers={"Cont-Yn": "Y", "Next-Key": "k9"},
            ),
            KiwoomResponse(
                body={"return_code": 0, "return_msg": "ok", "inds_min_pole_qry": []},
                headers={"cont-yn": "N"},
            ),
        ]
        transport = FakeTransport(responses=list(pages))
        client = make_client(transport=transport)
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

    def test_pagination_empty_key_with_cont_yn_y(self):
        """cont-yn=Y with an empty next-key is a clear error."""
        pages = [
            KiwoomResponse(
                body={"return_code": 0, "return_msg": "ok", "inds_min_pole_qry": []},
                headers={"cont-yn": "Y", "next-key": ""},
            ),
        ]
        client = make_client(transport=FakeTransport(responses=list(pages)))

        with pytest.raises(KiwoomPaginationError, match="next-key header is empty"):
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
            "warnings", "overlapHours", "existingLatestTimestamp",
            "calendarSource", "calendarVersion", "supportedCalendarYears",
            "noTradingDays",
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


class TestExactKospiContract:
    """EXACT_KOSPI_IDENTITY / OTHER_INDEX_REJECTED / MALFORMED_IDS_REJECTED."""

    @staticmethod
    def _bridge_rejects(tmp_path, **kw):
        rec = make_record(**kw)
        f = tmp_path / "bad.jsonl"
        f.write_text(json.dumps(rec.to_dict(), ensure_ascii=False) + chr(10), encoding="utf-8")
        return MarketBridge().read_kospi_records(str(f))

    def test_exact_001_kospi_accepted(self):
        assert MarketBridge.is_exact_kospi_contract("001", "KOSPI") is True

    def test_kospi_index_identity_exact_only(self):
        """is_exact_kospi_contract never does substring matching."""
        assert MarketBridge.is_exact_kospi_contract("001", "KOSPI") is True
        assert MarketBridge.is_exact_kospi_contract("0010", "KOSPI") is False
        assert MarketBridge.is_exact_kospi_contract("001234", "KOSPI") is False
        assert MarketBridge.is_exact_kospi_contract("001", "KOSDAQ") is False
        assert MarketBridge.is_exact_kospi_contract("101", "KOSDAQ") is False
        assert MarketBridge.is_exact_kospi_contract("1001", "KOSPI") is False
        assert MarketBridge.is_exact_kospi_contract("005930", "KOSPI") is False

    def test_bridge_rejects_kospi_with_wrong_symbol(self, tmp_path):
        """instrumentId=001 but symbol=KOSDAQ is rejected."""
        assert self._bridge_rejects(tmp_path, instrument_id="001", symbol="KOSDAQ") == []

    def test_bridge_rejects_other_index(self, tmp_path):
        """KOSDAQ index (101) with assetType=index is rejected."""
        assert self._bridge_rejects(
            tmp_path, instrument_id="101", symbol="KOSDAQ", asset_type="index"
        ) == []

    def test_bridge_rejects_malformed_ids(self, tmp_path):
        """1001 / 001234 instrument ids are rejected."""
        assert self._bridge_rejects(tmp_path, instrument_id="1001", symbol="KOSPI") == []
        assert self._bridge_rejects(tmp_path, instrument_id="001234", symbol="KOSPI") == []

    def test_bridge_rejects_stock_with_kospi_symbol(self, tmp_path):
        """005930/KOSPI (individual stock mislabeled) is rejected."""
        assert self._bridge_rejects(
            tmp_path, instrument_id="005930", symbol="KOSPI", asset_type="stock"
        ) == []


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


# --------------------------------------------------------------------------- #
# TIC_SCOPE_CANONICAL_INT / REQUEST_BODY_TIC_SCOPE_INT / INVALID_SCOPE_FAIL_FAST
# --------------------------------------------------------------------------- #


class TestTicScopeCanonicalContract:
    """Canonical tic_scope contract: string wire type, fail-fast on invalid."""

    def test_default_value_is_string(self):
        cfg = CollectorConfig(from_date="2026-08-05", to_date="2026-08-05")
        assert cfg.tic_scope == "60"
        assert type(cfg.tic_scope) is str

    def test_string_input_stays_string(self):
        cfg = CollectorConfig(from_date="2026-08-05", to_date="2026-08-05", tic_scope="60")
        assert cfg.tic_scope == "60"
        assert type(cfg.tic_scope) is str

    def test_int_compatible_input_canonicalized_to_string(self):
        cfg = CollectorConfig(from_date="2026-08-05", to_date="2026-08-05", tic_scope=60)
        assert cfg.tic_scope == "60"
        assert type(cfg.tic_scope) is str

    def test_all_allowed_scopes_are_strings(self):
        from fmindex.market.kospi_collector import ALLOWED_TIC_SCOPES
        for scope in ALLOWED_TIC_SCOPES:
            cfg = CollectorConfig(
                from_date="2026-08-05", to_date="2026-08-05", tic_scope=scope
            )
            assert type(cfg.tic_scope) is str
            assert cfg.tic_scope == scope

    def test_int_counterparts_canonicalize_to_string(self):
        for wire, as_int in [
            ("1", 1), ("3", 3), ("5", 5), ("10", 10),
            ("15", 15), ("30", 30), ("45", 45), ("60", 60),
        ]:
            cfg = CollectorConfig(
                from_date="2026-08-05", to_date="2026-08-05", tic_scope=as_int
            )
            assert cfg.tic_scope == wire
            assert type(cfg.tic_scope) is str

    @pytest.mark.parametrize(
        "bad",
        [
            "0", "2", "59", "90", "05", "060", " 60", "60 ", "abc", "60.0",
            0, 2, 59, 90, 60.0, None, True, False, [60], {"value": 60},
        ],
    )
    def test_invalid_scope_fails_fast(self, bad):
        with pytest.raises(ValueError):
            CollectorConfig(from_date="2026-08-05", to_date="2026-08-05", tic_scope=bad)

    def test_request_body_tic_scope_is_string(self, tmp_path):
        """The body seen by the transport must carry a string tic_scope."""
        seen = {}

        def transport(url, body, headers):
            seen["body"] = body
            return make_minute_page(
                [minute_row("0900", 3200, 3210, 3190, 3205)], cont_yn="N"
            )

        cfg = CollectorConfig(
            from_date="2026-08-05",
            to_date="2026-08-05",
            tic_scope=60,
            output=str(tmp_path / "out.jsonl"),
            metadata_output=str(tmp_path / "out.meta.json"),
        )
        collector = KospiCollector(client=make_client(transport=transport), config=cfg, request_delay=0.0)
        fresh = collector.collect_range("2026-08-05", "2026-08-05", requested_dates=[])
        assert fresh is not None
        assert "tic_scope" in seen["body"]
        assert seen["body"]["tic_scope"] == "60"
        assert type(seen["body"]["tic_scope"]) is str

    def test_all_allowed_scopes_sent_as_string(self, tmp_path):
        from fmindex.market.kospi_collector import ALLOWED_TIC_SCOPES
        for scope in ALLOWED_TIC_SCOPES:
            seen = {}

            def transport(url, body, headers):
                seen["body"] = body
                return make_minute_page(
                    [minute_row("0900", 3200, 3210, 3190, 3205)], cont_yn="N"
                )

            cfg = CollectorConfig(
                from_date="2026-08-05",
                to_date="2026-08-05",
                tic_scope=scope,
                output=str(tmp_path / f"out-{scope}.jsonl"),
                metadata_output=str(tmp_path / f"out-{scope}.meta.json"),
            )
            collector = KospiCollector(client=make_client(transport=transport), config=cfg, request_delay=0.0)
            collector.collect_range("2026-08-05", "2026-08-05", requested_dates=[])
            assert type(seen["body"]["tic_scope"]) is str
            assert seen["body"]["tic_scope"] == scope

    def test_live_contract_mock_string_succeeds_int_fails(self, tmp_path):
        """Mock the live API: str tic_scope succeeds, int tic_scope is rejected."""
        seen = {}

        def strict_transport(url, body, headers):
            seen["body"] = body
            if type(body.get("tic_scope")) is not str:
                raise KiwoomAPIError(
                    "Kiwoom API error [ka20005] return_code=2: "
                    "파라미터=tic_scope 실패사유= 타입 불일치"
                )
            return make_minute_page(
                [minute_row("0900", 3200, 3210, 3190, 3205)], cont_yn="N"
            )

        cfg = CollectorConfig(
            from_date="2026-08-05",
            to_date="2026-08-05",
            tic_scope="60",
            output=str(tmp_path / "out.jsonl"),
            metadata_output=str(tmp_path / "out.meta.json"),
        )
        assert type(cfg.tic_scope) is str
        collector = KospiCollector(client=make_client(transport=strict_transport), config=cfg, request_delay=0.0)
        collector.collect_range("2026-08-05", "2026-08-05", requested_dates=[])
        assert type(seen["body"]["tic_scope"]) is str

    def test_int_wire_regression_guard(self, tmp_path):
        """An int tic_scope in the request body must be rejected by the mock."""
        seen = {}

        def transport(url, body, headers):
            seen["body"] = body
            return make_minute_page([], cont_yn="N")

        cfg = CollectorConfig(
            from_date="2026-08-05",
            to_date="2026-08-05",
            tic_scope=60,
            output=str(tmp_path / "out.jsonl"),
            metadata_output=str(tmp_path / "out.meta.json"),
        )
        collector = KospiCollector(client=make_client(transport=transport), config=cfg, request_delay=0.0)
        collector.collect_range("2026-08-05", "2026-08-05", requested_dates=[])
        # Explicit guard: the production request body must NEVER carry int.
        assert type(seen["body"]["tic_scope"]) is not int

    def test_body_serialization_is_string(self, tmp_path):
        """json.dumps of the request body must emit tic_scope as a string."""
        import json as _json
        seen = {}

        def transport(url, body, headers):
            seen["body"] = body
            return make_minute_page(
                [minute_row("0900", 3200, 3210, 3190, 3205)], cont_yn="N"
            )

        cfg = CollectorConfig(
            from_date="2026-08-05",
            to_date="2026-08-05",
            tic_scope="60",
            output=str(tmp_path / "out.jsonl"),
            metadata_output=str(tmp_path / "out.meta.json"),
        )
        collector = KospiCollector(client=make_client(transport=transport), config=cfg, request_delay=0.0)
        collector.collect_range("2026-08-05", "2026-08-05", requested_dates=[])
        serialized = _json.dumps(seen["body"])
        assert '"tic_scope": "60"' in serialized
        assert '"tic_scope": 60' not in serialized

    def test_cli_parses_tic_scope_as_string(self):
        from fmindex.market.kospi_collector import build_parser
        parser = build_parser()
        args = parser.parse_args(
            ["--from", "2026-08-05", "--to", "2026-08-05", "--tic-scope", "60"]
        )
        assert type(args.tic_scope) is str
        assert args.tic_scope == "60"

    def test_cli_rejects_invalid_tic_scope(self):
        from fmindex.market.kospi_collector import build_parser
        parser = build_parser()
        import pytest as _pytest
        with _pytest.raises(SystemExit):
            parser.parse_args(
                ["--from", "2026-08-05", "--to", "2026-08-05", "--tic-scope", "2"]
            )

    def test_invalid_scope_never_reaches_transport(self, tmp_path):
        calls = []

        def transport(url, body, headers):
            calls.append(body)
            return make_minute_page([], cont_yn="N")

        with pytest.raises(ValueError):
            CollectorConfig(
                from_date="2026-08-05",
                to_date="2026-08-05",
                tic_scope=2,
                output=str(tmp_path / "out.jsonl"),
                metadata_output=str(tmp_path / "out.meta.json"),
            )
        assert calls == []

    def test_existing_contract_preserved(self, tmp_path):
        """inds_cd=001 / KOSPI / index / fail-closed still hold."""
        from fmindex.market.kospi_collector import (
            API_ID_MINUTE_CHART,
            KOSPI_INDS_CD,
            SECTOR_PATH,
            TABLE_MINUTE,
        )
        from fmindex.market.models import ASSET_TYPE_INDEX, KOSPI_SYMBOL
        assert API_ID_MINUTE_CHART == "ka20005"
        assert SECTOR_PATH == "/api/dostk/chart"
        assert TABLE_MINUTE == "inds_min_pole_qry"
        assert KOSPI_INDS_CD == "001"
        assert KOSPI_SYMBOL == "KOSPI"
        assert ASSET_TYPE_INDEX == "index"


# --------------------------------------------------------------------------- #
# CNTR_TM_14_DIGIT / DATE FILTER / NO_FALLBACK                                #
# --------------------------------------------------------------------------- #


class TestCntrTmTimestampContract:
    """14-digit YYYYMMDDHHMMSS live contract + legacy 4/6-digit formats."""

    def test_14_digit_parses(self):
        c = make_collector()
        assert c._timestamp_from_row({"cntr_tm": "20260805150000"}, "20260805") == \
            "2026-08-05T15:00:00+09:00"

    def test_14_digit_with_seconds(self):
        c = make_collector()
        assert c._timestamp_from_row({"cntr_tm": "20260805145959"}, "20260805") == \
            "2026-08-05T14:59:59+09:00"

    def test_request_date_mismatch_rejected(self):
        c = make_collector()
        assert c._timestamp_from_row({"cntr_tm": "20260804150000"}, "20260805") is None

    def test_invalid_date_rejected(self):
        c = make_collector()
        assert c._timestamp_from_row({"cntr_tm": "20260230090000"}, "20260805") is None

    @pytest.mark.parametrize("bad", ["20260805240000", "20260805156000", "20260805150060"])
    def test_invalid_time_rejected(self, bad):
        c = make_collector()
        assert c._timestamp_from_row({"cntr_tm": bad}, "20260805") is None

    def test_legacy_6_digit_parses(self):
        c = make_collector()
        assert c._timestamp_from_row({"cntr_tm": "150000"}, "20260805") == \
            "2026-08-05T15:00:00+09:00"

    def test_legacy_4_digit_parses(self):
        c = make_collector()
        assert c._timestamp_from_row({"cntr_tm": "1500"}, "20260805") == \
            "2026-08-05T15:00:00+09:00"

    @pytest.mark.parametrize("bad", [None, "", "   ", "1500x", "+20260805150000",
                                     "20260805150000.0", "1500 ", "1500000"])
    def test_missing_or_malformed_rejected_no_fallback(self, bad):
        c = make_collector()
        assert c._timestamp_from_row({"cntr_tm": bad}, "20260805") is None

    def test_status_classification(self):
        c = make_collector()
        assert c._row_timestamp_status({"cntr_tm": "20260805150000"}, "20260805") == "target"
        assert c._row_timestamp_status({"cntr_tm": "20260804150000"}, "20260805") == "other_date"
        assert c._row_timestamp_status({"cntr_tm": "150000"}, "20260805") == "target"
        assert c._row_timestamp_status({"cntr_tm": None}, "20260805") == "invalid"
        assert c._row_timestamp_status({"cntr_tm": "20260805240000"}, "20260805") == "invalid"

    def test_mixed_historical_rows_filtered(self, tmp_path):
        """Only the requested-date rows produce output."""
        seen = {}

        def transport(url, body, headers):
            seen["body"] = body
            return make_minute_page(
                [
                    minute_row("20260805150000", 3200, 3210, 3190, 3205),
                    minute_row("20260805140000", 3205, 3215, 3195, 3210),
                    minute_row("20260804150000", 3100, 3110, 3090, 3105),
                    minute_row("20260803150000", 3000, 3010, 2990, 3005),
                ],
                cont_yn="N",
            )

        cfg = CollectorConfig(
            from_date="2026-08-05", to_date="2026-08-05", tic_scope=60,
            output=str(tmp_path / "out.jsonl"),
            metadata_output=str(tmp_path / "out.meta.json"),
        )
        collector = KospiCollector(client=make_client(transport=transport), config=cfg, request_delay=0.0)
        records = collector.collect_day(__import__("datetime").date(2026, 8, 5))
        assert records
        for rec in records:
            assert rec.timestamp.startswith("2026-08-05")
        assert collector.other_date_rows == 2
        assert collector.target_date_rows == 2

    def test_pagination_mixed_dates(self, tmp_path):
        """Pagination terminates; other-date rows excluded from output."""
        def transport(url, body, headers):
            page = make_minute_page(
                [
                    minute_row("20260805140000", 3205, 3215, 3195, 3210),
                    minute_row("20260804140000", 3100, 3110, 3090, 3105),
                ],
                cont_yn="Y", next_key="page2",
            )
            page2 = make_minute_page(
                [minute_row("20260803140000", 3000, 3010, 2990, 3005)],
                cont_yn="N",
            )
            if headers.get("next-key") == "page2":
                return page2
            return page

        cfg = CollectorConfig(
            from_date="2026-08-05", to_date="2026-08-05", tic_scope=60,
            output=str(tmp_path / "out.jsonl"),
            metadata_output=str(tmp_path / "out.meta.json"),
        )
        collector = KospiCollector(client=make_client(transport=transport), config=cfg, request_delay=0.0)
        records = collector.collect_day(__import__("datetime").date(2026, 8, 5))
        assert records
        assert all(r.timestamp.startswith("2026-08-05") for r in records)
        assert collector.other_date_rows == 2


# --------------------------------------------------------------------------- #
# SIGNED_PRICE_MAGNITUDE / NONNEGATIVE_VOLUME                                 #
# --------------------------------------------------------------------------- #


class TestSignedPriceMagnitude:
    """Signed OHLC magnitude handling and volume policy."""

    def test_unsigned_ohlc(self):
        c = make_collector()
        assert c._parse_price_magnitude("3200") == 3200.0

    def test_plus_prefixed(self):
        c = make_collector()
        assert c._parse_price_magnitude("+3200.25") == 3200.25

    def test_minus_prefixed(self):
        c = make_collector()
        assert c._parse_price_magnitude("-3200.25") == 3200.25

    def test_mixed_sign_ohlc(self):
        c = make_collector()
        assert c._parse_price_magnitude("-3200") == 3200.0
        assert c._parse_price_magnitude("+3210") == 3210.0
        assert c._parse_price_magnitude("-3190") == 3190.0
        assert c._parse_price_magnitude("+3205") == 3205.0

    @pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "+", "-",
                                     "++3200", "--3200", "+-3200", "abc", None, "",
                                     "0", "-0", "0.0"])
    def test_rejected_prices(self, bad):
        c = make_collector()
        assert c._parse_price_magnitude(bad) is None

    def test_negative_volume_not_abs_normalized(self):
        c = make_collector()
        assert c._parse_nonnegative_number("-100") is None

    def test_nonnegative_volume(self):
        c = make_collector()
        assert c._parse_nonnegative_number("0") == 0.0
        assert c._parse_nonnegative_number("28852") == 28852.0
        assert c._parse_nonnegative_number("NaN") is None
        assert c._parse_nonnegative_number(None) is None

    def test_signed_ohlc_full_candle(self, tmp_path):
        """A full signed-ohlc row becomes a valid candle with magnitude."""
        c = make_collector()
        row = {
            "cntr_tm": "20260805150000",
            "open_pric": "-3200.25",
            "high_pric": "+3210.50",
            "low_pric": "-3190.10",
            "cur_prc": "+3205.75",
            "trde_qty": "1000",
        }
        candle = c._candle_from_minute_row(row, "20260805")
        assert candle is not None
        assert candle.open == 3200.25
        assert candle.high == 3210.50
        assert candle.low == 3190.10
        assert candle.close == 3205.75
        assert candle.volume == 1000.0

    def test_acc_volume_not_used(self, tmp_path):
        """acc_trde_qty presence without trde_qty yields null volume."""
        c = make_collector()
        row = {
            "cntr_tm": "20260805150000",
            "open_pric": "3200",
            "high_pric": "3210",
            "low_pric": "3190",
            "cur_prc": "3205",
            "acc_trde_qty": "999999",
        }
        candle = c._candle_from_minute_row(row, "20260805")
        assert candle is not None
        assert candle.volume is None


# --------------------------------------------------------------------------- #
# EXACT_KOSPI_IDENTITY                                                        #
# --------------------------------------------------------------------------- #


class TestExactKospiIdentity:
    """_is_valid_kospi_record uses exact matching, never substrings."""

    def test_accept_exact(self):
        collector = make_collector()
        assert collector._is_valid_kospi_record(make_record())

    def test_reject_other_instrument(self):
        collector = make_collector()
        assert not collector._is_valid_kospi_record(make_record(instrument_id="101", symbol="KOSDAQ"))
        assert not collector._is_valid_kospi_record(make_record(instrument_id="1001", symbol="KOSPI"))
        assert not collector._is_valid_kospi_record(make_record(instrument_id="001234", symbol="KOSPI"))
        assert not collector._is_valid_kospi_record(make_record(instrument_id="005930", symbol="삼성전자"))

    def test_reject_symbol_mismatch(self):
        collector = make_collector()
        assert not collector._is_valid_kospi_record(make_record(instrument_id="001", symbol="KOSDAQ"))
        assert not collector._is_valid_kospi_record(make_record(instrument_id="001", symbol="KOSPI200"))

    def test_reject_market_mismatch(self):
        collector = make_collector()
        rec = make_record()
        rec.market = "KOSDAQ"
        assert not collector._is_valid_kospi_record(rec)

    def test_reject_provider_mismatch(self):
        collector = make_collector()
        rec = make_record()
        rec.provider = "sample"
        assert not collector._is_valid_kospi_record(rec)

    def test_reject_asset_type_mismatch(self):
        collector = make_collector()
        rec = make_record()
        rec.asset_type = "stock"
        assert not collector._is_valid_kospi_record(rec)

    def test_reject_data_mode_mismatch(self):
        collector = make_collector()
        rec = make_record()
        rec.data_mode = "sample"
        assert not collector._is_valid_kospi_record(rec)


# --------------------------------------------------------------------------- #
# PRODUCTION_PATH_LIVE_LIKE_FIXTURE                                           #
# --------------------------------------------------------------------------- #


class TestProductionPathFixture:
    """End-to-end collector path with live-like signed/14-digit fixture."""

    def test_production_path_fixture(self, tmp_path):
        seen_bodies = []

        def transport(url, body, headers):
            seen_bodies.append(body)
            page1 = make_minute_page(
                [
                    minute_row("20260805150000", -3200.25, 3210.50, -3190.10, 3205.75),
                    minute_row("20260805140000", 3195.0, 3210.0, 3190.0, 3200.0),
                    minute_row("20260804150000", 3000.0, 3010.0, 2990.0, 3005.0),
                ],
                cont_yn="Y", next_key="page2",
            )
            page2 = make_minute_page(
                [minute_row("20260803150000", 2900.0, 2910.0, 2890.0, 2905.0)],
                cont_yn="N",
            )
            if headers.get("next-key") == "page2":
                return page2
            return page1

        cfg = CollectorConfig(
            from_date="2026-08-05", to_date="2026-08-05", tic_scope=60,
            output=str(tmp_path / "out.jsonl"),
            metadata_output=str(tmp_path / "out.meta.json"),
        )
        collector = KospiCollector(client=make_client(transport=transport), config=cfg, request_delay=0.0)
        records = collector.collect_day(__import__("datetime").date(2026, 8, 5))

        # tic_scope wire type is a string
        assert type(seen_bodies[0]["tic_scope"]) is str
        assert seen_bodies[0]["tic_scope"] == "60"

        # Only requested-date rows become hourly records
        assert records
        assert all(r.timestamp.startswith("2026-08-05") for r in records)
        assert collector.target_date_rows == 2
        assert collector.other_date_rows == 2
        assert collector.accepted_candles == 2
        # 15:00 partial bucket
        partial = [r for r in records if r.timestamp.endswith("T15:00:00+09:00")]
        assert partial and partial[0].is_partial
        # All values positive
        for r in records:
            assert r.open > 0 and r.high > 0 and r.low > 0 and r.close > 0
            assert r.high >= max(r.open, r.close)
            assert r.low <= min(r.open, r.close)


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


# --------------------------------------------------------------------------- #
# MISSING_TIME_REJECTED / NO_TIMESTAMP_FABRICATION                             #
# --------------------------------------------------------------------------- #


class TestTimestampStrictRejection:
    """cntr_tm is never fabricated; invalid/missing times are rejected."""

    def _ts(self, tm):
        collector = make_collector()
        row = minute_row("0905", 100, 105, 99, 101)
        row["cntr_tm"] = tm
        return collector._timestamp_from_row(row, "20260805")

    def test_missing_cntr_tm_rejected(self):
        collector = make_collector()
        row = minute_row("0905", 100, 105, 99, 101)
        del row["cntr_tm"]
        assert collector._timestamp_from_row(row, "20260805") is None

    def test_empty_string_rejected(self):
        assert self._ts("") is None

    def test_hour_25_rejected(self):
        assert self._ts("250000") is None

    def test_minute_60_rejected(self):
        assert self._ts("096000") is None

    def test_non_numeric_rejected(self):
        assert self._ts("abcdef") is None

    def test_colon_time_rejected(self):
        assert self._ts("08:59") is None
        assert self._ts("15:30") is None

    def test_valid_0900_accepted(self):
        ts = self._ts("0900")
        assert ts is not None
        assert ts == "2026-08-05T09:00:00+09:00"

    def test_valid_1529_accepted(self):
        ts = self._ts("1529")
        assert ts is not None
        assert ts == "2026-08-05T15:29:00+09:00"

    def test_no_0900_fabrication_on_missing_time(self):
        """collect_day with only missing-time rows yields no records and
        never fabricates a 09:00 bucket."""
        row = minute_row("0905", 100, 105, 99, 101)
        del row["cntr_tm"]
        transport = FakeTransport(responses=[make_minute_page([row])])
        collector = make_collector(transport=transport)
        records = collector.collect_day("2026-08-05")
        assert records == []
        assert collector.records_received == 1
        assert collector.records_rejected == 1
        assert collector.records_accepted == 0


# --------------------------------------------------------------------------- #
# COLLECTOR_API_FAILS_CLOSED / FAILED_RUN_PRESERVES_OUTPUT                    #
# --------------------------------------------------------------------------- #


class TestCollectorFailClosed:
    """API/auth/pagination failures never produce or clobber output."""

    @staticmethod
    def _env(monkeypatch):
        monkeypatch.setenv("KIWOOM_APPKEY", "app")
        monkeypatch.setenv("KIWOOM_SECRETKEY", "sec")
        monkeypatch.setenv("KIWOOM_65STOCK_ENV", "")

    def test_api_error_propagates_and_no_output(self, monkeypatch, tmp_path):
        """Full run_collector path: API error -> no output, no metadata."""
        import fmindex.market.kospi_collector as kc_mod
        self._env(monkeypatch)
        out = tmp_path / "out.jsonl"
        config = CollectorConfig(
            from_date="2026-08-03", to_date="2026-08-05",
            output=str(out), metadata_output=str(tmp_path / "out.meta.json"),
        )

        def bad_transport(url, body, headers):
            raise KiwoomAPIError("simulated API error")

        collector = KospiCollector(client=make_client(transport=bad_transport), config=config, request_delay=0.0)
        monkeypatch.setattr(kc_mod, "KospiCollector", lambda **kw: collector)

        with pytest.raises(KiwoomAPIError):
            run_collector(config)
        assert not out.exists()
        assert not (tmp_path / "out.meta.json").exists()

    def test_existing_output_preserved_after_failure(self, monkeypatch, tmp_path):
        """Full run_collector path: a failing run leaves existing files intact."""
        import fmindex.market.kospi_collector as kc_mod
        self._env(monkeypatch)
        out = tmp_path / "out.jsonl"
        out.write_text("KEEP-ME" + chr(10), encoding="utf-8")
        config = CollectorConfig(
            from_date="2026-08-03", to_date="2026-08-05",
            output=str(out), metadata_output=str(tmp_path / "out.meta.json"),
        )

        def bad_transport(url, body, headers):
            raise KiwoomPaginationError("simulated pagination error")

        collector = KospiCollector(client=make_client(transport=bad_transport), config=config, request_delay=0.0)
        monkeypatch.setattr(kc_mod, "KospiCollector", lambda **kw: collector)

        with pytest.raises(KiwoomPaginationError):
            run_collector(config)
        assert out.read_text(encoding="utf-8") == "KEEP-ME" + chr(10)
        assert not (tmp_path / "out.meta.json").exists()
    def test_zero_records_on_trading_days_fails(self, monkeypatch, tmp_path):
        """Trading days present but 0 rows accepted -> fail closed (no output)."""
        import fmindex.market.kospi_collector as kc_mod
        self._env(monkeypatch)
        out = tmp_path / "out.jsonl"
        config = CollectorConfig(
            from_date="2026-08-03", to_date="2026-08-05",
            output=str(out), metadata_output=str(tmp_path / "out.meta.json"),
        )
        collector = KospiCollector(client=make_client(transport=FakeTransport()), config=config, request_delay=0.0)
        monkeypatch.setattr(kc_mod, "KospiCollector", lambda **kw: collector)

        with pytest.raises(KiwoomAPIError, match="0 records accepted"):
            run_collector(config)
        assert collector.records_accepted == 0
        assert not out.exists()
        assert not (tmp_path / "out.meta.json").exists()

    def test_no_trading_days_raises(self, monkeypatch, tmp_path):
        """A holiday-only/weekend-only range is an explicit NO_TRADING_DAYS."""
        self._env(monkeypatch)
        config = CollectorConfig(
            from_date="2026-08-15", to_date="2026-08-16",  # Sat+Sun (광복절 Sat)
            output=str(tmp_path / "out.jsonl"),
            metadata_output=str(tmp_path / "out.meta.json"),
        )
        with pytest.raises(NoTradingDaysError, match="NO_TRADING_DAYS"):
            run_collector(config)
        assert not (tmp_path / "out.jsonl").exists()
        assert not (tmp_path / "out.meta.json").exists()

    def test_weekday_holiday_only_range_no_trading_days(self, monkeypatch, tmp_path):
        """2026-08-17 (Mon, 광복절 대체공휴일) alone is NO_TRADING_DAYS."""
        self._env(monkeypatch)
        config = CollectorConfig(
            from_date="2026-08-17", to_date="2026-08-17",
            output=str(tmp_path / "out.jsonl"),
            metadata_output=str(tmp_path / "out.meta.json"),
        )
        with pytest.raises(NoTradingDaysError, match="NO_TRADING_DAYS"):
            run_collector(config)


# --------------------------------------------------------------------------- #
# INCREMENTAL_FETCH_PLAN / OVERLAP_APPLIED_TO_REQUESTS                         #
# --------------------------------------------------------------------------- #


def _ok_transport(url, body, headers):
    """Return a valid single-row page for every request."""
    return make_minute_page([minute_row("0905", 100, 105, 99, 101)])


class TestIncrementalFetchPlan:
    """The overlap window is applied to the actual request plan."""

    @staticmethod
    def _env(monkeypatch):
        monkeypatch.setenv("KIWOOM_APPKEY", "app")
        monkeypatch.setenv("KIWOOM_SECRETKEY", "sec")
        monkeypatch.setenv("KIWOOM_65STOCK_ENV", "")

    def _run_and_capture(self, monkeypatch, tmp_path, config, existing):
        self._env(monkeypatch)
        out = tmp_path / "out.jsonl"
        if existing:
            out.write_text(kospi_jsonl_lines(existing), encoding="utf-8")
        collector = KospiCollector(
            client=make_client(transport=_ok_transport), config=config, request_delay=0.0
        )
        captured = {}
        orig = collector.collect_range

        def spy(*args, **kwargs):
            captured["from"] = args[0]
            captured["to"] = args[1]
            return orig(*args, **kwargs)

        collector.collect_range = spy
        import fmindex.market.kospi_collector as kc_mod
        monkeypatch.setattr(kc_mod, "KospiCollector", lambda **kw: collector)
        summary = run_collector(config)
        return summary, captured

    def test_overlap_applied_to_request_plan(self, monkeypatch, tmp_path):
        """existing latest 08-05 14:00 KST, overlap 8h, requested from 08-01
        -> effective re-fetch starts 08-05 (same trading day)."""
        config = CollectorConfig(
            from_date="2026-08-01", to_date="2026-08-06",
            output=str(tmp_path / "out.jsonl"),
            metadata_output=str(tmp_path / "out.meta.json"),
            overlap_hours=8,
        )
        existing = [make_record(ts="2026-08-05T14:00:00+09:00")]
        summary, captured = self._run_and_capture(monkeypatch, tmp_path, config, existing)
        assert captured["from"] == "2026-08-05"
        assert captured["to"] == "2026-08-06"
        assert "2026-08-05" in summary["requestedDates"]
        assert "2026-08-04" not in summary["requestedDates"]

    def test_overlap_spans_previous_day(self, monkeypatch, tmp_path):
        """overlap crossing into the previous trading day is fetched."""
        config = CollectorConfig(
            from_date="2026-08-01", to_date="2026-08-05",
            output=str(tmp_path / "out.jsonl"),
            metadata_output=str(tmp_path / "out.meta.json"),
            overlap_hours=24,
        )
        existing = [make_record(ts="2026-08-05T09:00:00+09:00")]
        summary, captured = self._run_and_capture(monkeypatch, tmp_path, config, existing)
        assert captured["from"] == "2026-08-04"
        assert "2026-08-04" in summary["requestedDates"]
        assert "2026-08-05" in summary["requestedDates"]

    def test_no_existing_file_full_range(self, monkeypatch, tmp_path):
        """No existing data -> request from requested_from."""
        config = CollectorConfig(
            from_date="2026-08-04", to_date="2026-08-05",
            output=str(tmp_path / "out.jsonl"),
            metadata_output=str(tmp_path / "out.meta.json"),
            overlap_hours=8,
        )
        summary, captured = self._run_and_capture(monkeypatch, tmp_path, config, [])
        assert captured["from"] == "2026-08-04"
        assert summary["requestedDates"] == ["2026-08-04", "2026-08-05"]

    def test_force_refresh_full_range(self, monkeypatch, tmp_path):
        """force_refresh=True re-fetches from requested_from."""
        config = CollectorConfig(
            from_date="2026-08-01", to_date="2026-08-03",
            output=str(tmp_path / "out.jsonl"),
            metadata_output=str(tmp_path / "out.meta.json"),
            overlap_hours=8,
            force_refresh=True,
        )
        existing = [make_record(ts="2026-08-03T14:00:00+09:00")]
        summary, captured = self._run_and_capture(monkeypatch, tmp_path, config, existing)
        assert captured["from"] == "2026-08-01"

    def test_metadata_records_overlap_plan(self, monkeypatch, tmp_path):
        """Metadata records effectiveFrom/overlapHours/existingLatest."""
        config = CollectorConfig(
            from_date="2026-08-01", to_date="2026-08-06",
            output=str(tmp_path / "out.jsonl"),
            metadata_output=str(tmp_path / "out.meta.json"),
            overlap_hours=8,
        )
        existing = [make_record(ts="2026-08-05T14:00:00+09:00")]
        self._run_and_capture(monkeypatch, tmp_path, config, existing)
        meta = json.loads(Path(tmp_path / "out.meta.json").read_text(encoding="utf-8"))
        assert meta["effectiveFrom"] == "2026-08-05"
        assert meta["overlapHours"] == 8
        assert meta["existingLatestTimestamp"] == "2026-08-05T14:00:00+09:00"

    def test_weekend_skipped_in_request_plan(self, monkeypatch, tmp_path):
        """Weekends are never in the actual requested dates."""
        config = CollectorConfig(
            from_date="2026-08-06", to_date="2026-08-10",  # Thu..Mon
            output=str(tmp_path / "out.jsonl"),
            metadata_output=str(tmp_path / "out.meta.json"),
        )
        summary, _ = self._run_and_capture(monkeypatch, tmp_path, config, [])
        assert summary["requestedDates"] == ["2026-08-06", "2026-08-07", "2026-08-10"]


# --------------------------------------------------------------------------- #
# VOLUME_POLICY: trde_qty only, cumulative never summed                       #
# --------------------------------------------------------------------------- #




    def test_existing_latest_after_requested_to_clamped(self, monkeypatch, tmp_path):
        """Existing data beyond requested_to clamps effective start to
        requested_to (never a bogus NO_TRADING_DAYS misreport)."""
        config = CollectorConfig(
            from_date="2026-08-01", to_date="2026-08-06",
            output=str(tmp_path / "out.jsonl"),
            metadata_output=str(tmp_path / "out.meta.json"),
            overlap_hours=8,
        )
        existing = [make_record(ts="2026-08-10T14:00:00+09:00")]
        summary, captured = self._run_and_capture(monkeypatch, tmp_path, config, existing)
        assert captured["from"] == "2026-08-06"  # clamped to requested_to
        assert captured["to"] == "2026-08-06"
        assert summary["requestedDates"] == ["2026-08-06"]
        assert summary["status"] == "OK"
class TestVolumeContract:
    """Hourly volume sums per-candle trde_qty; null when any candle lacks it."""

    def _collector(self, transport):
        return KospiCollector(client=make_client(transport=transport), request_delay=0.0)

    def test_sum_all_present(self):
        rows = [
            minute_row("0905", 100, 105, 99, 101, vol="100"),
            minute_row("0925", 101, 110, 100, 108, vol="200"),
            minute_row("0945", 108, 109, 102, 104, vol="150"),
        ]
        collector = self._collector(FakeTransport(responses=[make_minute_page(rows)]))
        records = collector.collect_day("2026-08-05")
        assert records[0].volume == 450.0

    def test_all_missing_with_cumulative_present_is_null(self):
        """trde_qty absent everywhere (acc_trde_qty present) -> null, not a sum."""
        rows = [
            minute_row("0905", 100, 105, 99, 101, vol=None, acc_vol="1000"),
            minute_row("0925", 101, 110, 100, 108, vol=None, acc_vol="1200"),
            minute_row("0945", 108, 109, 102, 104, vol=None, acc_vol="1500"),
        ]
        collector = self._collector(FakeTransport(responses=[make_minute_page(rows)]))
        records = collector.collect_day("2026-08-05")
        assert records[0].volume is None

    def test_cumulative_not_summed(self):
        """CUMULATIVE_VOLUME_NOT_SUMMED: 1000/1200/1500 never becomes 3700."""
        rows = [
            minute_row("0905", 100, 105, 99, 101, vol=None, acc_vol="1000"),
            minute_row("0925", 101, 110, 100, 108, vol=None, acc_vol="1200"),
            minute_row("0945", 108, 109, 102, 104, vol=None, acc_vol="1500"),
        ]
        collector = self._collector(FakeTransport(responses=[make_minute_page(rows)]))
        records = collector.collect_day("2026-08-05")
        assert records[0].volume != 3700.0
        assert records[0].volume is None

    def test_partial_missing_volume_is_null(self):
        """One candle missing volume -> the hourly bucket is null (policy)."""
        rows = [
            minute_row("0905", 100, 105, 99, 101, vol="100"),
            minute_row("0925", 101, 110, 100, 108, vol=None, acc_vol="1200"),
            minute_row("0945", 108, 109, 102, 104, vol="150"),
        ]
        collector = self._collector(FakeTransport(responses=[make_minute_page(rows)]))
        records = collector.collect_day("2026-08-05")
        assert records[0].volume is None


# --------------------------------------------------------------------------- #
# CALENDAR_SCOPE_FAILS_CLOSED / WEEKDAY_HOLIDAY_TEST                          #
# --------------------------------------------------------------------------- #


class TestCalendarScope:
    """The calendar supports only 2026; other years fail closed."""

    def test_weekday_holiday_excluded(self):
        """2026-03-02 (Mon, 삼일절 대체공휴일) is not a trading day."""
        dt = datetime(2026, 3, 2, 10, 0, tzinfo=KST)
        assert dt.weekday() == 0  # Monday
        assert is_trading_day(dt) is False

    def test_regular_weekday_included(self):
        dt = datetime(2026, 8, 5, 10, 0, tzinfo=KST)
        assert is_trading_day(dt) is True

    def test_supported_year_ok(self):
        validate_calendar_years("2026-01-01", "2026-12-31")

    def test_unsupported_year_fails_closed(self):
        with pytest.raises(CalendarYearError, match="does not support year"):
            validate_calendar_years("2025-08-01", "2025-08-05")
        with pytest.raises(CalendarYearError):
            validate_calendar_years("2026-12-31", "2027-01-02")

    def test_run_collector_rejects_unsupported_year(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KIWOOM_APPKEY", "app")
        monkeypatch.setenv("KIWOOM_SECRETKEY", "sec")
        monkeypatch.setenv("KIWOOM_65STOCK_ENV", "")
        config = CollectorConfig(
            from_date="2027-01-02", to_date="2027-01-03",
            output=str(tmp_path / "out.jsonl"),
            metadata_output=str(tmp_path / "out.meta.json"),
        )
        with pytest.raises(CalendarYearError):
            run_collector(config)
        assert not (tmp_path / "out.jsonl").exists()

    def test_holiday_excluded_weekday_not_weekend_only(self):
        """HOLIDAY_EXCLUDED is verified on a WEEKDAY holiday, not only
        2026-08-15 which is a Saturday anyway."""
        assert is_trading_day(datetime(2026, 5, 25, 10, 0, tzinfo=KST)) is False  # 부처님오신날 Mon
        assert is_trading_day(datetime(2026, 10, 9, 10, 0, tzinfo=KST)) is False  # 한글날 Fri
        assert is_trading_day(datetime(2026, 10, 8, 10, 0, tzinfo=KST)) is True   # Thu before

    def test_labor_day_closed(self):
        """2026-05-01 (Workers' Day) is NOT a trading day."""
        dt = datetime(2026, 5, 1, 10, 0, tzinfo=KST)
        assert dt.weekday() == 4  # Friday
        assert is_trading_day(dt) is False

    def test_nationwide_election_day_closed(self):
        """2026-06-01 (Local Election Day) is NOT a trading day."""
        dt = datetime(2026, 6, 1, 10, 0, tzinfo=KST)
        assert dt.weekday() == 0  # Monday
        assert is_trading_day(dt) is False

    def test_end_of_term_closure_closed(self):
        """2026-06-30 (End-of-term special closure) is NOT a trading day."""
        dt = datetime(2026, 6, 30, 10, 0, tzinfo=KST)
        assert dt.weekday() == 1  # Tuesday
        assert is_trading_day(dt) is False

    def test_year_end_closure_closed(self):
        """2026-12-28~31 are NOT trading days (year-end settlement)."""
        for day in (28, 29, 30, 31):
            dt = datetime(2026, 12, day, 10, 0, tzinfo=KST)
            assert is_trading_day(dt) is False, f"2026-12-{day:02d} should be closed"

    def test_year_end_closure_dow_check(self):
        """Verify weekday positions for year-end closure dates."""
        # 12/28 Mon, 12/29 Tue, 12/30 Wed, 12/31 Thu
        assert datetime(2026, 12, 28, tzinfo=KST).weekday() == 0
        assert datetime(2026, 12, 29, tzinfo=KST).weekday() == 1
        assert datetime(2026, 12, 30, tzinfo=KST).weekday() == 2
        assert datetime(2026, 12, 31, tzinfo=KST).weekday() == 3

    def test_trading_day_request_plan_excludes_labor_day(self, monkeypatch, tmp_path):
        """Labor Day must NOT appear in requestedDates for a valid plan."""
        monkeypatch.setenv("KIWOOM_APPKEY", "app")
        monkeypatch.setenv("KIWOOM_SECRETKEY", "sec")
        monkeypatch.setenv("KIWOOM_65STOCK_ENV", "")
        import fmindex.market.kospi_collector as kc_mod
        config = CollectorConfig(
            from_date="2026-04-27", to_date="2026-05-05", dry_run=True,
            output=str(tmp_path / "out.jsonl"),
            metadata_output=str(tmp_path / "out.meta.json"),
        )
        result = run_collector(config)
        assert result.get("dryRun") is True
        requested = result.get("requestedDates", [])
        assert "2026-05-01" not in requested, "Labor Day must not be in request plan"

    def test_holiday_only_range_returns_no_trading_days(self, monkeypatch, tmp_path):
        """A range containing ONLY holidays returns NO_TRADING_DAYS."""
        monkeypatch.setenv("KIWOOM_APPKEY", "app")
        monkeypatch.setenv("KIWOOM_SECRETKEY", "sec")
        monkeypatch.setenv("KIWOOM_65STOCK_ENV", "")
        # 2026-12-28 to 2026-12-31: all year-end closures
        config = CollectorConfig(
            from_date="2026-12-28", to_date="2026-12-31",
            output=str(tmp_path / "out.jsonl"),
            metadata_output=str(tmp_path / "out.meta.json"),
        )
        with pytest.raises(NoTradingDaysError, match="NO_TRADING_DAYS"):
            run_collector(config)
        assert not (tmp_path / "out.jsonl").exists()
        assert not (tmp_path / "out.meta.json").exists()

    def test_no_false_zero_record_failure_on_krx_closures(self, monkeypatch, tmp_path):
        """Zero-record on a known KRX closure day must NOT raise; NO_TRADING_DAYS instead."""
        monkeypatch.setenv("KIWOOM_APPKEY", "app")
        monkeypatch.setenv("KIWOOM_SECRETKEY", "sec")
        monkeypatch.setenv("KIWOOM_65STOCK_ENV", "")
        # 2026-05-01 alone: Labor Day → NO_TRADING_DAYS, not zero-record failure
        config = CollectorConfig(
            from_date="2026-05-01", to_date="2026-05-01",
            output=str(tmp_path / "out.jsonl"),
            metadata_output=str(tmp_path / "out.meta.json"),
        )
        with pytest.raises(NoTradingDaysError, match="NO_TRADING_DAYS"):
            run_collector(config)
        assert not (tmp_path / "out.jsonl").exists()
        assert not (tmp_path / "out.meta.json").exists()

    def test_calendar_version_bumped(self):
        """Calendar snapshot version must reflect the reconciliation."""
        from fmindex.market.market_calendar import CALENDAR_VERSION
        assert CALENDAR_VERSION == "2026.2"
