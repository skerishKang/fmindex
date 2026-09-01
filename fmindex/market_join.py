"""Market-sentiment joiner — combines market data with FM Index on time axis."""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

from .market.bridge import MarketRecord
from .fmindex_calc import HourlyFMIndex


@dataclass
class JoinedRecord:
    """A single time bucket with both market and sentiment data.

    Internal field names are snake_case. The external JSON contract
    (to_dict) uses camelCase keys (instrumentId, hasSentiment, dataMode).
    """

    timestamp: str
    market: str
    instrument_id: str
    symbol: str
    fmIndex: Optional[float]
    has_sentiment: bool
    marketNormalized: float
    marketChangeRate: float
    postCount: int
    confidence: float
    data_mode: str
    commentCount: int = 0
    analyzedPostCount: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to the external camelCase JSON contract."""
        return {
            "timestamp": self.timestamp,
            "market": self.market,
            "instrumentId": self.instrument_id,
            "symbol": self.symbol,
            "fmIndex": self.fmIndex,
            "hasSentiment": self.has_sentiment,
            "marketNormalized": self.marketNormalized,
            "marketChangeRate": self.marketChangeRate,
            "postCount": self.postCount,
            "confidence": self.confidence,
            "dataMode": self.data_mode,
            "commentCount": self.commentCount,
            "analyzedPostCount": self.analyzedPostCount,
        }


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
            if fm is None:
                # Case C: No FM bucket exists for this timestamp.
                fm_index = None
                post_count = 0
                confidence = 0.0
                has_sentiment = False
                comment_count = 0
                analyzed_post_count = 0
            elif fm.has_sentiment:
                # Case A: FM bucket exists and has analyzable sentiment.
                fm_index = fm.fmIndex
                post_count = fm.postCount
                confidence = fm.confidence
                has_sentiment = True
                comment_count = fm.commentCount
                analyzed_post_count = fm.analyzedPostCount
            else:
                # Case B: FM bucket exists but no analyzable sentiment.
                # Preserve postCount (and metadata) — postCount=0 here means
                # the bucket truly had zero posts, not that sentiment is missing.
                fm_index = None
                post_count = fm.postCount
                confidence = 0.0
                has_sentiment = False
                comment_count = fm.commentCount
                analyzed_post_count = fm.analyzedPostCount

            results.append(
                JoinedRecord(
                    timestamp=rec.timestamp,
                    market=market,
                    instrument_id=rec.instrument_id,
                    symbol=rec.symbol,
                    fmIndex=fm_index,
                    has_sentiment=has_sentiment,
                    marketNormalized=normalized,
                    marketChangeRate=rec.change_rate,
                    postCount=post_count,
                    confidence=confidence,
                    data_mode=rec.data_mode,
                    commentCount=comment_count,
                    analyzedPostCount=analyzed_post_count,
                )
            )

        return results

    def evaluate_overnight(
        self,
        overnight_indices: List[HourlyFMIndex],
        next_day_market: List[MarketRecord],
        trade_date: str = "",
    ) -> OvernightEvaluation:
        """Evaluate overnight sentiment vs next-day market results.

        Since real session data is not yet available in this slice,
        returns an unavailable status rather than computing inaccurate
        overnight metrics from non-session data.
        """
        return OvernightEvaluation(
            tradeDate=trade_date,
            overnight={
                "status": "unavailable",
                "reason": "real_session_data_not_available",
            },
            kospi={},
            matches={},
        )
