"""Integration tests for the UI + KOSPI candidate branch.

These tests verify that the chart-first UI and the KOSPI hourly collector
work together under a single pipeline contract. All tests are offline:
no network access, no real Kiwoom credentials, no live FMKorea fetch.

Assertions validate the actual pipeline output JSON and the real shipped
dashboard.js behavior rather than string presence.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fmindex.pipeline import run_pipeline_once
from fmindex.market.models import HourlyIndexRecord
from fmindex.market.bridge import MarketBridge, MarketRecord

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "fmkorea"
TEMPLATE = REPO_ROOT / "fmindex" / "dashboard" / "templates" / "index.html"
CSS = REPO_ROOT / "fmindex" / "dashboard" / "static" / "css" / "dashboard.css"
JS = REPO_ROOT / "fmindex" / "dashboard" / "static" / "js" / "dashboard.js"
PYPROJECT = REPO_ROOT / "pyproject.toml"

NODE = shutil.which("node")


def make_record(
    ts="2026-08-05T10:00:00+09:00",
    instrument_id="001",
    symbol="KOSPI",
    provider="kiwoom",
    asset_type="index",
    data_mode="real",
    observed_at="2026-08-05T11:00:00+09:00",
    **kw,
):
    """Build a valid HourlyIndexRecord with consistent OHLC defaults."""
    values = dict(
        timestamp=ts,
        market="KOSPI",
        instrument_id=instrument_id,
        symbol=symbol,
        asset_type=asset_type,
        open=3200.0,
        high=3210.0,
        low=3190.0,
        close=3205.0,
        volume=None,
        change_rate=0.15,
        source="kiwoom-rest-api",
        provider=provider,
        observed_at=observed_at,
        data_mode=data_mode,
    )
    values.update(kw)
    if values.get("open") is not None and values.get("close") is not None:
        hi = max(values["open"], values["close"])
        lo = min(values["open"], values["close"])
        if values.get("high") is not None and values["high"] < hi:
            values["high"] = hi
        if values.get("low") is not None and values["low"] > lo:
            values["low"] = lo
    return HourlyIndexRecord(**values)


def kospi_jsonl_lines(records):
    return "\n".join(json.dumps(r.to_dict(), ensure_ascii=False) for r in records)


def run_js(expr: str) -> str:
    assert NODE, "node is required for UI logic tests"
    script = (
        f"const ui = require({str(JS)!r}); "
        f"const out = {expr}; console.log(JSON.stringify(out));"
    )
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node failed: {proc.stderr}\n{proc.stdout}"
    return proc.stdout.strip().splitlines()[-1]


def js_json(expr: str):
    return json.loads(run_js(expr))


def _read_output(out_dir: Path) -> dict:
    """Read the pipeline output JSON written by the pipeline."""
    return json.loads((out_dir / "pipeline_output.json").read_text(encoding="utf-8"))


def _read_data_json(out_dir: Path) -> dict:
    return json.loads((out_dir / "api" / "data.json").read_text(encoding="utf-8"))


def _assert_dashboard_assets(out_dir: Path):
    assert (out_dir / "index.html").exists()
    assert (out_dir / "css" / "dashboard.css").exists()
    assert (out_dir / "js" / "dashboard.js").exists()
    assert (out_dir / "api" / "data.json").exists()
    assert (out_dir / "pipeline_output.json").exists()


# --------------------------------------------------------------------------- #
# SAMPLE_PIPELINE_UI_CONTRACT_PASS                                            #
# --------------------------------------------------------------------------- #


class TestSamplePipelineUiContract:
    def test_sample_pipeline_ui_contract(self, tmp_path):
        out = tmp_path / "out"
        results = run_pipeline_once(
            market_source="sample",
            fmkorea_fixture_dir=str(FIXTURE_DIR),
            output_dir=str(out),
        )
        _assert_dashboard_assets(out)
        data = _read_data_json(out)
        summary = data["summary"]
        assert summary["dataMode"] == "sample"
        assert summary["marketSource"] == "sample"
        assert summary["marketProvider"] == "sample"
        assert summary["provider"] == "MockLLMProvider"
        assert summary["llmProvider"] == "MockLLMProvider"
        assert summary["instrument"] == "KOSPI"
        assert summary["symbol"] == "KOSPI"
        assert summary["assetType"] == "index"
        assert summary["methodologyVersion"]
        assert summary["lastUpdated"]
        assert data["overnight"]["status"] == "unavailable"
        assert results["summary"]["dataMode"] == "sample"


# --------------------------------------------------------------------------- #
# AUTO_PIPELINE_SAMPLE_FALLBACK_PASS                                          #
# --------------------------------------------------------------------------- #


class TestAutoPipelineSampleFallback:
    def test_auto_falls_back_to_sample(self, tmp_path):
        out = tmp_path / "out"
        results = run_pipeline_once(
            market_data_path=None,
            market_source="auto",
            fmkorea_fixture_dir=str(FIXTURE_DIR),
            output_dir=str(out),
        )
        assert results["summary"]["dataMode"] == "sample"
        assert results["summary"]["marketSource"] == "auto"
        assert results["summary"]["marketProvider"] == "sample"
        for step in results["steps"]:
            assert step["status"] in ("ok", "sample"), f"Step {step['step']} failed: {step}"


# --------------------------------------------------------------------------- #
# KIWOOM_FIXTURE_UI_CONTRACT_PASS                                             #
# --------------------------------------------------------------------------- #


class TestKiwoomFixtureUiContract:
    def test_kiwoom_fixture_ui_contract(self, tmp_path):
        recs = [
            make_record(ts="2026-08-05T09:00:00+09:00", close=3200.0),
            make_record(ts="2026-08-05T10:00:00+09:00", close=3205.0),
            make_record(ts="2026-08-05T11:00:00+09:00", close=3210.0),
        ]
        fixture = tmp_path / "kospi.jsonl"
        fixture.write_text(kospi_jsonl_lines(recs), encoding="utf-8")

        out = tmp_path / "out"
        results = run_pipeline_once(
            market_data_path=str(fixture),
            market_source="kiwoom",
            fmkorea_fixture_dir=str(FIXTURE_DIR),
            output_dir=str(out),
        )
        summary = results["summary"]
        assert summary["dataMode"] == "real"
        assert summary["marketSource"] == "kiwoom"
        assert summary["marketProvider"] == "kiwoom"
        assert summary["instrument"] == "001"
        assert summary["symbol"] == "KOSPI"
        assert summary["assetType"] == "index"
        assert summary["provider"] == "MockLLMProvider"

        _assert_dashboard_assets(out)
        data = _read_data_json(out)
        assert data["summary"]["dataMode"] == "real"
        assert data["summary"]["provider"] == "MockLLMProvider"
        assert data["summary"]["marketProvider"] == "kiwoom"
        assert data["overnight"]["status"] == "unavailable"
        assert data["joined"]
        assert all(r["dataMode"] == "real" for r in data["joined"])


# --------------------------------------------------------------------------- #
# KIWOOM_INVALID_FAILS_CLOSED / OTHER_INDEX_FAILS_CLOSED                      #
# --------------------------------------------------------------------------- #


class TestKiwoomFailsClosed:
    def test_individual_stock_fails_closed(self, tmp_path):
        rec = make_record(instrument_id="005930", symbol="삼성전자", asset_type="stock")
        bad = tmp_path / "stock.jsonl"
        bad.write_text(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n", encoding="utf-8")
        with pytest.raises(RuntimeError):
            run_pipeline_once(
                market_data_path=str(bad),
                market_source="kiwoom",
                fmkorea_fixture_dir=str(FIXTURE_DIR),
                output_dir=str(tmp_path / "out"),
            )

    def test_other_index_fails_closed(self, tmp_path):
        rec = make_record(instrument_id="101", symbol="KOSDAQ", asset_type="index")
        bad = tmp_path / "kosdaq.jsonl"
        bad.write_text(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n", encoding="utf-8")
        with pytest.raises(RuntimeError):
            run_pipeline_once(
                market_data_path=str(bad),
                market_source="kiwoom",
                fmkorea_fixture_dir=str(FIXTURE_DIR),
                output_dir=str(tmp_path / "out"),
            )


# --------------------------------------------------------------------------- #
# SUMMARY_PROVIDER_SEPARATION_PASS / MARKET_PROVIDER / MARKET_SOURCE          #
# --------------------------------------------------------------------------- #


class TestSummaryProviderSeparation:
    def test_sample_provider_separation(self, tmp_path):
        out = tmp_path / "out"
        run_pipeline_once(
            market_source="sample",
            fmkorea_fixture_dir=str(FIXTURE_DIR),
            output_dir=str(out),
        )
        summary = _read_output(out)["summary"]
        assert summary["provider"] == "MockLLMProvider"
        assert summary["llmProvider"] == "MockLLMProvider"
        assert summary["marketProvider"] == "sample"
        assert summary["marketSource"] == "sample"

    def test_kiwoom_provider_separation(self, tmp_path):
        rec = make_record()
        fixture = tmp_path / "kospi.jsonl"
        fixture.write_text(kospi_jsonl_lines([rec]), encoding="utf-8")
        out = tmp_path / "out"
        run_pipeline_once(
            market_data_path=str(fixture),
            market_source="kiwoom",
            fmkorea_fixture_dir=str(FIXTURE_DIR),
            output_dir=str(out),
        )
        summary = _read_output(out)["summary"]
        assert summary["provider"] == "MockLLMProvider"
        assert summary["llmProvider"] == "MockLLMProvider"
        assert summary["marketProvider"] == "kiwoom"
        assert summary["marketSource"] == "kiwoom"

    def test_market_provider_and_source(self, tmp_path):
        out = tmp_path / "out"
        run_pipeline_once(
            market_source="sample",
            fmkorea_fixture_dir=str(FIXTURE_DIR),
            output_dir=str(out),
        )
        data = _read_data_json(out)
        assert data["summary"]["marketProvider"] == "sample"
        assert data["summary"]["marketSource"] == "sample"


# --------------------------------------------------------------------------- #
# ASSET_TYPE / METHODOLOGY_VERSION / LAST_UPDATED                             #
# --------------------------------------------------------------------------- #


class TestSummaryFields:
    def test_asset_type_present(self, tmp_path):
        out = tmp_path / "out"
        run_pipeline_once(
            market_source="sample",
            fmkorea_fixture_dir=str(FIXTURE_DIR),
            output_dir=str(out),
        )
        summary = _read_output(out)["summary"]
        assert summary["assetType"] == "index"

    def test_methodology_version_preserved(self, tmp_path):
        out = tmp_path / "out"
        run_pipeline_once(
            market_source="sample",
            fmkorea_fixture_dir=str(FIXTURE_DIR),
            output_dir=str(out),
        )
        assert _read_output(out)["summary"]["methodologyVersion"]

    def test_last_updated_preserved(self, tmp_path):
        out = tmp_path / "out"
        run_pipeline_once(
            market_source="sample",
            fmkorea_fixture_dir=str(FIXTURE_DIR),
            output_dir=str(out),
        )
        assert _read_output(out)["summary"]["lastUpdated"]


# --------------------------------------------------------------------------- #
# DASHBOARD_ASSET_EXPORT_PASS                                                 #
# --------------------------------------------------------------------------- #


class TestDashboardAssetExport:
    def test_dashboard_assets_exported(self, tmp_path):
        out = tmp_path / "out"
        run_pipeline_once(
            market_source="sample",
            fmkorea_fixture_dir=str(FIXTURE_DIR),
            output_dir=str(out),
        )
        _assert_dashboard_assets(out)


# --------------------------------------------------------------------------- #
# SAMPLE_BADGE_DATA_CONTRACT_PASS                                             #
# --------------------------------------------------------------------------- #


class TestSampleBadgeDataContract:
    def test_sample_badge_visible_for_sample_data(self):
        assert js_json("ui.sampleBadgeVisible('sample')") is True

    def test_sample_badge_hidden_for_real_data(self):
        assert js_json("ui.sampleBadgeVisible('real')") is False

    def test_sample_badge_data_contract(self, tmp_path):
        out = tmp_path / "out"
        run_pipeline_once(
            market_source="sample",
            fmkorea_fixture_dir=str(FIXTURE_DIR),
            output_dir=str(out),
        )
        data = _read_data_json(out)
        assert js_json(f"ui.sampleBadgeVisible({json.dumps(data['summary']['dataMode'])})") is True


# --------------------------------------------------------------------------- #
# REAL_MODE_REMOVES_SAMPLE_STATE                                              #
# --------------------------------------------------------------------------- #


class TestRealModeRemovesSampleState:
    def test_real_mode_not_sample(self, tmp_path):
        rec = make_record()
        fixture = tmp_path / "kospi.jsonl"
        fixture.write_text(kospi_jsonl_lines([rec]), encoding="utf-8")
        out = tmp_path / "out"
        run_pipeline_once(
            market_data_path=str(fixture),
            market_source="kiwoom",
            fmkorea_fixture_dir=str(FIXTURE_DIR),
            output_dir=str(out),
        )
        data = _read_data_json(out)
        assert data["summary"]["dataMode"] == "real"
        assert data["summary"]["marketSource"] == "kiwoom"
        assert js_json("ui.sampleBadgeVisible('real')") is False


# --------------------------------------------------------------------------- #
# NASDAQ_REMAINS_UNAVAILABLE                                                  #
# --------------------------------------------------------------------------- #


class TestNasdaqRemainsUnavailable:
    def test_nasdaq_market_series_all_null(self, tmp_path):
        out = tmp_path / "out"
        run_pipeline_once(
            market_source="sample",
            fmkorea_fixture_dir=str(FIXTURE_DIR),
            output_dir=str(out),
        )
        data = _read_data_json(out)
        joined = data["joined"]
        assert joined
        nasdaq = js_json(f"ui.marketSeriesFor({json.dumps(joined)}, 'NASDAQ')")
        assert nasdaq == [None] * len(joined)

    def test_nasdaq_not_reusing_kospi_data(self, tmp_path):
        out = tmp_path / "out"
        run_pipeline_once(
            market_source="sample",
            fmkorea_fixture_dir=str(FIXTURE_DIR),
            output_dir=str(out),
        )
        data = _read_data_json(out)
        joined = data["joined"]
        assert all(r["market"] == "KOSPI" for r in joined)


# --------------------------------------------------------------------------- #
# NULL_SENTIMENT_GAP_PRESERVED / ZERO_SENTIMENT_PRESERVED                     #
# --------------------------------------------------------------------------- #


class TestSentimentGapAndZero:
    def _joined(self):
        return [
            {"timestamp": "2026-08-05T09:00:00+09:00", "fmIndex": None,
             "marketNormalized": 100.0, "marketChangeRate": 0.1},
            {"timestamp": "2026-08-05T10:00:00+09:00", "fmIndex": 0.0,
             "marketNormalized": 100.5, "marketChangeRate": -0.2},
            {"timestamp": "2026-08-05T11:00:00+09:00", "fmIndex": 65.5,
             "marketNormalized": 101.0, "marketChangeRate": 0.3},
        ]

    def test_null_sentiment_gap_preserved(self):
        data = self._joined()
        built = js_json(f"ui.buildChartData({json.dumps(data)}, 'all')")
        assert built["fmData"][0] is None

    def test_zero_sentiment_preserved(self):
        data = self._joined()
        built = js_json(f"ui.buildChartData({json.dumps(data)}, 'all')")
        assert built["fmData"][1] == 0.0


# --------------------------------------------------------------------------- #
# CHART_FILTER_ALIGNMENT_PRESERVED                                            #
# --------------------------------------------------------------------------- #


class TestChartFilterAlignment:
    def test_chart_series_alignment(self):
        now = "2026-08-05T12:00:00+09:00"
        data = []
        for i in range(5):
            h = i + 8
            ts = f"2026-08-05T{h:02d}:00:00+09:00"
            data.append({"timestamp": ts, "fmIndex": float(50 + i),
                         "marketNormalized": 100.0 + i, "marketChangeRate": 0.1})
        data.append({"timestamp": now, "fmIndex": 60.0,
                     "marketNormalized": 105.0, "marketChangeRate": 0.2})
        built = js_json(f"ui.buildChartSeries({json.dumps(data)}, '24h', 'KOSPI')")
        assert built["source"]
        assert len(built["labels"]) == len(built["fmData"]) == len(built["marketData"])
        assert len(built["labels"]) == len(built["source"])
        # 시장선과 펨코지수가 같은 source(기간 필터 결과)를 사용해야 시간축이 정렬된다.
        assert len(built["fmData"]) == len(built["marketData"])


# --------------------------------------------------------------------------- #
# PACKAGE_DATA_PRESERVED                                                      #
# --------------------------------------------------------------------------- #


class TestPackageDataPreserved:
    def test_package_data_preserved(self):
        text = PYPROJECT.read_text(encoding="utf-8")
        assert '"fmindex.dashboard"' in text
        assert '"templates/*.html"' in text
        assert '"static/css/*.css"' in text
        assert '"static/js/*.js"' in text
        # dashboard assets themselves must still exist in the source tree.
        assert TEMPLATE.exists()
        assert CSS.exists()
        assert JS.exists()
