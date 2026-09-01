"""LLM sentiment analysis provider interface with deterministic mock."""

from __future__ import annotations

import hashlib
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

METHODOLOGY_VERSION = "sentiment-v1"


@dataclass
class SentimentResult:
    """Result of sentiment analysis for a single post."""

    direction: str  # positive | negative | neutral | mixed | unrelated
    score: float
    positiveProbability: float
    negativeProbability: float
    neutralProbability: float
    sarcasm: bool
    fearGreed: int
    marketTargets: List[str]
    confidence: float
    model: str
    methodologyVersion: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class LLMProvider(ABC):
    """Abstract base class for LLM sentiment analysis providers."""

    @abstractmethod
    def analyze(self, title: str, body: str, comments: List[str]) -> SentimentResult:
        """Analyze sentiment of a post with title, body, and comments."""
        ...

    def analyze_batch(
        self, posts: List[Dict[str, Any]]
    ) -> List[SentimentResult]:
        """Analyze a batch of posts."""
        results = []
        for post in posts:
            title = post.get("title", "")
            body = post.get("body", "")
            comments = [c.get("body", "") if isinstance(c, dict) else str(c) for c in post.get("comments", [])]
            results.append(self.analyze(title, body, comments))
        return results


class MockLLMProvider(LLMProvider):
    """Deterministic mock LLM provider for offline testing.

    Uses keyword matching and hash-based scoring to produce
    consistent, reproducible sentiment results without any
    network calls.
    """

    POSITIVE_KEYWORDS = [
        "상승", "급등", "호재", "매수", "bull", "long", "상한가", "갭상승",
        "돌파", "반등", "회복", "긍정", "좋", "기대", "수익", "익절",
        "보유", "추가매수", "신고가", "러시", "폭등", "급반등",
    ]

    NEGATIVE_KEYWORDS = [
        "하락", "급락", "악재", "매도", "bear", "short", "하한가", "갭하락",
        "손절", "손실", "부정", "나쁘", "위험", "폭락", "버림", "탈출",
        "붕괴", "급락", "공포", "패닉", "출렁", "약세", "조정",
    ]

    NEUTRAL_KEYWORDS = [
        "관망", "횡보", "박스권", "지지", "저항", "보합", "보통",
        "neutral", "횡보세", "지루",
    ]

    def __init__(self, model_name: str = "mock-deterministic-v1"):
        self.model_name = model_name

    def analyze(self, title: str, body: str, comments: List[str]) -> SentimentResult:
        full_text = f"{title} {body} {' '.join(comments)}".lower()
        combined = f"{title} {body} {' '.join(comments)}"

        pos_count = sum(1 for kw in self.POSITIVE_KEYWORDS if kw.lower() in full_text)
        neg_count = sum(1 for kw in self.NEGATIVE_KEYWORDS if kw.lower() in full_text)
        neu_count = sum(1 for kw in self.NEUTRAL_KEYWORDS if kw.lower() in full_text)

        total = pos_count + neg_count + neu_count
        if total == 0:
            direction = "unrelated"
            pos_prob = neg_prob = neu_prob = 0.33
            score = 0.0
            confidence = 0.3
        else:
            pos_prob = pos_count / total
            neg_prob = neg_count / total
            neu_prob = neu_count / total

            if pos_count > neg_count and pos_count > neu_count:
                direction = "positive"
                score = pos_prob
            elif neg_count > pos_count and neg_count > neu_count:
                direction = "negative"
                score = -neg_prob
            elif pos_count > 0 and neg_count > 0:
                direction = "mixed"
                score = (pos_count - neg_count) / total
            else:
                direction = "neutral"
                score = 0.0

            confidence = min(0.5 + (total / 10.0), 0.95)

            # Hash-based deterministic score refinement (only for related content)
            text_hash = int(hashlib.md5(combined.encode("utf-8")).hexdigest()[:8], 16)
            jitter = ((text_hash % 100) / 1000.0) - 0.05
            score = round(max(-1.0, min(1.0, score + jitter)), 4)

        fear_greed = int(round((score + 1.0) * 50))
        fear_greed = max(0, min(100, fear_greed))

        sarcasm = False
        if direction == "positive" and neg_count > 0:
            sarcasm = (text_hash % 10) < 2

        market_targets = self._detect_market_targets(full_text)

        return SentimentResult(
            direction=direction,
            score=score,
            positiveProbability=round(pos_prob, 4),
            negativeProbability=round(neg_prob, 4),
            neutralProbability=round(neu_prob, 4),
            sarcasm=sarcasm,
            fearGreed=fear_greed,
            marketTargets=market_targets,
            confidence=round(confidence, 4),
            model=self.model_name,
            methodologyVersion=METHODOLOGY_VERSION,
        )

    @staticmethod
    def _detect_market_targets(text: str) -> List[str]:
        targets = []
        if any(kw in text for kw in ["코스피", "kospi", "kospidaq"]):
            targets.append("KOSPI")
        if any(kw in text for kw in ["코스닥", "kosdaq"]):
            targets.append("KOSDAQ")
        if any(kw in text for kw in ["나스닥", "nasdaq", "nas"]):
            targets.append("NASDAQ")
        if any(kw in text for kw in ["비트코인", "bitcoin", "btc", "crypto"]):
            targets.append("CRYPTO")
        return targets if targets else ["KOSPI"]


def create_provider() -> LLMProvider:
    """Factory: create LLM provider based on environment variables.

    If FMINDEX_LLM_PROVIDER is 'mock' or unset, returns MockLLMProvider.
    Any other provider name raises NotImplementedError until implemented.
    """
    provider = os.environ.get("FMINDEX_LLM_PROVIDER", "mock")
    model = os.environ.get("FMINDEX_LLM_MODEL", "mock-deterministic-v1")

    if provider == "mock" or not provider:
        return MockLLMProvider(model_name=model)

    raise NotImplementedError(
        f"LLM provider '{provider}' is not yet implemented. "
        f"Set FMINDEX_LLM_PROVIDER=mock to use the deterministic mock provider."
    )
