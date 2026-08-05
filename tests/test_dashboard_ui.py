"""Product UI contract tests for the chart-first dashboard shell."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fmindex.dashboard.server import (
    CSS_PATH,
    DASHBOARD_HTML,
    JS_PATH,
    TEMPLATE_PATH,
    filter_by_period,
    write_dashboard_files,
)

KST = timezone(timedelta(hours=9))


def _sample_joined() -> list[dict]:
    base = datetime(2026, 8, 5, 9, 0, tzinfo=KST)
    return [
        {
            "timestamp": base.isoformat(),
            "market": "KOSPI",
            "instrumentId": "KOSPI",
            "symbol": "KOSPI",
            "fmIndex": 0.0,
            "hasSentiment": True,
            "marketNormalized": 100.0,
            "marketChangeRate": -0.2,
            "postCount": 5,
            "confidence": 0.7,
            "dataMode": "sample",
            "source": "sample-data",
        },
        {
            "timestamp": (base + timedelta(hours=1)).isoformat(),
            "market": "KOSPI",
            "instrumentId": "KOSPI",
            "symbol": "KOSPI",
            "fmIndex": None,
            "hasSentiment": False,
            "marketNormalized": 100.4,
            "marketChangeRate": 0.4,
            "postCount": 0,
            "confidence": 0.0,
            "dataMode": "sample",
            "source": "sample-data",
        },
    ]


def test_dashboard_assets_are_packaged():
    assert TEMPLATE_PATH.exists()
    assert CSS_PATH.exists()
    assert JS_PATH.exists()
    assert TEMPLATE_PATH.read_text(encoding="utf-8") == DASHBOARD_HTML


def test_light_theme_is_default_and_dark_toggle_exists():
    assert '<html lang="ko" data-theme="light">' in DASHBOARD_HTML
    assert 'id="themeToggle"' in DASHBOARD_HTML
    js = JS_PATH.read_text(encoding="utf-8")
    assert 'localStorage.getItem(STORAGE_KEYS.theme) || "light"' in js
    assert 'applyTheme(state.theme === "light" ? "dark" : "light")' in js


def test_chart_first_layout_precedes_secondary_analysis():
    hero_position = DASHBOARD_HTML.index('class="hero-grid"')
    chart_position = DASHBOARD_HTML.index('id="mainChart"')
    metrics_position = DASHBOARD_HTML.index('class="metric-grid"')
    analysis_position = DASHBOARD_HTML.index('class="analysis-grid"')
    assert hero_position < chart_position < metrics_position < analysis_position


def test_home_does_not_copy_board_list_structure():
    lowered = DASHBOARD_HTML.lower()
    assert "board-list" not in lowered
    assert "게시글 번호" not in DASHBOARD_HTML
    assert "작성자 목록" not in DASHBOARD_HTML
    assert "댓글 목록" not in DASHBOARD_HTML


def test_market_period_view_and_color_controls_exist():
    for token in (
        'data-market="KOSPI"',
        'data-market="NASDAQ"',
        'data-period="24h"',
        'data-period="7d"',
        'data-period="30d"',
        'data-view="companion"',
        'data-view="lead"',
        'data-view="divergence"',
        'value="kr"',
        'value="us"',
        'value="custom"',
    ):
        assert token in DASHBOARD_HTML

    js = JS_PATH.read_text(encoding="utf-8")
    assert "fmindex.colorMode" in js
    assert "fmindex.riseColor" in js
    assert "fmindex.fallColor" in js


def test_safe_dom_rendering_contract():
    js = JS_PATH.read_text(encoding="utf-8")
    assert ".innerHTML" not in js
    assert "textContent" in js
    assert 'fetch("/api/data.json", { cache: "no-store" })' in js


def test_missing_sentiment_and_zero_are_preserved_in_export(tmp_path: Path):
    write_dashboard_files(
        str(tmp_path),
        _sample_joined(),
        {"status": "unavailable", "reason": "real_session_data_not_available"},
        {"dataMode": "sample", "provider": "MockLLMProvider"},
    )
    payload = json.loads((tmp_path / "api" / "data.json").read_text(encoding="utf-8"))
    assert payload["joined"][0]["fmIndex"] == 0.0
    assert payload["joined"][0]["hasSentiment"] is True
    assert payload["joined"][1]["fmIndex"] is None
    assert payload["joined"][1]["hasSentiment"] is False


def test_static_export_contains_html_css_js_and_status(tmp_path: Path):
    html_path = write_dashboard_files(
        str(tmp_path),
        _sample_joined(),
        None,
        {"dataMode": "sample", "provider": "MockLLMProvider"},
    )
    assert Path(html_path).exists()
    assert (tmp_path / "assets" / "css" / "dashboard.css").exists()
    assert (tmp_path / "assets" / "js" / "dashboard.js").exists()

    payload = json.loads((tmp_path / "api" / "data.json").read_text(encoding="utf-8"))
    assert payload["overnight"]["status"] == "unavailable"
    assert payload["overnight"]["reason"] == "real_session_data_not_available"
    assert payload["summary"]["dataMode"] == "sample"
    assert payload["summary"]["methodologyVersion"] == "fmindex-v1"


def test_period_filter_is_timestamp_based_and_anchored_to_latest_record():
    latest = datetime(2025, 1, 30, 12, 0, tzinfo=KST)
    records = [
        {"timestamp": (latest - timedelta(days=40)).isoformat()},
        {"timestamp": (latest - timedelta(days=8)).isoformat()},
        {"timestamp": (latest - timedelta(days=2)).isoformat()},
        {"timestamp": latest.isoformat()},
    ]
    assert len(filter_by_period(records, "24h")) == 1
    assert len(filter_by_period(records, "7d")) == 2
    assert len(filter_by_period(records, "30d")) == 3
    assert len(filter_by_period(records, "all")) == 4


def test_accessible_navigation_and_mobile_contract():
    assert 'class="skip-link"' in DASHBOARD_HTML
    assert 'aria-label="주요 메뉴"' in DASHBOARD_HTML
    assert 'aria-label="펨코지수와 시장지수 비교 차트"' in DASHBOARD_HTML
    assert 'class="mobile-nav"' in DASHBOARD_HTML
    css = CSS_PATH.read_text(encoding="utf-8")
    assert "@media (max-width: 760px)" in css
    assert "@media (max-width: 420px)" in css
    assert "overflow-x" in css
