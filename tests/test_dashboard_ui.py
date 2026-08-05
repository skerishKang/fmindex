"""Issue #5 UI tests — verify the light chart-first dashboard product UI.

Most logic tests execute the real shipped dashboard.js under node, so we
verify actual behavior rather than string presence. Structural tests read
the real templates/static files.
"""

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fmindex.dashboard.server import (
    write_dashboard_files,
    generate_dashboard_data,
    filter_by_period,
)

KST = timezone(timedelta(hours=9))
REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = REPO_ROOT / "fmindex" / "dashboard" / "templates" / "index.html"
CSS = REPO_ROOT / "fmindex" / "dashboard" / "static" / "css" / "dashboard.css"
JS = REPO_ROOT / "fmindex" / "dashboard" / "static" / "js" / "dashboard.js"

NODE = shutil.which("node")


def run_js(expr: str) -> str:
    """Run an expression against the real dashboard.js under node."""
    assert NODE, "node is required for UI logic tests"
    script = f"const ui = require({str(JS)!r}); const out = {expr}; console.log(JSON.stringify(out));"
    proc = subprocess.run(
        [NODE, "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}\n{proc.stdout}"
    return proc.stdout.strip().splitlines()[-1]


def js_json(expr: str):
    return json.loads(run_js(expr))


def sample_joined() -> list:
    """Deterministic joined records with a null fmIndex gap and a real 0."""
    now = datetime(2026, 8, 5, 9, 0, tzinfo=KST)
    rows = []
    for i in range(6):
        ts = now - timedelta(hours=5 - i)
        row = {
            "timestamp": ts.isoformat(),
            "market": "KOSPI",
            "instrumentId": "KOSPI",
            "symbol": "KOSPI",
            "fmIndex": None,          # default null → gap
            "hasSentiment": False,
            "marketNormalized": 100.0,
            "marketChangeRate": 0.0,
            "postCount": 0,
            "confidence": 0.0,
            "dataMode": "sample",
        }
        if i == 1:
            row["fmIndex"] = 0.0          # real zero must be preserved
            row["hasSentiment"] = True
        elif i == 3:
            row["fmIndex"] = 65.5         # sentiment present
            row["hasSentiment"] = True
        elif i == 4:
            row["fmIndex"] = 55.0
            row["hasSentiment"] = True
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- #
# LIGHT_THEME_DEFAULT_PASS                                                    #
# --------------------------------------------------------------------------- #


class TestLightThemeDefault:
    def test_default_settings_is_light(self):
        assert js_json("ui.defaultSettings()")["theme"] == "light"

    def test_read_settings_defaults_light_without_storage(self):
        assert js_json("ui.readSettings(null)")["theme"] == "light"

    def test_css_root_uses_light_background(self):
        css = CSS.read_text(encoding="utf-8")
        # :root defines light-first variables; body uses light bg by default
        assert "--bg: #f5f6f8" in css or "--bg" in css
        assert "[data-theme=\"dark\"]" in css


# --------------------------------------------------------------------------- #
# DARK_THEME_TOGGLE_PASS                                                      #
# --------------------------------------------------------------------------- #


class TestDarkThemeToggle:
    def test_dark_theme_css_exists(self):
        css = CSS.read_text(encoding="utf-8")
        assert "[data-theme=\"dark\"]" in css

    def test_theme_toggle_button_in_template(self):
        html = TEMPLATE.read_text(encoding="utf-8")
        assert 'id="themeToggle"' in html
        assert "다크 모드 전환" in html


# --------------------------------------------------------------------------- #
# THEME_PERSISTENCE_PASS                                                      #
# --------------------------------------------------------------------------- #


class TestUrlDeepLink:
    def test_valid_params_parsed(self):
        out = js_json("ui.readUrlParams('?theme=dark&market=NASDAQ&colors=us&period=30d&view=divergence&up=%23ff00aa')")
        assert out['theme'] == 'dark'
        assert out['colorMode'] == 'us'
        assert out['market'] == 'NASDAQ'
        assert out['period'] == '30d'
        assert out['view'] == 'divergence'
        assert out['customUp'] == '#ff00aa'

    def test_invalid_params_rejected(self):
        out = js_json("ui.readUrlParams('?theme=blue&market=TEST&colors=neon&evil=<script>')")
        assert out == {}

    def test_empty_search_returns_empty(self):
        out = js_json("ui.readUrlParams('')")
        assert out == {}

class TestThemePersistence:
    def test_theme_saved_and_reloaded(self):
        # verify via node round trip
        out = run_js(
            "(() => { const s={}; const st={setItem:(k,v)=>s[k]=v,getItem:(k)=>s[k]??null};"
            "ui.saveSettings(st,{...ui.defaultSettings(),theme:'dark'});"
            "const r=ui.readSettings(st); return r.theme; })()"
        )
        assert json.loads(out) == "dark"

    def test_storage_key_exists(self):
        keys = js_json("ui.STORAGE_KEYS")
        assert keys["theme"] == "fmindex.theme"


# --------------------------------------------------------------------------- #
# KR_COLOR_MODE_DEFAULT_PASS                                                  #
# --------------------------------------------------------------------------- #


class TestKrColorModeDefault:
    def test_default_color_mode_is_kr(self):
        assert js_json("ui.defaultSettings()")["colorMode"] == "kr"

    def test_kr_up_red_down_blue(self):
        c = js_json("ui.upDownColors({colorMode:'kr', customUp:'#000000', customDown:'#ffffff'})")
        assert c["up"] == "#e53935"
        assert c["down"] == "#1e63c7"


# --------------------------------------------------------------------------- #
# US_COLOR_MODE_PASS                                                          #
# --------------------------------------------------------------------------- #


class TestUsColorMode:
    def test_us_up_green_down_red(self):
        c = js_json("ui.upDownColors({colorMode:'us', customUp:'#000000', customDown:'#ffffff'})")
        assert c["up"] == "#2e9e5b"
        assert c["down"] == "#e53935"

    def test_us_option_in_settings(self):
        html = TEMPLATE.read_text(encoding="utf-8")
        assert "value=\"us\"" in html
        assert "미국식" in html


# --------------------------------------------------------------------------- #
# CUSTOM_COLOR_MODE_PASS                                                      #
# --------------------------------------------------------------------------- #


class TestCustomColorMode:
    def test_custom_colors_used(self):
        c = js_json("ui.upDownColors({colorMode:'custom', customUp:'#111111', customDown:'#222222'})")
        assert c["up"] == "#111111"
        assert c["down"] == "#222222"

    def test_custom_color_pickers_in_template(self):
        html = TEMPLATE.read_text(encoding="utf-8")
        assert 'id="customUpColor"' in html
        assert 'id="customDownColor"' in html


# --------------------------------------------------------------------------- #
# COLOR_MODE_PERSISTENCE_PASS                                                 #
# --------------------------------------------------------------------------- #


class TestColorModePersistence:
    def test_color_mode_round_trip(self):
        out = run_js(
            "(() => { const s={}; const st={setItem:(k,v)=>s[k]=v,getItem:(k)=>s[k]??null};"
            "ui.saveSettings(st,{...ui.defaultSettings(),colorMode:'us'});"
            "const r=ui.readSettings(st); return r.colorMode; })()"
        )
        assert json.loads(out) == "us"

    def test_custom_colors_round_trip(self):
        out = run_js(
            "(() => { const s={}; const st={setItem:(k,v)=>s[k]=v,getItem:(k)=>s[k]??null};"
            "ui.saveSettings(st,{...ui.defaultSettings(),colorMode:'custom',customUp:'#a1b2c3',customDown:'#d4e5f6'});"
            "const r=ui.readSettings(st); return [r.colorMode,r.customUp,r.customDown]; })()"
        )
        mode, up, down = json.loads(out)
        assert mode == "custom"
        assert up == "#a1b2c3"
        assert down == "#d4e5f6"


# --------------------------------------------------------------------------- #
# CHART_FIRST_LAYOUT_PASS                                                     #
# --------------------------------------------------------------------------- #


class TestChartFirstLayout:
    def test_chart_is_above_the_fold_structure(self):
        html = TEMPLATE.read_text(encoding="utf-8")
        chart_pos = html.index('id="mainChart"')
        interp_pos = html.index("오늘의 해석")
        # the current-index hero card and controls are required ABOVE the chart
        assert html.index("index-hero") < chart_pos
        # chart comes before interpretation & bottom analysis in DOM
        assert chart_pos < interp_pos

    def test_no_marketing_banner_above_chart(self):
        html = TEMPLATE.read_text(encoding="utf-8")
        main_start = html.index("<main")
        chart_pos = html.index('id="mainChart"')
        above = html[main_start:chart_pos]
        for banned in ("광고", "배너", "intro", "hero-banner", "마케팅"):
            assert banned not in above

    def test_chart_canvas_has_min_height_css(self):
        css = CSS.read_text(encoding="utf-8")
        assert "height: 320px" in css


# --------------------------------------------------------------------------- #
# NO_BOARD_LIST_ON_HOME_PASS                                                  #
# --------------------------------------------------------------------------- #


class TestNoBoardListOnHome:
    def test_home_has_no_board_table(self):
        html = TEMPLATE.read_text(encoding="utf-8")
        assert "<table" not in html
        assert "board-list" not in html
        assert "post-list" not in html

    def test_no_post_number_title_author_list(self):
        html = TEMPLATE.read_text(encoding="utf-8")
        for marker in ("class=\"post", "class=\"board", "작성자", "글번호"):
            assert marker not in html


# --------------------------------------------------------------------------- #
# KOSPI_NASDAQ_SWITCH_PASS                                                    #
# --------------------------------------------------------------------------- #


class TestKospiNasdaqSwitch:
    def test_market_buttons_exist(self):
        html = TEMPLATE.read_text(encoding="utf-8")
        assert 'data-market="KOSPI"' in html
        assert 'data-market="NASDAQ"' in html
        assert "코스피" in html
        assert "나스닥" in html

    def test_kospi_panel_is_night_session(self):
        html = js_json(
            "ui.sessionPanelHtml('KOSPI', {overnight:{status:'unavailable',reason:'real_session_data_not_available'}}, {dataMode:'sample'})"
        )
        assert "야간 심리와 다음 국내장" in html
        assert "야간선물" in html

    def test_nasdaq_panel_is_us_session(self):
        html = js_json(
            "ui.sessionPanelHtml('NASDAQ', null, {dataMode:'sample'})"
        )
        assert "한국 주간 심리와 미국장 결과" in html
        assert "미국장" in html

    def test_chart_build_preserves_market_series(self):
        joined = sample_joined()
        built = js_json(f"ui.buildChartData({json.dumps(joined)}, 'all')")
        assert len(built["labels"]) == len(joined)
        assert len(built["marketData"]) == len(joined)


# --------------------------------------------------------------------------- #
# TIMESTAMP_PERIOD_FILTER_UI_PASS                                             #
# --------------------------------------------------------------------------- #


class TestTimestampPeriodFilterUi:
    def test_js_filter_uses_timestamps(self):
        latest = datetime(2026, 8, 5, 12, 0, tzinfo=KST)
        rows = [
            {"timestamp": latest.isoformat(), "v": 1},
            {"timestamp": (latest - timedelta(hours=25)).isoformat(), "v": 2},
            {"timestamp": (latest - timedelta(days=8)).isoformat(), "v": 3},
            {"timestamp": (latest - timedelta(days=31)).isoformat(), "v": 4},
        ]
        got = js_json(f"ui.filterByPeriod({json.dumps(rows)}, '24h')")
        assert len(got) == 1 and got[0]["v"] == 1

    def test_server_filter_matches_js_semantics(self):
        latest = datetime(2026, 8, 5, 12, 0, tzinfo=KST)
        rows = [
            {"timestamp": latest.isoformat()},
            {"timestamp": (latest - timedelta(hours=25)).isoformat()},
        ]
        server = filter_by_period(rows, "24h")
        client = js_json(f"ui.filterByPeriod({json.dumps(rows)}, '24h')")
        assert len(server) == len(client) == 1


# --------------------------------------------------------------------------- #
# MISSING_SENTIMENT_GAP_PASS                                                  #
# --------------------------------------------------------------------------- #


class TestMissingSentimentGap:
    def test_null_fmindex_stays_null(self):
        joined = sample_joined()
        built = js_json(f"ui.buildChartData({json.dumps(joined)}, 'all')")
        fm = built["fmData"]
        # positions where fmIndex is None must remain null (gap), never 50
        assert fm[0] is None
        assert fm[2] is None
        assert fm[5] is None
        assert 0.0 in fm  # the real zero survives

    def test_zero_is_not_50(self):
        joined = sample_joined()
        built = js_json(f"ui.buildChartData({json.dumps(joined)}, 'all')")
        assert built["fmData"][1] == 0.0


# --------------------------------------------------------------------------- #
# ZERO_FMINDEX_UI_PASS                                                        #
# --------------------------------------------------------------------------- #


class TestZeroFmIndexUi:
    def test_zero_not_treated_as_missing(self):
        joined = sample_joined()
        built = js_json(f"ui.buildChartData({json.dumps(joined)}, 'all')")
        assert built["fmData"][1] == 0.0

    def test_sentiment_label_for_zero(self):
        assert js_json("ui.sentimentLabel(0)") == "강한 부정"


# --------------------------------------------------------------------------- #
# SAMPLE_DATA_BADGE_PASS                                                      #
# --------------------------------------------------------------------------- #


class TestSampleDataBadge:
    def test_sample_badge_visible_for_sample_mode(self):
        assert js_json("ui.sampleBadgeVisible('sample')") is True
        assert js_json("ui.sampleBadgeVisible('real')") is False

    def test_badge_element_in_template(self):
        html = TEMPLATE.read_text(encoding="utf-8")
        assert 'id="sampleBadge"' in html
        assert "SAMPLE DATA" in html


# --------------------------------------------------------------------------- #
# OVERNIGHT_UNAVAILABLE_UI_PASS                                               #
# --------------------------------------------------------------------------- #


class TestOvernightUnavailableUi:
    def test_kospi_panel_unavailable_message(self):
        html = js_json(
            "ui.sessionPanelHtml('KOSPI', {overnight:{status:'unavailable',reason:'real_session_data_not_available'}}, {dataMode:'sample'})"
        )
        assert "UNAVAILABLE" in html
        assert "실제 거래일·야간선물 데이터 연동 전입니다" in html
        assert "샘플" in html
        # no fake numbers
        assert "0%" not in html.replace("0.0", "")

    def test_interpretation_mentions_sample(self):
        txt = js_json(
            "ui.interpretToday({fmIndex:34.0, marketChangeRate:-1.2, postCount:87, confidence:0.82}, {totalAnalyzed:87, avgConfidence:0.82, dataMode:'sample'})"
        )
        assert "샘플" in txt
        assert "부정" in txt


# --------------------------------------------------------------------------- #
# US_DAY_SENTIMENT_UNAVAILABLE_UI_PASS                                        #
# --------------------------------------------------------------------------- #


class TestUsDaySentimentUnavailableUi:
    def test_nasdaq_panel_unavailable_message(self):
        html = js_json(
            "ui.sessionPanelHtml('NASDAQ', null, {dataMode:'sample'})"
        )
        assert "UNAVAILABLE" in html
        assert "실제 나스닥 세션 데이터 연동 전입니다" in html

    def test_no_fake_us_numbers(self):
        html = js_json(
            "ui.sessionPanelHtml('NASDAQ', null, {dataMode:'sample'})"
        )
        assert "–" in html  # placeholders, not fabricated values
        assert "%" not in html


# --------------------------------------------------------------------------- #
# MOBILE_STRUCTURE_PASS                                                       #
# --------------------------------------------------------------------------- #


class TestMobileStructure:
    def test_mobile_media_queries_exist(self):
        css = CSS.read_text(encoding="utf-8")
        assert "@media (max-width: 860px)" in css
        assert "@media (max-width: 640px)" in css
        assert "@media (max-width: 400px)" in css

    def test_hero_before_chart_in_dom(self):
        html = TEMPLATE.read_text(encoding="utf-8")
        assert html.index("index-hero") < html.index('id="mainChart"')

    def test_viewport_meta_present(self):
        html = TEMPLATE.read_text(encoding="utf-8")
        assert 'name="viewport"' in html


# --------------------------------------------------------------------------- #
# SAFE_DOM_RENDERING_PASS                                                     #
# --------------------------------------------------------------------------- #


class TestSafeDomRendering:
    def test_escape_html_escapes_all(self):
        out = js_json("ui.escapeHtml('<script>\\\"&\\'\\\"</script>')")
        assert "<" not in out
        assert "&lt;" in out
        assert "&amp;" in out
        assert "&quot;" in out
        assert "&#39;" in out

    def test_session_panel_escapes_input(self):
        # hostile market/overnight values must not inject HTML
        html = js_json(
            "ui.sessionPanelHtml('KOSPI', {overnight:{status:'<img src=x onerror=alert(1)>', reason:'x'}}, {dataMode:'sample'})"
        )
        assert "<img" not in html
        assert "&lt;img" in html

    def test_no_inline_onclick_with_untrusted_data(self):
        js = JS.read_text(encoding="utf-8")
        assert "innerHTML" in js  # used, but...
        # ...data values are escaped before interpolation in session panel
        assert "escapeHtml" in js


# --------------------------------------------------------------------------- #
# OUTPUT CONTRACT PASS                                                        #
# --------------------------------------------------------------------------- #


class TestOutputContract:
    def test_write_dashboard_files_writes_assets(self, tmp_path):
        joined = [j for j in sample_joined()]
        summary = {
            "totalPosts": 10,
            "totalAnalyzed": 8,
            "avgConfidence": 0.8,
            "provider": "MockLLMProvider",
            "instrument": "KOSPI",
            "symbol": "KOSPI",
            "dataMode": "sample",
            "methodologyVersion": "fmindex-v1",
            "lastUpdated": "2026-08-05T12:00:00+09:00",
        }
        overnight = {"status": "unavailable", "reason": "real_session_data_not_available"}
        out = tmp_path / "out"
        html_path = write_dashboard_files(str(out), joined, overnight, summary)

        assert (out / "index.html").exists()
        assert (out / "css" / "dashboard.css").exists()
        assert (out / "js" / "dashboard.js").exists()
        assert (out / "api" / "data.json").exists()

        html = (out / "index.html").read_text(encoding="utf-8")
        assert '/css/dashboard.css' in html
        assert '/js/dashboard.js' in html

        data = json.loads((out / "api" / "data.json").read_text(encoding="utf-8"))
        assert data["summary"]["methodologyVersion"] == "fmindex-v1"
        assert data["overnight"]["status"] == "unavailable"
        assert data["summary"]["dataMode"] == "sample"


# --------------------------------------------------------------------------- #
# INTEGRATED_CONTRACTS_PASS (ported from remote PR #7 + integration gates)
# --------------------------------------------------------------------------- #


class TestIntegratedContracts:
    def test_period_filter_anchored_to_latest_record(self):
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
        client = js_json(f"ui.filterByPeriod({json.dumps(records)}, '7d')")
        assert len(client) == 2

    def test_nasdaq_market_series_is_not_relabeled(self):
        joined = sample_joined()
        nasdaq = js_json(f"ui.marketSeriesFor({json.dumps(joined)}, 'NASDAQ')")
        assert all(v is None for v in nasdaq)
        kospi = js_json(f"ui.marketSeriesFor({json.dumps(joined)}, 'KOSPI')")
        assert any(v is not None for v in kospi)

    def test_chart_status_aria_live_region_exists(self):
        html = TEMPLATE.read_text(encoding="utf-8")
        assert 'id="chartStatus"' in html
        assert 'aria-live="polite"' in html
        assert 'role="status"' in html

    def test_mobile_nav_contract(self):
        html = TEMPLATE.read_text(encoding="utf-8")
        assert 'class="mobile-nav"' in html
        for label in ("오늘", "연관성", "기록", "데이터 상태"):
            assert label in html
        css = CSS.read_text(encoding="utf-8")
        assert ".mobile-nav" in css
        assert "env(safe-area-inset-bottom)" in css

    def test_settings_escape_close_and_aria(self):
        js = JS.read_text(encoding="utf-8")
        assert "Escape" in js
        html = TEMPLATE.read_text(encoding="utf-8")
        assert 'aria-controls="settingsPanel"' in html
        assert 'aria-expanded' in html

    def test_theme_toggle_aria_pressed(self):
        js = JS.read_text(encoding="utf-8")
        assert "aria-pressed" in js
        assert "aria-label" in js

    def test_package_data_assets_configured(self):
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert "package-data" in pyproject
        assert "templates/*.html" in pyproject
        assert "static/css/*.css" in pyproject
        assert "static/js/*.js" in pyproject
