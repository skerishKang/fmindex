"""Typed models for the KOSPI hourly market collector.

This module defines the data contracts shared by the Kiwoom collector,
the MarketBridge, and the pipeline. Field names follow the external
camelCase JSON contract used across FMIndex (instrumentId, dataMode, ...).

Official API contract (confirmed from Kiwoom REST API docs / official
GitHub examples, https://github.com/Kiwoom-Securities/Kiwoom-REST-API):

- KOSPI index code: inds_cd = "001" (종합(KOSPI)), mrkt_tp = "0" (코스피)
- ka20005 업종분봉조회요청: POST /api/dostk/chart, table inds_min_pole_qry
- ka20006 업종일봉조회요청: POST /api/dostk/chart, table inds_dt_pole_qry
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

#: Provider identifier for the Kiwoom REST API (confirmed contract).
PROVIDER = "kiwoom"

#: KOSPI composite index code as defined by the official API docs.
KOSPI_INDS_CD = "001"

#: Market type for KOSPI as defined by the official API docs (mrkt_tp).
KOSPI_MRKT_TP = "0"

#: Symbol/label used across FMIndex for the KOSPI composite index.
KOSPI_SYMBOL = "KOSPI"

#: Asset type marker for an index series (never an individual stock).
ASSET_TYPE_INDEX = "index"

#: Data mode marker for records collected from the live Kiwoom API.
DATA_MODE_REAL = "real"


@dataclass
class Tick:
    """A single raw tick or intraday print from the Kiwoom API."""

    timestamp: str  # ISO 8601 with +09:00
    price: float
    volume: Optional[float] = None
    instrument_id: str = ""
    symbol: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Candle:
    """A raw OHLC candle preserving the original open/high/low/close."""

    timestamp: str  # ISO 8601 with +09:00
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float] = None
    instrument_id: str = ""
    symbol: str = ""
    is_partial: bool = False  # True when the bucket is shorter than the nominal period
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HourlyIndexRecord:
    """A validated 1-hour OHLC record for the KOSPI index.

    Only records that satisfy the full KOSPI instrument contract
    (provider=kiwoom, instrumentId=001/KOSPI, assetType=index,
    dataMode=real) may be emitted as real KOSPI data.
    """

    timestamp: str  # ISO 8601 with +09:00, top of the hour
    market: str
    instrument_id: str
    symbol: str
    asset_type: str
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float]
    change_rate: float
    source: str
    provider: str
    observed_at: str
    data_mode: str
    is_partial: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to the external camelCase JSON contract."""
        return {
            "timestamp": self.timestamp,
            "market": self.market,
            "instrumentId": self.instrument_id,
            "symbol": self.symbol,
            "assetType": self.asset_type,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "changeRate": self.change_rate,
            "source": self.source,
            "provider": self.provider,
            "observedAt": self.observed_at,
            "dataMode": self.data_mode,
            "isPartial": self.is_partial,
        }


@dataclass
class CollectionMetadata:
    """Collector run metadata. Never contains secrets or tokens."""

    provider: str
    instrument_id: str
    symbol: str
    asset_type: str
    requested_from: str
    requested_to: str
    effective_from: str
    effective_to: str
    requests_made: int
    records_received: int
    records_accepted: int
    records_rejected: int
    duplicates_removed: int
    partial_buckets: int
    latest_timestamp: str
    generated_at: str
    data_mode: str
    api_contract_version: str
    collector_version: str
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to the metadata JSON contract (camelCase keys)."""
        data = asdict(self)
        mapping = {
            "instrument_id": "instrumentId",
            "asset_type": "assetType",
            "requested_from": "requestedFrom",
            "requested_to": "requestedTo",
            "effective_from": "effectiveFrom",
            "effective_to": "effectiveTo",
            "requests_made": "requestsMade",
            "records_received": "recordsReceived",
            "records_accepted": "recordsAccepted",
            "records_rejected": "recordsRejected",
            "duplicates_removed": "duplicatesRemoved",
            "partial_buckets": "partialBuckets",
            "latest_timestamp": "latestTimestamp",
            "generated_at": "generatedAt",
            "data_mode": "dataMode",
            "api_contract_version": "apiContractVersion",
            "collector_version": "collectorVersion",
        }
        result: Dict[str, Any] = {}
        for key, value in data.items():
            result[mapping.get(key, key)] = value
        return result
