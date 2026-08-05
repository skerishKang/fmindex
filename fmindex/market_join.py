"""Market-sentiment joiner — combines market data with FM Index on time axis."""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

from .market.bridge import MarketRecord
from .fmindex_calc import HourlyFMIndex


@dataclass
class JoinedRecord:
    """A single time bucket with both market and sentiment data."""

    timestamp: str
    market: str
    fmIndex: float
    marketNormalized: float
    marketChangeRate: float
    postCount: int
    confidence: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class OvernightEvaluation:
    """Overnight sentiment vs next-day market result."""

    tradeDate: str
    overnight: Dict[str, Any]
    kospi: Dict[str, Any]
    matches: Dict[str, bool]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class MarketSentimentJoiner:
    """Joins market data and FM Index on the same hourly time axis.

    Normalizes market data to a base of 100 for the selected period.
    """

    def join(
        self,
        market_records: List[MarketRecord],
        fm_indices: List[HourlyFMIndex],
        market: str = "KOSPI",
    ) -> List[JoinedRecord]:
        """Join market and sentiment data by timestamp.

        Args:
            market_records: Hourly market data.
            fm_indices: Hourly FM Index data.
            market: Market identifier.

        Returns:
            List of JoinedRecord sorted by timestamp.
        """
        if not market_records:
            return []

        # Build lookup for FM Index by timestamp
        fm_lookup: Dict[str, HourlyFMIndex] = {}
        for fm in fm_indices:
            fm_lookup[fm.timestamp] = fm

        # Normalize market: first close = 100
        base_close = market_records[0].close
        if base_close == 0:
            base_close = 1.0

        results: List[JoinedRecord] = []
        for rec in market_records:
            normalized = round((rec.close / base_close) * 100.0, 2)

            fm = fm_lookup.get(rec.timestamp)
            fm_index = fm.fmIndex if fm else 50.0
            post_count = fm.postCount if fm else 0
            confidence = fm.confidence if fm else 0.0

            results.append(
                JoinedRecord(
                    timestamp=rec.timestamp,
                    market=market,
                    fmIndex=fm_index,
                    marketNormalized=normalized,
                    marketChangeRate=rec.changeRate,
                    postCount=post_count,
                    confidence=confidence,
                )
            )

        return results

    def evaluate_overnight(
        self,
        overnight_indices: List[HourlyFMIndex],
        next_day_market: List[MarketRecord],
        trade_date: str = "",
    ) -> Optional[OvernightEvaluation]:
        """Evaluate overnight sentiment vs next-day market results.

        Args:
            overnight_indices: FM Index data from overnight hours.
            next_day_market: Market data for the next trading day.
            trade_date: The trade date being evaluated.

        Returns:
            OvernightEvaluation or None if insufficient data.
        """
        if not overnight_indices or not next_day_market:
            return None

        # Overnight summary
        fm_values = [fm.fmIndex for fm in overnight_indices]
        avg_fm = round(sum(fm_values) / len(fm_values), 2) if fm_values else 50.0
        min_fm = round(min(fm_values), 2) if fm_values else 50.0
        last_fm = round(fm_values[-1], 2) if fm_values else 50.0

        if avg_fm < 35:
            signal = "negative"
        elif avg_fm > 65:
            signal = "positive"
        else:
            signal = "neutral"

        # Next-day market metrics
        if len(next_day_market) < 2:
            return None

        first = next_day_market[0]
        first_hour = next_day_market[1] if len(next_day_market) > 1 else first
        last = next_day_market[-1]
        low_rec = min(next_day_market, key=lambda r: r.low)

        base = first.open if first.open != 0 else 1.0

        open_change = round(((first.close - base) / base) * 100, 2)
        first_hour_change = round(
            ((first_hour.close - first.close) / first.close) * 100, 2
        ) if first.close != 0 else 0.0
        low_change = round(((low_rec.low - base) / base) * 100, 2)
        close_change = round(((last.close - base) / base) * 100, 2)

        # Direction matching
        def _matches(change: float, sig: str) -> bool:
            if sig == "negative":
                return change < 0
            elif sig == "positive":
                return change > 0
            else:
                return abs(change) < 0.5

        return OvernightEvaluation(
            tradeDate=trade_date,
            overnight={
                "averageFmIndex": avg_fm,
                "minimumFmIndex": min_fm,
                "lastFmIndex": last_fm,
                "signal": signal,
            },
            kospi={
                "openChangeRate": open_change,
                "firstHourChangeRate": first_hour_change,
                "lowChangeRate": low_change,
                "closeChangeRate": close_change,
            },
            matches={
                "open": _matches(open_change, signal),
                "firstHour": _matches(first_hour_change, signal),
                "close": _matches(close_change, signal),
            },
        )
