"""Time-based FM Index calculator — aggregates sentiment into hourly buckets."""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .llm.provider import SentimentResult

KST = timezone(timedelta(hours=9))
METHODOLOGY_VERSION = "fmindex-v1"

DIRECTION_SCORES = {
    "positive": 1.0,
    "neutral": 0.0,
    "negative": -1.0,
    "mixed": 0.0,
    "unrelated": None,  # excluded from aggregation
}


@dataclass
class HourlyFMIndex:
    """FM Index for a single hour bucket."""

    timestamp: str
    fmIndex: float
    positiveRatio: float
    negativeRatio: float
    neutralRatio: float
    postCount: int
    commentCount: int
    analyzedPostCount: int
    confidence: float
    methodologyVersion: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PostWithSentiment:
    """A post with its sentiment analysis result and timestamp."""

    timestamp: str  # ISO 8601 with +09:00
    sentiment: SentimentResult
    commentCount: int = 0


class FMIndexCalculator:
    """Calculates hourly FM Index from sentiment-analyzed posts.

    Groups posts by Asia/Seoul 1-hour buckets and computes:
    - fmIndex: (averageScore + 1) * 50, range 0-100
    - positiveRatio, negativeRatio, neutralRatio
    - postCount, commentCount, analyzedPostCount
    - confidence: weighted by sample size
    """

    def __init__(self, use_first_seen: bool = True):
        """Initialize calculator.

        Args:
            use_first_seen: If True, use firstSeenAt for bucketing (prevents future leakage).
                           If False, use publishedAt.
        """
        self.use_first_seen = use_first_seen

    def calculate_hourly(
        self, posts: List[PostWithSentiment]
    ) -> List[HourlyFMIndex]:
        """Calculate hourly FM Index from sentiment-analyzed posts.

        Args:
            posts: List of posts with sentiment results and timestamps.

        Returns:
            List of HourlyFMIndex sorted by timestamp.
        """
        if not posts:
            return []

        # Group by hour
        buckets: Dict[str, List[PostWithSentiment]] = {}
        for post in posts:
            hour_key = self._truncate_to_hour(post.timestamp)
            if hour_key is None:
                continue
            buckets.setdefault(hour_key, []).append(post)

        results: List[HourlyFMIndex] = []
        for hour_key in sorted(buckets.keys()):
            bucket_posts = buckets[hour_key]
            result = self._calculate_bucket(hour_key, bucket_posts)
            if result is not None:
                results.append(result)

        return results

    def _calculate_bucket(
        self, hour_key: str, posts: List[PostWithSentiment]
    ) -> Optional[HourlyFMIndex]:
        """Calculate FM Index for a single hour bucket."""
        # Filter out unrelated posts
        scored_posts = []
        for post in posts:
            score = DIRECTION_SCORES.get(post.sentiment.direction)
            if score is not None:
                scored_posts.append((post, score))

        total_posts = len(posts)
        analyzed_posts = len(scored_posts)
        total_comments = sum(p.commentCount for p in posts)

        if analyzed_posts == 0:
            return HourlyFMIndex(
                timestamp=hour_key,
                fmIndex=50.0,
                positiveRatio=0.0,
                negativeRatio=0.0,
                neutralRatio=0.0,
                postCount=total_posts,
                commentCount=total_comments,
                analyzedPostCount=0,
                confidence=0.0,
                methodologyVersion=METHODOLOGY_VERSION,
            )

        scores = [s for _, s in scored_posts]
        avg_score = sum(scores) / len(scores)

        # fmIndex = (averageScore + 1) * 50, clamped to 0-100
        fm_index = round(max(0.0, min(100.0, (avg_score + 1.0) * 50.0)), 2)

        # Ratios
        pos_count = sum(1 for p in posts if p.sentiment.direction == "positive")
        neg_count = sum(1 for p in posts if p.sentiment.direction == "negative")
        neu_count = sum(1 for p in posts if p.sentiment.direction in ("neutral", "mixed"))

        pos_ratio = round(pos_count / analyzed_posts, 4) if analyzed_posts else 0.0
        neg_ratio = round(neg_count / analyzed_posts, 4) if analyzed_posts else 0.0
        neu_ratio = round(neu_count / analyzed_posts, 4) if analyzed_posts else 0.0

        # Confidence: based on sample size and average sentiment confidence
        avg_conf = sum(p.sentiment.confidence for p, _ in scored_posts) / len(scored_posts)
        sample_factor = min(1.0, analyzed_posts / 10.0)
        confidence = round(avg_conf * sample_factor, 4)

        return HourlyFMIndex(
            timestamp=hour_key,
            fmIndex=fm_index,
            positiveRatio=pos_ratio,
            negativeRatio=neg_ratio,
            neutralRatio=neu_ratio,
            postCount=total_posts,
            commentCount=total_comments,
            analyzedPostCount=analyzed_posts,
            confidence=confidence,
            methodologyVersion=METHODOLOGY_VERSION,
        )

    @staticmethod
    def _truncate_to_hour(ts_str: str) -> Optional[str]:
        """Truncate timestamp to the hour."""
        try:
            dt = datetime.fromisoformat(ts_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=KST)
            dt = dt.replace(minute=0, second=0, microsecond=0)
            return dt.isoformat()
        except ValueError:
            return None
