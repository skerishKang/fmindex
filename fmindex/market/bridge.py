"""Market data bridge — reads 65stock output and normalizes to common format."""

from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

KST = timezone(timedelta(hours=9))

DEFAULT_65STOCK_DATA_ROOT = os.environ.get(
    "FMINDEX_65STOCK_DATA_ROOT",
    str(Path(__file__).resolve().parents[3] / "65stock" / "05_data" / "stocks"),
)
DEFAULT_MARKET = os.environ.get("FMINDEX_MARKET", "KOSPI")
DEFAULT_TIMEZONE = os.environ.get("FMINDEX_TIMEZONE", "Asia/Seoul")


@dataclass
class MarketRecord:
    """Normalized market data record (1-hour OHLC bucket)."""

    timestamp: str  # ISO 8601 with +09:00
    market: str
    open: float
    high: float
    low: float
    close: float
    changeRate: float
    source: str
    observedAt: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RawTick:
    """Raw tick or minute-bar record from 65stock."""

    timestamp: str
    price: float
    volume: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict)


class MarketBridge:
    """Bridge that reads 65stock data files and normalizes them.

    Supports JSON and CSV input. Does NOT modify source files.
    """

    def __init__(
        self,
        data_root: Optional[str] = None,
        market: str = DEFAULT_MARKET,
    ):
        self.data_root = Path(data_root or DEFAULT_65STOCK_DATA_ROOT)
        self.market = market

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #

    def read_records(self) -> List[MarketRecord]:
        """Read all market records from the data root.

        Auto-detects JSON and CSV files. Returns normalized records.
        Raises FileNotFoundError if data root does not exist.
        """
        if not self.data_root.exists():
            raise FileNotFoundError(
                f"65stock data root not found: {self.data_root}\n"
                f"Set FMINDEX_65STOCK_DATA_ROOT to the correct path."
            )

        raw_ticks: List[RawTick] = []
        for filepath in sorted(self.data_root.rglob("*")):
            if filepath.suffix.lower() == ".json":
                raw_ticks.extend(self._parse_json_file(filepath))
            elif filepath.suffix.lower() == ".jsonl":
                raw_ticks.extend(self._parse_jsonl_file(filepath))
            elif filepath.suffix.lower() == ".csv":
                raw_ticks.extend(self._parse_csv_file(filepath))

        if not raw_ticks:
            return []

        return self._aggregate_hourly(raw_ticks)

    def read_records_from_path(self, filepath: str) -> List[MarketRecord]:
        """Read records from a specific file path (for testing)."""
        path = Path(filepath)
        if not path.exists():
            raise FileNotFoundError(f"Data file not found: {path}")

        raw_ticks: List[RawTick] = []
        if path.suffix.lower() == ".json":
            raw_ticks = self._parse_json_file(path)
        elif path.suffix.lower() == ".jsonl":
            raw_ticks = self._parse_jsonl_file(path)
        elif path.suffix.lower() == ".csv":
            raw_ticks = self._parse_csv_file(path)

        return self._aggregate_hourly(raw_ticks)

    # ------------------------------------------------------------------ #
    # Parsing                                                             #
    # ------------------------------------------------------------------ #

    def _parse_json_file(self, filepath: Path) -> List[RawTick]:
        """Parse a JSON file from 65stock into raw ticks.

        Handles various 65stock output formats:
        - List of objects with price/timestamp fields
        - Dict with 'data' key containing list
        - Dict with 'candles' key containing list
        """
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return []

        records: List[Any] = []
        if isinstance(content, list):
            records = content
        elif isinstance(content, dict):
            for key in ("data", "candles", "ticks", "prices", "records"):
                if key in content and isinstance(content[key], list):
                    records = content[key]
                    break
            if not records:
                records = [content]

        ticks: List[RawTick] = []
        for rec in records:
            if not isinstance(rec, dict):
                continue
            tick = self._extract_tick(rec)
            if tick is not None:
                ticks.append(tick)

        return ticks

    def _parse_csv_file(self, filepath: Path) -> List[RawTick]:
        """Parse a CSV file from 65stock into raw ticks."""
        ticks: List[RawTick] = []
        try:
            with open(filepath, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    tick = self._extract_tick(row)
                    if tick is not None:
                        ticks.append(tick)
        except (UnicodeDecodeError, csv.Error):
            pass
        return ticks

    def _parse_jsonl_file(self, filepath: Path) -> List[RawTick]:
        """Parse a JSONL file (one JSON object per line) into raw ticks.

        Handles illinoisK-style candle data with fields like:
        ts, cur_prc, flu_rt, high, low, open, vol
        """
        ticks: List[RawTick] = []
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        if isinstance(rec, dict):
                            tick = self._extract_tick(rec)
                            if tick is not None:
                                ticks.append(tick)
                    except json.JSONDecodeError:
                        continue
        except (UnicodeDecodeError, OSError):
            pass
        return ticks

    def _extract_tick(self, rec: Dict[str, Any]) -> Optional[RawTick]:
        """Extract a RawTick from a record dict.

        Handles various field naming conventions from 65stock and illinoisK:
        - 65stock: timestamp, current_price, change_rate
        - illinoisK: ts, cur_prc, flu_rt, open, high, low
        """
        ts = self._find_field(rec, ["timestamp", "time", "date", "datetime", "ts", "일자", "시간"])
        price = self._find_field(rec, [
            "price", "close", "현재가", "종가", "current_price", "cur_prc",
        ])

        if ts is None or price is None:
            return None

        ts_str = self._normalize_timestamp(str(ts))
        if ts_str is None:
            return None

        try:
            price_val = float(price)
        except (ValueError, TypeError):
            return None

        volume = self._find_field(rec, ["volume", "거래량"])
        vol_val = None
        if volume is not None:
            try:
                vol_val = float(volume)
            except (ValueError, TypeError):
                vol_val = None

        return RawTick(
            timestamp=ts_str,
            price=price_val,
            volume=vol_val,
            raw=rec,
        )

    # ------------------------------------------------------------------ #
    # Aggregation                                                         #
    # ------------------------------------------------------------------ #

    def _aggregate_hourly(self, ticks: List[RawTick]) -> List[MarketRecord]:
        """Aggregate raw ticks into 1-hour OHLC buckets.

        Deduplicates by timestamp. Normalizes to Asia/Seoul timezone.
        """
        if not ticks:
            return []

        # Sort by timestamp
        ticks.sort(key=lambda t: t.timestamp)

        # Deduplicate by timestamp (keep last)
        seen: Dict[str, RawTick] = {}
        for tick in ticks:
            seen[tick.timestamp] = tick
        ticks = list(seen.values())
        ticks.sort(key=lambda t: t.timestamp)

        # Group by hour
        buckets: Dict[str, List[RawTick]] = {}
        for tick in ticks:
            hour_key = self._truncate_to_hour(tick.timestamp)
            if hour_key is None:
                continue
            buckets.setdefault(hour_key, []).append(tick)

        records: List[MarketRecord] = []
        for hour_key in sorted(buckets.keys()):
            bucket_ticks = buckets[hour_key]
            if not bucket_ticks:
                continue

            prices = [t.price for t in bucket_ticks]
            open_price = prices[0]
            close_price = prices[-1]
            high_price = max(prices)
            low_price = min(prices)

            change_rate = 0.0
            if open_price != 0:
                change_rate = round(
                    ((close_price - open_price) / open_price) * 100, 4
                )

            observed_at = datetime.now(KST).isoformat()

            records.append(
                MarketRecord(
                    timestamp=hour_key,
                    market=self.market,
                    open=round(open_price, 2),
                    high=round(high_price, 2),
                    low=round(low_price, 2),
                    close=round(close_price, 2),
                    changeRate=change_rate,
                    source="kiwoom-65stock",
                    observedAt=observed_at,
                )
            )

        return records

    # ------------------------------------------------------------------ #
    # Helpers                                                             #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _find_field(rec: Dict[str, Any], candidates: List[str]) -> Any:
        """Find a field in a record by trying multiple candidate names."""
        for key in candidates:
            if key in rec:
                return rec[key]
            # Case-insensitive match
            for rec_key in rec:
                if rec_key.lower() == key.lower():
                    return rec[rec_key]
        return None

    @staticmethod
    def _normalize_timestamp(ts_str: str) -> Optional[str]:
        """Normalize various timestamp formats to ISO 8601 with +09:00."""
        ts_str = ts_str.strip()

        # Already ISO with timezone
        try:
            dt = datetime.fromisoformat(ts_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=KST)
            return dt.isoformat()
        except ValueError:
            pass

        # Common formats
        formats = [
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d %H:%M",
            "%Y%m%d %H%M%S",
            "%Y%m%d%H%M%S",
            "%Y-%m-%d",
        ]
        for fmt in formats:
            try:
                dt = datetime.strptime(ts_str, fmt)
                dt = dt.replace(tzinfo=KST)
                return dt.isoformat()
            except ValueError:
                continue

        return None

    @staticmethod
    def _truncate_to_hour(ts_str: str) -> Optional[str]:
        """Truncate a timestamp to the hour (e.g., 10:35 -> 10:00)."""
        try:
            dt = datetime.fromisoformat(ts_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=KST)
            dt = dt.replace(minute=0, second=0, microsecond=0)
            return dt.isoformat()
        except ValueError:
            return None
