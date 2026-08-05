"""Market data bridge — reads 65stock output and normalizes to common format."""

from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

KST = timezone(timedelta(hours=9))

DEFAULT_65STOCK_DATA_ROOT = os.environ.get(
    "FMINDEX_65STOCK_DATA_ROOT",
    str(Path(__file__).resolve().parents[3] / "65stock" / "05_data" / "stocks"),
)
DEFAULT_MARKET = os.environ.get("FMINDEX_MARKET", "KOSPI")
DEFAULT_TIMEZONE = os.environ.get("FMINDEX_TIMEZONE", "Asia/Seoul")

# Identifiers that unambiguously refer to the KOSPI index (not an individual stock).
INDEX_MARKET_NAMES = (
    "KOSPI",
    "KS11",
    "KOSPI COMPOSITE",
    "KOSPI 200",
    "KOSPI200",
    "KOSPI INDEX",
    "코스피",
)


@dataclass
class MarketRecord:
    """Normalized market data record (1-hour OHLC bucket).

    Internal field names are snake_case. The external JSON contract
    (to_dict) uses camelCase keys (instrumentId, dataMode, ...).
    """

    timestamp: str  # ISO 8601 with +09:00
    market: str
    instrument_id: str
    symbol: str
    open: float
    high: float
    low: float
    close: float
    change_rate: float
    source: str
    observed_at: str
    data_mode: str  # "real" | "sample" | "unsupported"

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to the external camelCase JSON contract."""
        return {
            "timestamp": self.timestamp,
            "market": self.market,
            "instrumentId": self.instrument_id,
            "symbol": self.symbol,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "changeRate": self.change_rate,
            "source": self.source,
            "observedAt": self.observed_at,
            "dataMode": self.data_mode,
        }


@dataclass
class Tick:
    """Raw tick or minute-bar record from 65stock."""

    timestamp: str
    price: float
    volume: Optional[float] = None
    instrument_id: str = ""
    symbol: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Candle:
    """OHLC candle record preserving original high/low."""

    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float] = None
    instrument_id: str = ""
    symbol: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


class MarketBridge:
    """Bridge that reads 65stock data files and normalizes them.

    Supports JSON and CSV input. Does NOT modify source files.
    Validates that data belongs to the configured market index instrument
    and rejects individual stock records so they are never presented as
    the KOSPI index series.
    """

    def __init__(
        self,
        data_root: Optional[str] = None,
        market: str = DEFAULT_MARKET,
        instrument_id: str = "",
        symbol: str = "",
    ):
        self.data_root = Path(data_root or DEFAULT_65STOCK_DATA_ROOT)
        self.market = market
        self.instrument_id = instrument_id
        self.symbol = symbol
        self.rejected_non_index = 0

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

        ticks: List[Tick] = []
        candles: List[Candle] = []
        for filepath in sorted(self.data_root.rglob("*")):
            if filepath.suffix.lower() == ".json":
                result = self._parse_json_file(filepath)
                ticks.extend(result.get("ticks", []))
                candles.extend(result.get("candles", []))
            elif filepath.suffix.lower() == ".jsonl":
                result = self._parse_jsonl_file(filepath)
                ticks.extend(result.get("ticks", []))
                candles.extend(result.get("candles", []))
            elif filepath.suffix.lower() == ".csv":
                result = self._parse_csv_file(filepath)
                ticks.extend(result.get("ticks", []))
                candles.extend(result.get("candles", []))

        if not ticks and not candles:
            return []

        return self._aggregate_hourly(ticks, candles)

    def read_records_from_path(self, filepath: str) -> List[MarketRecord]:
        """Read records from a specific file path (for testing)."""
        path = Path(filepath)
        if not path.exists():
            raise FileNotFoundError(f"Data file not found: {path}")

        ticks: List[Tick] = []
        candles: List[Candle] = []
        if path.suffix.lower() == ".json":
            result = self._parse_json_file(path)
            ticks.extend(result.get("ticks", []))
            candles.extend(result.get("candles", []))
        elif path.suffix.lower() == ".jsonl":
            result = self._parse_jsonl_file(path)
            ticks.extend(result.get("ticks", []))
            candles.extend(result.get("candles", []))
        elif path.suffix.lower() == ".csv":
            result = self._parse_csv_file(path)
            ticks.extend(result.get("ticks", []))
            candles.extend(result.get("candles", []))

        return self._aggregate_hourly(ticks, candles)

    # ------------------------------------------------------------------ #
    # Parsing                                                             #
    # ------------------------------------------------------------------ #

    def _parse_json_file(self, filepath: Path) -> Dict[str, List]:
        """Parse a JSON file from 65stock into raw ticks and candles."""
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {"ticks": [], "candles": []}

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

        ticks: List[Tick] = []
        candles: List[Candle] = []
        for rec in records:
            if not isinstance(rec, dict):
                continue
            if self._is_candle_record(rec):
                candle = self._extract_candle(rec)
                if candle is not None:
                    candles.append(candle)
            else:
                tick = self._extract_tick(rec)
                if tick is not None:
                    ticks.append(tick)

        return {"ticks": ticks, "candles": candles}

    def _parse_jsonl_file(self, filepath: Path) -> Dict[str, List]:
        """Parse a JSONL file into raw ticks and candles."""
        ticks: List[Tick] = []
        candles: List[Candle] = []
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        if isinstance(rec, dict):
                            if self._is_candle_record(rec):
                                candle = self._extract_candle(rec)
                                if candle is not None:
                                    candles.append(candle)
                            else:
                                tick = self._extract_tick(rec)
                                if tick is not None:
                                    ticks.append(tick)
                    except json.JSONDecodeError:
                        continue
        except (UnicodeDecodeError, OSError):
            pass
        return {"ticks": ticks, "candles": candles}

    def _parse_csv_file(self, filepath: Path) -> Dict[str, List]:
        """Parse a CSV file from 65stock into raw ticks and candles."""
        ticks: List[Tick] = []
        candles: List[Candle] = []
        try:
            with open(filepath, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if self._is_candle_record(row):
                        candle = self._extract_candle(row)
                        if candle is not None:
                            candles.append(candle)
                    else:
                        tick = self._extract_tick(row)
                        if tick is not None:
                            ticks.append(tick)
        except (UnicodeDecodeError, csv.Error):
            pass
        return {"ticks": ticks, "candles": candles}

    @staticmethod
    def _is_candle_record(rec: Dict[str, Any]) -> bool:
        """Check if a record has OHLC candle fields."""
        candle_fields = {"open", "high", "low", "close"}
        return any(f in rec for f in candle_fields)

    def _extract_tick(self, rec: Dict[str, Any]) -> Optional[Tick]:
        """Extract a Tick from a record dict."""
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

        instrument_id = self._find_field(rec, ["instrumentId", "instrument_id", "code", "종목코드", "symbol"]) or ""
        symbol = self._find_field(rec, ["symbol", "종목명", "name", "shortName"]) or ""

        return Tick(
            timestamp=ts_str,
            price=price_val,
            volume=vol_val,
            instrument_id=instrument_id,
            symbol=symbol,
            raw=rec,
        )

    def _extract_candle(self, rec: Dict[str, Any]) -> Optional[Candle]:
        """Extract a Candle from a record dict with OHLC fields."""
        ts = self._find_field(rec, ["timestamp", "time", "date", "datetime", "ts", "일자", "시간"])
        if ts is None:
            return None

        ts_str = self._normalize_timestamp(str(ts))
        if ts_str is None:
            return None

        open_val = self._find_field(rec, ["open", "시가", "opening"])
        high_val = self._find_field(rec, ["high", "고가", "highPrice"])
        low_val = self._find_field(rec, ["low", "저가", "lowPrice"])
        close_val = self._find_field(rec, ["close", "종가", "current_price", "cur_prc"])

        if any(v is None for v in [open_val, high_val, low_val, close_val]):
            return None

        try:
            open_f = float(open_val)
            high_f = float(high_val)
            low_f = float(low_val)
            close_f = float(close_val)
        except (ValueError, TypeError):
            return None

        volume = self._find_field(rec, ["volume", "거래량"])
        vol_val = None
        if volume is not None:
            try:
                vol_val = float(volume)
            except (ValueError, TypeError):
                vol_val = None

        instrument_id = self._find_field(rec, ["instrumentId", "instrument_id", "code", "종목코드", "symbol"]) or ""
        symbol = self._find_field(rec, ["symbol", "종목명", "name", "shortName"]) or ""

        return Candle(
            timestamp=ts_str,
            open=open_f,
            high=high_f,
            low=low_f,
            close=close_f,
            volume=vol_val,
            instrument_id=instrument_id,
            symbol=symbol,
            raw=rec,
        )

    # ------------------------------------------------------------------ #
    # Aggregation                                                         #
    # ------------------------------------------------------------------ #

    def _aggregate_hourly(
        self, ticks: List[Tick], candles: List[Candle]
    ) -> List[MarketRecord]:
        """Aggregate raw ticks and candles into 1-hour OHLC buckets.

        Deduplicates by (instrument, timestamp) keeping the last record.
        Normalizes to Asia/Seoul and buckets by the hour.
        Only accepts records matching the configured market index instrument;
        individual stock records are rejected (never merged into one series).
        Candle OHLC is preserved: open = first candle open, high = max high,
        low = min low, close = last candle close.
        """
        if not ticks and not candles:
            return []

        # Deduplicate by (instrument, timestamp) — keep the LAST record.
        # Candles take precedence over ticks at the same key.
        merged: Dict[Tuple[str, str], Tuple[str, Any]] = {}
        for tick in ticks:
            key = (tick.instrument_id or tick.symbol, tick.timestamp)
            merged[key] = ("tick", tick)
        for candle in candles:
            key = (candle.instrument_id or candle.symbol, candle.timestamp)
            merged[key] = ("candle", candle)

        # Group by hour per instrument
        buckets: Dict[Tuple[str, str, str], List[Tuple[str, Any]]] = {}
        for (_, ts_str), (rec_type, item) in merged.items():
            hour_key = self._truncate_to_hour(ts_str)
            if hour_key is None:
                continue
            buckets.setdefault((item.instrument_id, item.symbol, hour_key), []).append(
                (rec_type, item)
            )

        self.rejected_non_index = 0
        records: List[MarketRecord] = []
        for (instrument_id, symbol, hour_key), recs in sorted(buckets.items()):
            if not recs:
                continue

            # Reject explicit non-index instruments (individual stocks) so
            # they are never presented as the KOSPI index series.
            first_raw = recs[0][1].raw
            if not self._is_index_identity(instrument_id, symbol, first_raw):
                self.rejected_non_index += 1
                continue

            candle_recs = [item for t, item in recs if t == "candle"]
            tick_recs = [item for t, item in recs if t == "tick"]

            if candle_recs:
                # Preserve original candle OHLC across the bucket.
                open_price = candle_recs[0].open
                high_price = max(c.high for c in candle_recs)
                low_price = min(c.low for c in candle_recs)
                close_price = candle_recs[-1].close
            elif tick_recs:
                # Aggregate ticks into OHLC.
                prices = [t.price for t in tick_recs]
                open_price = prices[0]
                close_price = prices[-1]
                high_price = max(prices)
                low_price = min(prices)
            else:
                continue

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
                    instrument_id=instrument_id,
                    symbol=symbol,
                    open=round(open_price, 2),
                    high=round(high_price, 2),
                    low=round(low_price, 2),
                    close=round(close_price, 2),
                    change_rate=change_rate,
                    source="kiwoom-65stock",
                    observed_at=observed_at,
                    data_mode="real" if self.data_root.exists() else "sample",
                )
            )

        return records

    def _is_index_identity(
        self,
        instrument_id: str,
        symbol: str,
        raw: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Whether a record identity refers to the configured market index.

        Records with no explicit identity are treated as belonging to the
        configured market series. Explicit stock identities (codes, names,
        or a stock asset type) are rejected so individual stocks are never
        presented as the KOSPI index.
        """
        if raw:
            asset = self._find_field(raw, ["assetType", "asset_type", "securityType"])
            if asset is not None:
                asset_l = str(asset).strip().lower()
                if asset_l in ("index", "지수", "kospi"):
                    return True
                if asset_l in ("stock", "equity", "share", "common_stock", "주식", "종목"):
                    return False

        identity = f"{instrument_id} {symbol}".strip().upper()
        if not identity:
            return True  # no identity → assume the configured market series

        if any(name.upper() in identity for name in INDEX_MARKET_NAMES):
            return True

        if self.instrument_id and self.instrument_id.upper() in identity:
            return True
        if self.symbol and self.symbol.upper() in identity:
            return True

        return False

    # ------------------------------------------------------------------ #
    # Helpers                                                             #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _find_field(rec: Dict[str, Any], candidates: List[str]) -> Any:
        """Find a field in a record by trying multiple candidate names."""
        for key in candidates:
            if key in rec:
                return rec[key]
            for rec_key in rec:
                if rec_key.lower() == key.lower():
                    return rec[rec_key]
        return None

    @staticmethod
    def _normalize_timestamp(ts_str: str) -> Optional[str]:
        """Normalize various timestamp formats to ISO 8601 with +09:00."""
        ts_str = ts_str.strip()

        try:
            dt = datetime.fromisoformat(ts_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=KST)
            else:
                dt = dt.astimezone(KST)
            return dt.isoformat()
        except ValueError:
            pass

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
            else:
                dt = dt.astimezone(KST)
            dt = dt.replace(minute=0, second=0, microsecond=0)
            return dt.isoformat()
        except ValueError:
            return None
