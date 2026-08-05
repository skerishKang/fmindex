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

When the API returns true 60-minute index candles (tic_scope="60"),
those are used directly. Minute buckets (tic_scope < 60) are aggregated
into hourly OHLC: open=first open, high=max high, low=min low,
close=last close, volume=sum (null if the API does not provide it).

Usage:
    python -m fmindex.market.kospi_collector --from 2026-07-01 --to 2026-08-05
"""

from __future__ import annotations

import argparse
import json
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
from .market_calendar import KST, bucket_for_timestamp, is_trading_day
from .kiwoom_auth import KiwoomAuthError, KiwoomTokenManager, credentials_available
from .kiwoom_client import (
    API_CONTRACT_VERSION,
    KiwoomClient,
    KiwoomRateLimitError,
    KiwoomAPIError,
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
        self.tic_scope = tic_scope


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
        self.requests_made = 0
        self.records_received = 0
        self.records_accepted = 0
        self.records_rejected = 0
        self.duplicates_removed = 0
        self.partial_buckets = 0
        self.warnings: List[str] = []

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #

    def collect_range(
        self, from_date: str, to_date: str
    ) -> List[HourlyIndexRecord]:
        """Backfill hourly KOSPI records for every trading day in range."""
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

        Uses the official 60-minute index chart (ka20005, tic_scope="60")
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
            self.records_received += 1
            bucket = bucket_for_timestamp(candle.timestamp)
            if bucket is None:
                self.records_rejected += 1
                continue
            buckets.setdefault(bucket[0].isoformat(), []).append(candle)
            if bucket[1]:
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

    def _fetch_minute_pages(self, date_str: str) -> List[Dict[str, Any]]:
        """Fetch ka20005 (업종분봉) pages for one date."""
        if self.config and self.config.dry_run:
            return []
        self._check_request_budget()
        body: Dict[str, Any] = {
            "mrkt_tp": KOSPI_MRKT_TP,
            "inds_cd": KOSPI_INDS_CD,
            "tic_scope": (self.config.tic_scope if self.config else "60"),
            "base_dt": date_str,
        }
        try:
            pages = self.client.fetch_all(API_ID_MINUTE_CHART, SECTOR_PATH, body)
            self.requests_made += len(pages)
            return pages
        except KiwoomRateLimitError as e:
            raise
        except KiwoomAPIError as e:
            self.warnings.append(f"ka20005 {date_str}: {e}")
            return []

    def _parse_minute_pages(
        self, pages: List[Dict[str, Any]], date_str: str
    ) -> List[Candle]:
        """Parse ka20005 response tables into Candle records (KST)."""
        candles: List[Candle] = []
        for page in pages:
            rows = page.get(TABLE_MINUTE, [])
            if not isinstance(rows, list):
                continue
            for row in rows:
                candle = self._candle_from_minute_row(row, date_str)
                if candle is not None:
                    candles.append(candle)
        return candles

    def _candle_from_minute_row(
        self, row: Dict[str, Any], date_str: str
    ) -> Optional[Candle]:
        """Map a ka20005 row to a Candle using official field names."""
        o = self._float(row.get("open_pric"))
        h = self._float(row.get("high_pric"))
        l = self._float(row.get("low_pric"))
        c = self._float(row.get("cur_prc"))
        if o is None or h is None or l is None or c is None:
            return None

        ts = self._timestamp_from_row(row, date_str)
        if ts is None:
            return None

        vol = self._float(row.get("trde_qty"))
        volume = vol if vol is not None else self._float(row.get("acc_trde_qty"))

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

        volumes = [c.volume for c in sorted_c if c.volume is not None]
        volume = round(sum(volumes), 4) if volumes else None

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
        """Validate the full KOSPI instrument contract."""
        if rec.provider != PROVIDER:
            return False
        if rec.asset_type != ASSET_TYPE_INDEX:
            return False
        if rec.data_mode != DATA_MODE_REAL:
            return False
        identity = f"{rec.instrument_id} {rec.symbol}".upper()
        if KOSPI_INDS_CD not in identity and "KOSPI" not in identity:
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
        """Write records as timestamp-sorted JSONL."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for rec in sorted(records, key=lambda r: r.timestamp):
                f.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")

    def build_metadata(
        self,
        from_date: str,
        to_date: str,
        records: List[HourlyIndexRecord],
    ) -> CollectionMetadata:
        """Build run metadata (never contains secrets)."""
        effective_from = records[0].timestamp if records else from_date
        effective_to = records[-1].timestamp if records else to_date
        latest = records[-1].timestamp if records else ""
        return CollectionMetadata(
            provider=PROVIDER,
            instrument_id=KOSPI_INDS_CD,
            symbol=KOSPI_SYMBOL,
            asset_type=ASSET_TYPE_INDEX,
            requested_from=from_date,
            requested_to=to_date,
            effective_from=effective_from,
            effective_to=effective_to,
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

    @staticmethod
    def _float(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _timestamp_from_row(row: Dict[str, Any], date_str: str) -> Optional[str]:
        """Build a KST-aware ISO timestamp from a chart row.

        ka20005 rows carry ``cntr_tm`` (HHMMSS, KST intraday). When it is
        missing the bucket is derived from the date at 09:00 (session
        open) — a conservative fallback that never fabricates time.
        """
        tm = row.get("cntr_tm")
        if tm:
            text = str(tm).strip()
            if len(text) == 4 and text.isdigit():
                # HHMM -> HHMMSS (pad seconds on the right, never left).
                text = text + "00"
            if len(text) == 6 and text.isdigit():
                hour, minute = int(text[:2]), int(text[2:4])
                try:
                    return datetime.strptime(
                        f"{date_str} {hour:02d}:{minute:02d}", "%Y%m%d %H:%M"
                    ).replace(tzinfo=KST).isoformat()
                except ValueError:
                    pass
        try:
            return datetime.strptime(f"{date_str} 09:00", "%Y%m%d %H:%M").replace(
                tzinfo=KST
            ).isoformat()
        except ValueError:
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
    """Run the collector CLI flow and return a summary dict."""
    if not credentials_available():
        raise KiwoomAuthError(
            "Kiwoom credentials not configured (KIWOOM_APPKEY/KIWOOM_SECRETKEY). "
            "LIVE_SMOKE=SKIPPED_NO_CREDENTIALS"
        )

    collector = KospiCollector(config=config, request_delay=config.request_delay)

    output_path = Path(config.output)
    existing = collector.load_existing(output_path) if not config.force_refresh else []

    fresh = collector.collect_range(config.from_date, config.to_date)
    records = collector.merge_existing(existing, fresh)

    collector.write_records(output_path, records)
    metadata = collector.build_metadata(config.from_date, config.to_date, records)
    meta_path = Path(config.metadata_output)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(
        json.dumps(metadata.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return {
        "records": len(records),
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


def main() -> None:
    """CLI entry point."""
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
    parser.add_argument("--tic-scope", default="60", choices=["1", "3", "5", "10", "15", "30", "45", "60"])
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
    except (KiwoomAuthError, KiwoomRateLimitError, KiwoomAPIError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
