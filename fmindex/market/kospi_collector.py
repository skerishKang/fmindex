"""KOSPI hourly index collector (official Kiwoom REST API).

Endpoints (confirmed official contract):

- ka20005 업종분봉조회요청 — POST /api/dostk/chart
  body: inds_cd="001" (종합/KOSPI), tic_scope (1|3|5|10|15|30|45|60), base_dt (YYYYMMDD)
  table: inds_min_pole_qry — fields: cur_prc, trde_qty, cntr_tm, open_pric,
         high_pric, low_pric, acc_trde_qty, pred_pre, pred_pre_sig
- ka20006 업종일봉조회요청 — POST /api/dostk/chart
  body: inds_cd="001", base_dt (YYYYMMDD)
  table: inds_dt_pole_qry — fields: cur_prc, trde_qty, dt, open_pric,
         high_pric, low_pric, trde_prica

When the API returns true 60-minute index candles (tic_scope=60),
those are used directly. Minute buckets (tic_scope < 60) are aggregated
into hourly OHLC: open=first open, high=max high, low=min low,
close=last close, volume=sum (null if the API does not provide it).

A controlled same-token/same-endpoint A/B probe against the live Kiwoom
API confirmed the ka20005 wire contract is a JSON **string**: sending
tic_scope="60" returns return_code=0 with data, while the JSON number 60
returns return_code=2 (type mismatch). CLI and CollectorConfig therefore
canonicalize tic_scope to a string, and invalid values fail fast before
any API request. The single initial string-state live failure was not
confirmed to be caused by the tic_scope string and is not recorded as one.

Usage:
    python -m fmindex.market.kospi_collector --from 2026-07-01 --to 2026-08-05
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .models import (
    ASSET_TYPE_INDEX,
    DATA_MODE_REAL,
    KOSPI_INDS_CD,
    KOSPI_MRKT_TP,
    KOSPI_SYMBOL,
    PROVIDER,
    Candle,
    CollectionMetadata,
    HourlyIndexRecord,
)
from .market_calendar import (
    CALENDAR_SOURCE,
    CALENDAR_VERSION,
    KST,
    SUPPORTED_CALENDAR_YEARS,
    bucket_for_timestamp,
    is_trading_day,
    validate_calendar_years,
)
from .kiwoom_auth import KiwoomAuthError, KiwoomTokenManager, credentials_available
from .kiwoom_client import (
    API_CONTRACT_VERSION,
    BudgetExhaustedError,
    KiwoomClient,
    KiwoomPaginationError,
    KiwoomRateLimitError,
    KiwoomAPIError,
    KiwoomResponse,
    MutableBudgetGate,
)

#: Official endpoint paths (confirmed contract).
SECTOR_PATH = "/api/dostk/chart"

#: Official TR ids for index minute / daily chart.
API_ID_MINUTE_CHART = "ka20005"
API_ID_DAILY_CHART = "ka20006"

#: Response table keys per the official contract.
TABLE_MINUTE = "inds_min_pole_qry"
TABLE_DAILY = "inds_dt_pole_qry"

COLLECTOR_VERSION = "1.0.0"

#: Default output paths.
DEFAULT_OUTPUT = "output/market/kospi-hourly.jsonl"
DEFAULT_METADATA = "output/market/kospi-hourly.meta.json"

#: Max requests per run when --max-requests is not given (safety default).
DEFAULT_MAX_REQUESTS = 500

#: Default overlap window (hours) re-fetched on incremental runs.
DEFAULT_OVERLAP_HOURS = 8

#: Allowed tic_scope values. A controlled same-token/same-endpoint A/B
#: probe against the live Kiwoom API confirmed the ka20005 wire contract is
#: a JSON **string**: tic_scope="60" returns return_code=0 with data, while
#: the JSON number 60 returns return_code=2 (type mismatch). The canonical
#: internal type is therefore str.
ALLOWED_TIC_SCOPES = (
    "1",
    "3",
    "5",
    "10",
    "15",
    "30",
    "45",
    "60",
)


def _canonicalize_tic_scope(value: Any) -> str:
    """Return ``value`` as a canonical string tic_scope or raise ValueError.

    Accepted inputs (canonical wire form is always a string):
    - string in ALLOWED_TIC_SCOPES (e.g. "60")
    - int in {1,3,5,10,15,30,45,60} for programmatic compatibility
      (converted to its exact string form, e.g. 60 -> "60")

    Rejected inputs raise ValueError before any API request is made:
    "0", "2", "59", "90", "05", "060", " 60", "60 ", "abc",
    0, 2, 59, 90, None, True, False, floats, lists, dicts, etc.
    """
    if isinstance(value, bool):
        raise ValueError(
            f"invalid tic_scope {value!r}: expected one of {ALLOWED_TIC_SCOPES}"
        )
    if isinstance(value, str):
        if value in ALLOWED_TIC_SCOPES:
            return value
        raise ValueError(
            f"invalid tic_scope {value!r}: expected one of {ALLOWED_TIC_SCOPES}"
        )
    if isinstance(value, int):
        string_form = str(value)
        if string_form in ALLOWED_TIC_SCOPES:
            return string_form
        raise ValueError(
            f"invalid tic_scope {value!r}: expected one of {ALLOWED_TIC_SCOPES}"
        )
    raise ValueError(
        f"invalid tic_scope {value!r}: expected one of {ALLOWED_TIC_SCOPES}"
    )


class NoTradingDaysError(RuntimeError):
    """Raised when the requested range contains no supported trading days.

    This is an explicit NO_TRADING_DAYS status: no output is fabricated
    and the run is not reported as a successful real collection.
    """


class CollectorConfig:
    """CLI/config knobs for the collector."""

    def __init__(
        self,
        from_date: str,
        to_date: str,
        output: str = DEFAULT_OUTPUT,
        metadata_output: str = DEFAULT_METADATA,
        dry_run: bool = False,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        request_delay: float = 1.0,
        force_refresh: bool = False,
        overlap_hours: int = DEFAULT_OVERLAP_HOURS,
        tic_scope: str = "60",
    ) -> None:
        self.from_date = from_date
        self.to_date = to_date
        self.output = output
        self.metadata_output = metadata_output
        self.dry_run = dry_run
        self.max_requests = max_requests
        self.request_delay = request_delay
        self.force_refresh = force_refresh
        self.overlap_hours = overlap_hours
        # Canonical string; validated here so the request body only ever
        # carries a string tic_scope (the live API wire contract).
        self.tic_scope = _canonicalize_tic_scope(tic_scope)


class KospiCollector:
    """Collects validated 1-hour OHLC records for the KOSPI index.

    Only the KOSPI index instrument (inds_cd="001", assetType=index) is
    accepted. Individual stock candles are never merged into the KOSPI
    series. Out-of-session data is excluded via market_calendar.
    """

    def __init__(
        self,
        client: Optional[KiwoomClient] = None,
        config: Optional[CollectorConfig] = None,
        request_delay: float = 1.0,
    ) -> None:
        self.client = client or KiwoomClient()
        self.config = config
        self.request_delay = request_delay
        # Shared hard budget gate — pagination and retry both consume from it.
        if config is not None:
            self._budget_gate = MutableBudgetGate(config.max_requests)
            self.client.budget_gate = self._budget_gate
        else:
            self._budget_gate = None
        self.requests_made = 0
        self.records_received = 0
        self.records_accepted = 0
        self.records_rejected = 0
        self.duplicates_removed = 0
        self.partial_buckets = 0
        self.warnings: List[str] = []
        # Detailed row classification (never includes raw values).
        self.target_date_rows = 0
        self.other_date_rows = 0
        self.invalid_timestamp_rows = 0
        self.invalid_price_rows = 0
        self.out_of_session_rows = 0
        self.accepted_candles = 0

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #

    def collect_range(
        self,
        from_date: str,
        to_date: str,
        requested_dates: Optional[List[str]] = None,
    ) -> List[HourlyIndexRecord]:
        """Backfill hourly KOSPI records for every trading day in range.

        ``requested_dates`` (when provided) is appended with every date
        actually fetched, so tests and metadata can assert the REAL
        request plan rather than only the merged result.
        """
        start = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=KST)
        end = datetime.strptime(to_date, "%Y-%m-%d").replace(
            tzinfo=KST, hour=23, minute=59, second=59
        )
        if start > end:
            raise ValueError(
                f"Invalid range: from={from_date} is after to={to_date}"
            )

        records: Dict[Tuple[str, str, str], HourlyIndexRecord] = {}
        day = start
        while day.date() <= end.date():
            if is_trading_day(day):
                if requested_dates is not None:
                    requested_dates.append(day.date().isoformat())
                daily = self.collect_day(day.date())
                for rec in daily:
                    key = (rec.provider, rec.instrument_id, rec.timestamp)
                    records[key] = rec
                if self.request_delay > 0:
                    # interval is enforced by the client throttle too; this
                    # adds an explicit cross-day pacing knob.
                    time.sleep(self.request_delay)
            day += timedelta(days=1)
            self._check_request_budget()

        return sorted(records.values(), key=lambda r: r.timestamp)

    def collect_day(self, trade_date) -> List[HourlyIndexRecord]:
        """Collect hourly records for a single trading date.

        Uses the official 60-minute index chart (ka20005, tic_scope=60)
        when available; falls back to aggregating minute candles otherwise.
        """
        if hasattr(trade_date, "strftime"):
            date_str = trade_date.strftime("%Y%m%d")
        else:
            date_str = str(trade_date).replace("-", "")[:8]
        buckets: Dict[str, List[Candle]] = {}

        minute_pages = self._fetch_minute_pages(date_str)
        raw_candles = self._parse_minute_pages(minute_pages, date_str)
        for candle in raw_candles:
            bucket = bucket_for_timestamp(candle.timestamp)
            if bucket is None:
                # Out-of-session / invalid-time rows are rejected here.
                self.records_rejected += 1
                continue
            key = bucket[0].isoformat()
            is_new_bucket = key not in buckets
            buckets.setdefault(key, []).append(candle)
            # Count a partial bucket once, not once per constituent candle.
            if bucket[1] and is_new_bucket:
                self.partial_buckets += 1

        if not buckets and not (self.config and self.config.dry_run):
            self.warnings.append(f"No candles for {date_str} (holiday or no data).")

        records: List[HourlyIndexRecord] = []
        for bucket_start, candles in sorted(buckets.items()):
            agg = self._aggregate_hourly(candles)
            if agg is None:
                continue
            is_partial = any(c.is_partial for c in candles)
            records.append(agg)
            self.records_accepted += 1
        return records

    # ------------------------------------------------------------------ #
    # Fetching                                                            #
    # ------------------------------------------------------------------ #

    def _fetch_minute_pages(self, date_str: str) -> List[KiwoomResponse]:
        """Fetch ka20005 (업종분봉) pages for one date.

        Fail-closed: KiwoomAuthError / KiwoomAPIError / KiwoomRateLimitError
        / KiwoomPaginationError / transport timeout final failures are
        propagated to the caller — never swallowed into an empty page set.
        """
        if self.config and self.config.dry_run:
            return []
        body: Dict[str, Any] = {
            "mrkt_tp": KOSPI_MRKT_TP,
            "inds_cd": KOSPI_INDS_CD,
            "tic_scope": (self.config.tic_scope if self.config else "60"),
            "base_dt": date_str,
        }
        try:
            pages = self.client.fetch_all(API_ID_MINUTE_CHART, SECTOR_PATH, body)
        except BudgetExhaustedError as exc:
            raise KiwoomRateLimitError(str(exc)) from exc
        self.requests_made = self.client.requests_made
        return pages

    def _parse_minute_pages(
        self, pages: List[KiwoomResponse], date_str: str
    ) -> List[Candle]:
        """Parse ka20005 response tables into Candle records (KST).

        Counting semantics (no double counting):
        - records_received: every raw row received from the API
        - records_rejected: rows rejected during parse/time/contract checks
        - records_accepted: final hourly records (counted in collect_day)
        - target_date_rows / other_date_rows / invalid_timestamp_rows /
          invalid_price_rows: detailed classification (no raw values)
        """
        candles: List[Candle] = []
        for page in pages:
            rows = page.body.get(TABLE_MINUTE, [])
            if not isinstance(rows, list):
                continue
            for row in rows:
                self.records_received += 1
                status = self._row_timestamp_status(row, date_str)
                if status == "other_date":
                    self.other_date_rows += 1
                    self.records_rejected += 1
                    continue
                if status == "invalid":
                    self.invalid_timestamp_rows += 1
                    self.records_rejected += 1
                    continue
                self.target_date_rows += 1
                candle = self._candle_from_minute_row(row, date_str)
                if candle is None:
                    self.invalid_price_rows += 1
                    self.records_rejected += 1
                    continue
                self.accepted_candles += 1
                candles.append(candle)
        return candles

    def _candle_from_minute_row(
        self, row: Dict[str, Any], date_str: str
    ) -> Optional[Candle]:
        """Map a ka20005 row to a Candle using official field names.

        OHLC uses ``_parse_price_magnitude`` (signed values become positive
        magnitudes); volume uses ``_parse_nonnegative_number``. Rows from
        dates other than ``date_str`` are rejected.
        """
        o = self._parse_price_magnitude(row.get("open_pric"))
        h = self._parse_price_magnitude(row.get("high_pric"))
        l = self._parse_price_magnitude(row.get("low_pric"))
        c = self._parse_price_magnitude(row.get("cur_prc"))
        if o is None or h is None or l is None or c is None:
            return None

        ts = self._timestamp_from_row(row, date_str)
        if ts is None:
            return None

        # Volume contract: only the per-candle trde_qty is used. acc_trde_qty
        # is a CUMULATIVE total for the day — summing it across rows would
        # fabricate volume, so it is never used as a per-candle volume.
        volume = self._parse_nonnegative_number(row.get("trde_qty"))

        bucket = bucket_for_timestamp(ts)
        is_partial = bool(bucket and bucket[1]) if bucket else False

        return Candle(
            timestamp=ts,
            open=o,
            high=h,
            low=l,
            close=c,
            volume=volume,
            instrument_id=KOSPI_INDS_CD,
            symbol=KOSPI_SYMBOL,
            is_partial=is_partial,
            raw=row,
        )

    # ------------------------------------------------------------------ #
    # Aggregation                                                         #
    # ------------------------------------------------------------------ #

    def _aggregate_hourly(self, candles: List[Candle]) -> Optional[HourlyIndexRecord]:
        """Aggregate candles into a validated hourly KOSPI record."""
        if not candles:
            return None
        sorted_c = sorted(candles, key=lambda c: c.timestamp)
        open_p = sorted_c[0].open
        high_p = max(c.high for c in sorted_c)
        low_p = min(c.low for c in sorted_c)
        close_p = sorted_c[-1].close

        # Volume policy: sum only when EVERY constituent candle carries a
        # per-candle volume. If any candle lacks volume (e.g. trde_qty
        # absent), the hourly volume is null — never a partial/fabricated sum.
        if all(c.volume is not None for c in sorted_c):
            volume = round(sum(c.volume for c in sorted_c), 4)  # type: ignore[arg-type]
        else:
            volume = None

        change_rate = 0.0
        if open_p != 0:
            change_rate = round(((close_p - open_p) / open_p) * 100.0, 4)

        bucket_start = bucket_for_timestamp(sorted_c[0].timestamp)
        if bucket_start is None:
            return None
        ts = bucket_start[0].replace(minute=0, second=0, microsecond=0).isoformat()
        is_partial = any(c.is_partial for c in sorted_c)

        return HourlyIndexRecord(
            timestamp=ts,
            market="KOSPI",
            instrument_id=KOSPI_INDS_CD,
            symbol=KOSPI_SYMBOL,
            asset_type=ASSET_TYPE_INDEX,
            open=round(open_p, 4),
            high=round(high_p, 4),
            low=round(low_p, 4),
            close=round(close_p, 4),
            volume=volume,
            change_rate=change_rate,
            source="kiwoom-rest-api",
            provider=PROVIDER,
            observed_at=datetime.now(KST).isoformat(),
            data_mode=DATA_MODE_REAL,
            is_partial=is_partial,
        )

    # ------------------------------------------------------------------ #
    # Incremental merge + dedup                                           #
    # ------------------------------------------------------------------ #

    def merge_existing(
        self, existing: List[HourlyIndexRecord], fresh: List[HourlyIndexRecord]
    ) -> List[HourlyIndexRecord]:
        """Merge fresh records into existing ones with dedup rules.

        Dedup key: (provider, instrumentId, timestamp). For the same key
        the record with the newest ``observedAt`` wins; when observedAt
        is equal, the more complete OHLC wins. Records are sorted by
        timestamp ascending. Only KOSPI-index records are kept.
        """
        combined: Dict[Tuple[str, str, str], HourlyIndexRecord] = {}
        for rec in existing + fresh:
            if not self._is_valid_kospi_record(rec):
                self.records_rejected += 1
                continue
            key = (rec.provider, rec.instrument_id, rec.timestamp)
            prev = combined.get(key)
            if prev is None:
                combined[key] = rec
                continue
            winner = self._pick_winner(prev, rec)
            if winner is rec:
                self.duplicates_removed += 1
            combined[key] = winner
        return sorted(combined.values(), key=lambda r: r.timestamp)

    @staticmethod
    def _pick_winner(
        a: HourlyIndexRecord, b: HourlyIndexRecord
    ) -> HourlyIndexRecord:
        if a.observed_at == b.observed_at:
            # More complete OHLC wins.
            a_score = sum(
                1 for v in (a.open, a.high, a.low, a.close) if v is not None
            )
            b_score = sum(
                1 for v in (b.open, b.high, b.low, b.close) if v is not None
            )
            if b_score > a_score:
                return b
            return a
        return b if b.observed_at > a.observed_at else a

    @staticmethod
    def _is_valid_kospi_record(rec: HourlyIndexRecord) -> bool:
        """Validate the full KOSPI instrument contract (exact match only).

        No substring matching: instrumentId must be exactly "001", symbol
        exactly "KOSPI", market exactly "KOSPI". Any other instrument,
        symbol, market, provider, asset type, or data mode is rejected.
        """
        if rec.provider != PROVIDER:
            return False
        if rec.asset_type != ASSET_TYPE_INDEX:
            return False
        if rec.data_mode != DATA_MODE_REAL:
            return False
        if rec.instrument_id != KOSPI_INDS_CD:
            return False
        if rec.symbol.upper() != KOSPI_SYMBOL.upper():
            return False
        if rec.market.upper() != KOSPI_SYMBOL.upper():
            return False
        if rec.open is None or rec.high is None or rec.low is None or rec.close is None:
            return False
        if rec.high < max(rec.open, rec.close):
            return False
        if rec.low > min(rec.open, rec.close):
            return False
        return True

    # ------------------------------------------------------------------ #
    # I/O                                                                 #
    # ------------------------------------------------------------------ #

    def load_existing(self, path: Path) -> List[HourlyIndexRecord]:
        """Load previously collected JSONL records."""
        if not path.exists():
            return []
        records: List[HourlyIndexRecord] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
                records.append(_record_from_dict(raw))
            except (json.JSONDecodeError, ValueError):
                self.records_rejected += 1
        return records

    def write_records(self, path: Path, records: List[HourlyIndexRecord]) -> None:
        """Write records as timestamp-sorted JSONL (atomic replace).

        Data is written to a temporary sibling file first and atomically
        moved over the target only on success, so a failed run never
        truncates or corrupts an existing good output file.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                for rec in sorted(records, key=lambda r: r.timestamp):
                    f.write(json.dumps(rec.to_dict(), ensure_ascii=False) + chr(10))
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()

    def build_metadata(
        self,
        from_date: str,
        to_date: str,
        records: List[HourlyIndexRecord],
        overlap_hours: int = 0,
        existing_latest_timestamp: str = "",
        effective_from: Optional[str] = None,
        no_trading_days: bool = False,
    ) -> CollectionMetadata:
        """Build run metadata (never contains secrets)."""
        eff_from = effective_from or (records[0].timestamp if records else from_date)
        eff_to = records[-1].timestamp if records else to_date
        latest = records[-1].timestamp if records else ""
        return CollectionMetadata(
            provider=PROVIDER,
            instrument_id=KOSPI_INDS_CD,
            symbol=KOSPI_SYMBOL,
            asset_type=ASSET_TYPE_INDEX,
            requested_from=from_date,
            requested_to=to_date,
            effective_from=eff_from,
            effective_to=eff_to,
            requests_made=self.requests_made,
            records_received=self.records_received,
            records_accepted=self.records_accepted,
            records_rejected=self.records_rejected,
            duplicates_removed=self.duplicates_removed,
            partial_buckets=self.partial_buckets,
            latest_timestamp=latest,
            generated_at=datetime.now(KST).isoformat(),
            data_mode=DATA_MODE_REAL,
            api_contract_version=API_CONTRACT_VERSION,
            collector_version=COLLECTOR_VERSION,
            warnings=list(self.warnings),
            overlap_hours=overlap_hours,
            existing_latest_timestamp=existing_latest_timestamp,
            calendar_source=CALENDAR_SOURCE,
            calendar_version=CALENDAR_VERSION,
            supported_calendar_years=list(SUPPORTED_CALENDAR_YEARS),
            no_trading_days=no_trading_days,
        )

    # ------------------------------------------------------------------ #
    # Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _check_request_budget(self) -> None:
        limit = self.config.max_requests if self.config else DEFAULT_MAX_REQUESTS
        if self.requests_made >= limit:
            raise KiwoomRateLimitError(
                f"Max requests reached ({limit}). Stopping collection."
            )
        if self._budget_gate is not None:
            try:
                self._budget_gate.consume()
            except BudgetExhaustedError as exc:
                raise KiwoomRateLimitError(str(exc)) from exc

    @staticmethod
    def _float(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_price_magnitude(value: Any) -> Optional[float]:
        """Parse a signed OHLC value into a finite positive magnitude.

        Kiwoom ka20005 OHLC strings may carry a leading ``+`` or ``-`` sign
        that is a direction marker, separate from the index magnitude. Only
        the absolute magnitude is used for open_pric/high_pric/low_pric/
        cur_prc. Zero or negative final magnitudes, NaN, Infinity,
        multiple/mixed signs, and non-numeric input are rejected (None).
        """
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        if text in ("+", "-"):
            return None
        if text.count("+") > 1 or text.count("-") > 1:
            return None
        if "+" in text and "-" in text:
            return None
        try:
            f = float(text)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(f):
            return None
        magnitude = abs(f)
        if magnitude <= 0:
            return None
        return magnitude

    @staticmethod
    def _parse_nonnegative_number(value: Any) -> Optional[float]:
        """Parse a non-negative quantity (e.g. trde_qty) or return None.

        Rejects negative values, NaN, Infinity, mixed/multiple signs, and
        non-numeric input. Never applies absolute-value normalization (a
        negative quantity is invalid, not a positive one in disguise).
        """
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        if text in ("+", "-"):
            return None
        if text.count("+") > 1 or text.count("-") > 1:
            return None
        if "+" in text and "-" in text:
            return None
        try:
            f = float(text)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(f):
            return None
        if f < 0:
            return None
        return f

    @staticmethod
    def _row_timestamp_status(row: Dict[str, Any], date_str: str) -> str:
        """Classify a row's cntr_tm without exposing the raw value.

        Returns one of:
        - ``target``: valid timestamp on the requested date
        - ``other_date``: a well-formed 14-digit timestamp whose embedded
          date differs from ``date_str`` (valid but not requested)
        - ``invalid``: missing, malformed, or out-of-range timestamp
        """
        tm = row.get("cntr_tm")
        if tm is None:
            return "invalid"
        raw = str(tm)
        if raw != raw.strip():
            # Leading/trailing extra characters are invalid.
            return "invalid"
        text = raw.strip()
        if not text or not text.isdigit():
            return "invalid"
        if len(text) == 14:
            try:
                dt = datetime.strptime(text, "%Y%m%d%H%M%S")
            except ValueError:
                return "invalid"
            if dt.strftime("%Y%m%d") != date_str:
                return "other_date"
            return "target"
        if len(text) in (4, 6):
            # Legacy fixture formats carry no embedded date; they are always
            # interpreted against the requested date.
            return "target"
        return "invalid"

    @staticmethod
    def _timestamp_from_row(row: Dict[str, Any], date_str: str) -> Optional[str]:
        """Build a KST-aware ISO timestamp from a chart row.

        Supported cntr_tm formats:
        - 14 digits YYYYMMDDHHMMSS (the live ka20005 response format, e.g.
          ``20260805150000``). The embedded date MUST equal ``date_str``;
          rows from other dates are rejected, never relabelled.
        - 6 digits HHMMSS (legacy fixture compatibility).
        - 4 digits HHMM (legacy fixture compatibility).

        Missing or malformed timestamps return None (no 09:00 fallback, no
        fabricated time). Invalid dates/times are rejected.
        """
        tm = row.get("cntr_tm")
        if tm is None:
            return None
        raw = str(tm)
        if raw != raw.strip():
            # Leading/trailing extra characters are rejected.
            return None
        text = raw.strip()
        if not text or not text.isdigit():
            return None
        if len(text) == 14:
            try:
                dt = datetime.strptime(text, "%Y%m%d%H%M%S")
            except ValueError:
                return None
            if dt.strftime("%Y%m%d") != date_str:
                # Other-date row: valid timestamp, but not the requested day.
                return None
            return dt.replace(tzinfo=KST).isoformat()
        if len(text) == 6:
            try:
                hour, minute, second = int(text[:2]), int(text[2:4]), int(text[4:6])
            except ValueError:
                return None
            if hour > 23 or minute > 59 or second > 59:
                return None
            try:
                return datetime.strptime(
                    f"{date_str} {hour:02d}:{minute:02d}:{second:02d}",
                    "%Y%m%d %H:%M:%S",
                ).replace(tzinfo=KST).isoformat()
            except ValueError:
                return None
        if len(text) == 4:
            try:
                hour, minute = int(text[:2]), int(text[2:4])
            except ValueError:
                return None
            if hour > 23 or minute > 59:
                return None
            try:
                return datetime.strptime(
                    f"{date_str} {hour:02d}:{minute:02d}", "%Y%m%d %H:%M"
                ).replace(tzinfo=KST).isoformat()
            except ValueError:
                return None
        return None


def _record_from_dict(raw: Dict[str, Any]) -> HourlyIndexRecord:
    """Rehydrate an HourlyIndexRecord from its camelCase JSON dict."""
    required = [
        "timestamp", "market", "instrumentId", "symbol", "assetType",
        "open", "high", "low", "close", "changeRate", "source", "provider",
        "observedAt", "dataMode",
    ]
    missing = [k for k in required if k not in raw]
    if missing:
        raise ValueError(f"record missing fields: {missing}")
    return HourlyIndexRecord(
        timestamp=raw["timestamp"],
        market=raw["market"],
        instrument_id=raw["instrumentId"],
        symbol=raw["symbol"],
        asset_type=raw["assetType"],
        open=float(raw["open"]),
        high=float(raw["high"]),
        low=float(raw["low"]),
        close=float(raw["close"]),
        volume=raw.get("volume"),
        change_rate=float(raw["changeRate"]),
        source=raw["source"],
        provider=raw["provider"],
        observed_at=raw["observedAt"],
        data_mode=raw["dataMode"],
        is_partial=bool(raw.get("isPartial", False)),
    )


def run_collector(config: CollectorConfig) -> Dict[str, Any]:
    """Run the collector CLI flow and return a summary dict.

    Fail-closed guarantees:
    - The requested range must be within the supported calendar years.
    - On API/auth/pagination errors nothing is written: existing output
      files stay byte-identical (atomic writes) and no "successful real"
      metadata is produced.
    - A range with at least one trading day that yields zero accepted
      records fails instead of emitting an empty/fake real series.
    - A range with zero supported trading days raises ``NoTradingDaysError``
      (explicit NO_TRADING_DAYS status; no fabricated output).
    - Incremental runs re-fetch from ``max(requested_from,
      latest_existing_timestamp - overlap_hours)`` so the overlap is
      actually applied to the request plan, not just the merge.
    """
    if not credentials_available():
        raise KiwoomAuthError(
            "Kiwoom credentials not configured (KIWOOM_APPKEY/KIWOOM_SECRETKEY). "
            "LIVE_SMOKE=SKIPPED_NO_CREDENTIALS"
        )

    # Calendar year scope fails closed before any request is made.
    validate_calendar_years(config.from_date, config.to_date)

    collector = KospiCollector(config=config, request_delay=config.request_delay)

    output_path = Path(config.output)
    meta_path = Path(config.metadata_output)
    existing = collector.load_existing(output_path) if not config.force_refresh else []

    # --- Incremental overlap applied to the REQUEST plan ----------------- #
    existing_latest = max((r.timestamp for r in existing), default="")
    effective_from = config.from_date
    if existing and not config.force_refresh and existing_latest:
        latest_dt = datetime.fromisoformat(existing_latest)
        overlap_start = latest_dt - timedelta(hours=config.overlap_hours)
        requested_from_dt = datetime.strptime(
            config.from_date, "%Y-%m-%d"
        ).replace(tzinfo=KST)
        to_dt_ = datetime.strptime(config.to_date, "%Y-%m-%d").replace(
            tzinfo=KST
        )
        effective_dt = max(requested_from_dt, overlap_start)
        if effective_dt.date() > to_dt_.date():
            # Existing data already extends beyond the requested range:
            # clamp to requested_to instead of misreporting NO_TRADING_DAYS.
            effective_dt = to_dt_
        effective_from = effective_dt.date().isoformat()

    # Count supported trading days in the effective range (fail-closed).
    from_dt = datetime.strptime(effective_from, "%Y-%m-%d").replace(tzinfo=KST)
    to_dt = datetime.strptime(config.to_date, "%Y-%m-%d").replace(tzinfo=KST)
    trading_days = sum(
        1
        for offset in range((to_dt.date() - from_dt.date()).days + 1)
        if is_trading_day(from_dt + timedelta(days=offset))
    )
    if trading_days == 0:
        if config.dry_run:
            return {"dryRun": True, "status": "NO_TRADING_DAYS"}
        raise NoTradingDaysError(
            "NO_TRADING_DAYS: requested range "
            f"{effective_from}..{config.to_date} contains no supported "
            "trading days; no output was fabricated."
        )

    requested_dates: List[str] = []
    fresh = collector.collect_range(
        effective_from, config.to_date, requested_dates=requested_dates
    )
    records = collector.merge_existing(existing, fresh)

    if config.dry_run:
        return {
            "dryRun": True,
            "requestedDates": requested_dates,
            "recordsReceived": collector.records_received,
            "recordsAccepted": collector.records_accepted,
            "recordsRejected": collector.records_rejected,
        }

    # Zero accepted records on a range that has trading days -> fail closed.
    # This holds regardless of any pre-existing data: a run that accepts
    # nothing must never silently rewrite "real" metadata with stale rows.
    if collector.records_accepted == 0 and trading_days > 0:
        raise KiwoomAPIError(
            "Collection failed closed: 0 records accepted across "
            f"{trading_days} trading day(s). No output was written "
            "(no sample substitution)."
        )

    collector.write_records(output_path, records)
    metadata = collector.build_metadata(
        config.from_date,
        config.to_date,
        records,
        overlap_hours=config.overlap_hours,
        existing_latest_timestamp=existing_latest,
        effective_from=effective_from,
    )
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_meta = meta_path.with_name(meta_path.name + ".tmp")
    try:
        tmp_meta.write_text(
            json.dumps(metadata.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_meta, meta_path)
    finally:
        if tmp_meta.exists():
            tmp_meta.unlink()

    return {
        "status": "OK",
        "records": len(records),
        "requestedDates": requested_dates,
        "effectiveFrom": effective_from,
        "requestsMade": collector.requests_made,
        "recordsReceived": collector.records_received,
        "recordsAccepted": collector.records_accepted,
        "recordsRejected": collector.records_rejected,
        "duplicatesRemoved": collector.duplicates_removed,
        "partialBuckets": collector.partial_buckets,
        "output": str(output_path),
        "metadata": str(meta_path),
        "warnings": collector.warnings,
    }


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser (exposed for CLI contract tests)."""
    parser = argparse.ArgumentParser(
        description="Collect KOSPI index hourly OHLC from the Kiwoom REST API"
    )
    parser.add_argument("--from", dest="from_date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="JSONL output path")
    parser.add_argument(
        "--metadata-output", default=DEFAULT_METADATA, help="Metadata JSON path"
    )
    parser.add_argument("--dry-run", action="store_true", help="Validate only")
    parser.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS)
    parser.add_argument("--request-delay", type=float, default=1.0)
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--overlap-hours", type=int, default=DEFAULT_OVERLAP_HOURS)
    parser.add_argument(
        "--tic-scope",
        default="60",
        choices=ALLOWED_TIC_SCOPES,
        help="Minute chart interval for ka20005 (1|3|5|10|15|30|45|60). "
        "Sent to the Kiwoom API as a JSON string (the live wire contract).",
    )
    return parser


def main() -> None:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args()

    config = CollectorConfig(
        from_date=args.from_date,
        to_date=args.to_date,
        output=args.output,
        metadata_output=args.metadata_output,
        dry_run=args.dry_run,
        max_requests=args.max_requests,
        request_delay=args.request_delay,
        force_refresh=args.force_refresh,
        overlap_hours=args.overlap_hours,
        tic_scope=args.tic_scope,
    )
    try:
        summary = run_collector(config)
    except (
        KiwoomAuthError,
        KiwoomRateLimitError,
        KiwoomAPIError,
        KiwoomPaginationError,
        NoTradingDaysError,
    ) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
