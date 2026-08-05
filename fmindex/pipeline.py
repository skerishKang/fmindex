"""FMIndex pipeline — orchestrates the full data flow in a single command.

Usage:
    python -m fmindex.pipeline --once
    python -m fmindex.pipeline --once --serve
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .market.bridge import MarketBridge, MarketRecord
from .fmkorea.parser import FMKoreaParser, ParsedPost, Comment
from .llm.provider import LLMProvider, create_provider
from .fmindex_calc import FMIndexCalculator, PostWithSentiment, METHODOLOGY_VERSION
from .market_join import MarketSentimentJoiner
from .dashboard.server import write_dashboard_files, serve_dashboard

KST = timezone(timedelta(hours=9))

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "fmkorea"
OUTPUT_DIR = Path(__file__).resolve().parents[1] / "output"


def run_pipeline_once(
    market_data_path: Optional[str] = None,
    market_source: str = "auto",
    fmkorea_fixture_dir: Optional[str] = None,
    fmkorea_source: str = "fixture",
    fmkorea_data_path: Optional[str] = None,
    output_dir: Optional[str] = None,
    llm_provider: Optional[LLMProvider] = None,
) -> Dict[str, Any]:
    """Run the full pipeline once.

    1. Read 65stock market data (or sample)
    2. Parse FMKorea fixtures
    3. Run LLM sentiment analysis
    4. Calculate hourly FM Index
    5. Join market + sentiment
    6. Generate dashboard data
    7. Write output files

    Returns a summary dict with results and status.
    """
    results: Dict[str, Any] = {
        "startedAt": datetime.now(KST).isoformat(),
        "steps": [],
    }

    # --- Step 1: Market data ---
    bridge = MarketBridge()
    market_records: List[MarketRecord] = []

    if market_source == "sample":
        market_records = _generate_sample_market()
        results["steps"].append({
            "step": "market_bridge",
            "status": "sample",
            "records": len(market_records),
            "note": "Explicit --market-source sample",
        })
    elif market_data_path:
        try:
            if market_source == "kiwoom":
                # Strict real-KOSPI mode: validate the full contract and never
                # silently fall back to sample data on failure.
                market_records = bridge.read_kospi_records(market_data_path)
            else:
                market_records = bridge.read_records_from_path(market_data_path)
            results["steps"].append({
                "step": "market_bridge",
                "status": "ok",
                "records": len(market_records),
                "rejectedNonIndex": getattr(bridge, "rejected_non_index", 0),
                "source": market_data_path,
                "mode": market_source,
            })
        except FileNotFoundError as e:
            results["steps"].append({
                "step": "market_bridge",
                "status": "fail",
                "error": str(e),
            })
    else:
        try:
            market_records = bridge.read_records()
            results["steps"].append({
                "step": "market_bridge",
                "status": "ok",
                "records": len(market_records),
                "rejectedNonIndex": getattr(bridge, "rejected_non_index", 0),
                "source": str(bridge.data_root),
            })
        except FileNotFoundError as e:
            results["steps"].append({
                "step": "market_bridge",
                "status": "fail",
                "error": str(e),
            })

    # Fallback to sample data only in auto mode (never in kiwoom mode).
    if not market_records and market_source != "kiwoom":
        market_records = _generate_sample_market()
        results["steps"].append({
            "step": "market_bridge",
            "status": "sample",
            "records": len(market_records),
            "note": "Using sample market data (real data not found)",
        })

    if not market_records and market_source == "kiwoom":
        # Fail closed: do NOT substitute sample data for a failed Kiwoom run.
        results["steps"].append({
            "step": "market_bridge",
            "status": "unavailable",
            "records": 0,
            "note": "Kiwoom real mode requested but no validated KOSPI records; no sample fallback.",
        })
        results["marketUnavailable"] = True
        raise RuntimeError(
            "Kiwoom real mode requested but no validated KOSPI records were found. "
            "Sample data was NOT substituted."
        )

    # --- Step 2: FMKorea fixtures ---
    fixture_dir = Path(fmkorea_fixture_dir or FIXTURE_DIR)
    parser = FMKoreaParser()
    posts: List[ParsedPost] = []
    community_source = fmkorea_source
    community_data_mode = fmkorea_source

    if fmkorea_source == "live":
        posts = _load_live_posts(fmkorea_data_path)
        if not posts:
            raise RuntimeError(
                "FMKorea live mode requested but no validated posts found "
                "(missing/empty/contract-invalid JSONL). Sample posts were NOT substituted."
            )
        community_source = "fmkorea-stock"
        community_data_mode = "real"
        results["steps"].append({
            "step": "fmkorea_parse",
            "status": "ok",
            "posts": len(posts),
            "comments": sum(len(p.comments) for p in posts),
            "source": "live-normalized-jsonl",
            "dataMode": "real",
        })
    else:
        try:
            posts = parser.parse_fixture_dir(str(fixture_dir))
            results["steps"].append({
                "step": "fmkorea_parse",
                "status": "ok",
                "posts": len(posts),
                "comments": sum(len(p.comments) for p in posts),
                "source": str(fixture_dir),
            })
        except FileNotFoundError as e:
            results["steps"].append({
                "step": "fmkorea_parse",
                "status": "fail",
                "error": str(e),
            })

        # Fallback to sample posts only in fixture/sample modes (never live).
        if not posts:
            posts = _generate_sample_posts()
            community_source = "sample"
            community_data_mode = "sample"
            results["steps"].append({
                "step": "fmkorea_parse",
                "status": "sample",
                "posts": len(posts),
                "note": "Using sample posts (fixtures not found)",
            })

    # --- Step 3: LLM sentiment analysis ---
    provider = llm_provider or create_provider()
    post_sentiments: List[PostWithSentiment] = []

    for post in posts:
        comment_texts = [c.body for c in post.comments]
        sentiment = provider.analyze(post.title, post.body, comment_texts)
        ts = post.firstSeenAt or post.publishedAt or datetime.now(KST).isoformat()
        post_sentiments.append(
            PostWithSentiment(
                timestamp=ts,
                sentiment=sentiment,
                commentCount=len(post.comments),
            )
        )

    results["steps"].append({
        "step": "llm_sentiment",
        "status": "ok",
        "analyzed": len(post_sentiments),
        "provider": provider.__class__.__name__,
    })

    # --- Step 4: Hourly FM Index ---
    calculator = FMIndexCalculator(use_first_seen=True)
    fm_indices = calculator.calculate_hourly(post_sentiments)

    results["steps"].append({
        "step": "fmindex_hourly",
        "status": "ok",
        "buckets": len(fm_indices),
    })

    # --- Step 5: Join market + sentiment ---
    joiner = MarketSentimentJoiner()
    joined = joiner.join(market_records, fm_indices, market="KOSPI")

    results["steps"].append({
        "step": "market_join",
        "status": "ok",
        "joined": len(joined),
    })

    # --- Step 5b: Overnight evaluation (not computed from non-session data) ---
    overnight = {
        "status": "unavailable",
        "reason": "real_session_data_not_available",
    }

    # --- Step 6: Summary ---
    total_posts = len(posts)
    total_comments = sum(len(p.comments) for p in posts)
    total_analyzed = len(post_sentiments)
    avg_conf = (
        sum(ps.sentiment.confidence for ps in post_sentiments) / len(post_sentiments)
        if post_sentiments else 0.0
    )

    provider_name = provider.__class__.__name__
    instrument_id = getattr(market_records[0], "instrument_id", "") if market_records else ""
    symbol = getattr(market_records[0], "symbol", "") if market_records else ""
    data_mode = getattr(market_records[0], "data_mode", "sample") if market_records else "sample"
    asset_type = getattr(market_records[0], "asset_type", "index") if market_records else "index"
    market_provider = getattr(market_records[0], "provider", "") if market_records else ""
    if not market_provider:
        market_provider = market_source

    seen_times = [
        t for t in (p.firstSeenAt or p.publishedAt for p in posts) if t
    ]
    community_first_seen = min(seen_times) if seen_times else None
    community_last_seen = max(seen_times) if seen_times else None

    summary = {
        "totalPosts": total_posts,
        "totalComments": total_comments,
        "totalAnalyzed": total_analyzed,
        "avgConfidence": round(avg_conf, 4),
        "fmIndexBuckets": len(fm_indices),
        "joinedRecords": len(joined),
        "provider": provider_name,
        "llmProvider": provider_name,
        "marketProvider": market_provider,
        "marketSource": market_source,
        "instrument": instrument_id,
        "symbol": symbol,
        "assetType": asset_type,
        "dataMode": data_mode,
        "communitySource": community_source,
        "communityDataMode": community_data_mode,
        "communityPosts": total_posts,
        "communityComments": total_comments,
        "communityFirstSeenAt": community_first_seen,
        "communityLastSeenAt": community_last_seen,
        "methodologyVersion": METHODOLOGY_VERSION,
        "lastUpdated": datetime.now(KST).isoformat(),
    }

    # --- Step 7: Write output ---
    out_dir = Path(output_dir or OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)

    joined_dicts = [j.to_dict() for j in joined]
    write_dashboard_files(str(out_dir), joined_dicts, overnight, summary)

    # Also write raw JSON
    raw_path = out_dir / "pipeline_output.json"
    raw_path.write_text(
        json.dumps(
            {
                "marketRecords": [r.to_dict() for r in market_records],
                "fmIndices": [f.to_dict() for f in fm_indices],
                "joined": joined_dicts,
                "overnight": overnight,
                "summary": summary,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    results["steps"].append({
        "step": "dashboard_export",
        "status": "ok",
        "outputDir": str(out_dir),
        "htmlPath": str(out_dir / "index.html"),
    })

    results["completedAt"] = datetime.now(KST).isoformat()
    results["summary"] = summary
    results["outputDir"] = str(out_dir)

    return results


def _generate_sample_market() -> List[MarketRecord]:
    """Generate sample market data for testing when 65stock data is unavailable."""
    now = datetime.now(KST).replace(minute=0, second=0, microsecond=0)
    records = []
    base_price = 3200.0

    for i in range(8):
        ts = now - timedelta(hours=7 - i)
        change = ((-1) ** i) * (i * 0.5 + 2)
        open_p = base_price + change
        close_p = open_p + ((-1) ** (i + 1)) * 3
        high_p = max(open_p, close_p) + 2
        low_p = min(open_p, close_p) - 2
        cr = round(((close_p - open_p) / open_p) * 100, 2) if open_p else 0.0

        records.append(
            MarketRecord(
                timestamp=ts.isoformat(),
                market="KOSPI",
                instrument_id="KOSPI",
                symbol="KOSPI",
                open=round(open_p, 2),
                high=round(high_p, 2),
                low=round(low_p, 2),
                close=round(close_p, 2),
                change_rate=cr,
                source="sample-data",
                observed_at=datetime.now(KST).isoformat(),
                data_mode="sample",
                provider="sample",
                asset_type="index",
            )
        )
        base_price = close_p

    return records


def _load_live_posts(data_path: Optional[str]) -> List[ParsedPost]:
    """Load normalized live FMKorea posts from a JSONL file.

    Fail-closed: missing file, empty file, invalid JSON, wrong source, or
    wrong dataMode raises. Only ``source == "fmkorea-stock"`` and
    ``dataMode == "real"`` records are accepted.
    """
    if not data_path:
        raise RuntimeError(
            "FMKorea live mode requires --fmkorea-data <jsonl path>"
        )
    path = Path(data_path)
    if not path.exists():
        raise RuntimeError(f"FMKorea live data file not found: {path}")

    posts: List[ParsedPost] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"FMKorea live JSONL line {lineno} is not valid JSON: {e}"
            )
        if raw.get("source") != "fmkorea-stock":
            raise RuntimeError(
                f"FMKorea live JSONL line {lineno}: source must be fmkorea-stock"
            )
        if raw.get("dataMode") != "real":
            raise RuntimeError(
                f"FMKorea live JSONL line {lineno}: dataMode must be real"
            )
        comments = [
            Comment(
                body=c.get("body", ""),
                publishedAt=c.get("publishedAt", ""),
                recommendationCount=c.get("recommendationCount", 0),
            )
            for c in raw.get("comments", [])
        ]
        posts.append(
            ParsedPost(
                sourcePostId=str(raw.get("sourcePostId", "")),
                url=raw.get("canonicalUrl", "") or "",
                title=raw.get("title", "") or "",
                body=raw.get("body", "") or "",
                publishedAt=raw.get("publishedAt") or "",
                firstSeenAt=raw.get("firstSeenAt") or "",
                viewCount=raw.get("viewCount", 0) or 0,
                recommendationCount=raw.get("recommendationCount", 0) or 0,
                commentCount=raw.get("commentCount", 0) or 0,
                comments=comments,
            )
        )
    return posts


def _generate_sample_posts() -> List[ParsedPost]:
    """Generate sample FMKorea posts for testing."""
    now = datetime.now(KST).replace(minute=0, second=0, microsecond=0)
    samples = [
        ("코스피 오늘 급등 예상, 반도체 호재 쏟아진다", "삼성전자 SK하이닉스 상승 매수 신호", "positive"),
        ("시장 폭락 공포 심화, 손절 가속", "하락 장세 매도 관망 위험", "negative"),
        ("오늘 장세는 보합권, 관망이 답", "횡보 박스권 지지 저항", "neutral"),
        ("반도체 폭등! 상한가 러시", "급등 상승 갭상승 신고가", "positive"),
        ("외인 대거 매도, 하락 압력 심하다", "급락 손실 위험 공포", "negative"),
    ]

    posts = []
    for i, (title, body, _) in enumerate(samples):
        ts = now - timedelta(hours=4 - i)
        posts.append(
            ParsedPost(
                sourcePostId=f"sample-{i+1}",
                url=f"https://example.com/post/{i+1}",
                title=title,
                body=body,
                publishedAt=ts.isoformat(),
                firstSeenAt=ts.isoformat(),
                viewCount=100 + i * 50,
                recommendationCount=10 + i * 5,
                commentCount=3 + i,
                comments=[],
            )
        )
    return posts


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="FMIndex pipeline — run the full data flow once"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run the pipeline once and exit",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Serve the dashboard after running the pipeline",
    )
    parser.add_argument(
        "--market-data",
        type=str,
        default=None,
        help="Path to 65stock market data file (JSON or CSV)",
    )
    parser.add_argument(
        "--market-source",
        type=str,
        default="auto",
        choices=["auto", "sample", "kiwoom"],
        help="Market data source: auto (real if available, else sample), "
        "sample (explicit sample), kiwoom (real Kiwoom KOSPI only, fail-closed)",
    )
    parser.add_argument(
        "--fmkorea-dir",
        type=str,
        default=None,
        help="Path to FMKorea fixture directory",
    )
    parser.add_argument(
        "--fmkorea-source",
        type=str,
        default="fixture",
        choices=["fixture", "live", "sample"],
        help="FMKorea community source: fixture (default), live "
        "(verified normalized JSONL, fail-closed), sample (explicit)",
    )
    parser.add_argument(
        "--fmkorea-data",
        type=str,
        default=None,
        help="Path to live normalized FMKorea JSONL (used with --fmkorea-source live)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Output directory for dashboard files",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8420,
        help="Port for dashboard server (default: 8420)",
    )

    args = parser.parse_args()

    if not args.once and not args.serve:
        args.once = True

    if args.once:
        print("FMIndex pipeline: running once...")
        results = run_pipeline_once(
            market_data_path=args.market_data,
            market_source=args.market_source,
            fmkorea_fixture_dir=args.fmkorea_dir,
            fmkorea_source=args.fmkorea_source,
            fmkorea_data_path=args.fmkorea_data,
            output_dir=args.output_dir,
        )

        print("\n=== Pipeline Results ===")
        for step in results["steps"]:
            status_icon = "✓" if step["status"] in ("ok", "sample") else "✗"
            print(f"  {status_icon} {step['step']}: {step['status']}")
            for k, v in step.items():
                if k not in ("step", "status"):
                    print(f"      {k}: {v}")

        print(f"\n  Output: {results.get('outputDir', 'N/A')}")
        print(f"  Summary: {json.dumps(results.get('summary', {}), ensure_ascii=False)}")

    if args.serve:
        out_dir = Path(args.output_dir or OUTPUT_DIR)
        if not (out_dir / "index.html").exists():
            print("Dashboard not found, running pipeline first...")
            run_pipeline_once(
                market_data_path=args.market_data,
                market_source=args.market_source,
                fmkorea_fixture_dir=args.fmkorea_dir,
                fmkorea_source=args.fmkorea_source,
                fmkorea_data_path=args.fmkorea_data,
                output_dir=args.output_dir,
            )
        print(f"\nServing dashboard from {out_dir}...")
        serve_dashboard(str(out_dir), port=args.port)


if __name__ == "__main__":
    main()
