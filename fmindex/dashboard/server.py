"""Chart.js dashboard server — serves the FMIndex dashboard."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional

KST = timezone(timedelta(hours=9))


def filter_by_period(data: List[Dict[str, Any]], period: str) -> List[Dict[str, Any]]:
    """Filter joined records by timestamp-based period.

    Args:
        data: List of joined records with 'timestamp' field.
        period: '24h', '7d', '30d', or 'all'.

    Returns:
        Filtered list of records.
    """
    if period == "all":
        return data

    now = datetime.now(KST)
    if period == "24h":
        cutoff = now - timedelta(hours=24)
    elif period == "7d":
        cutoff = now - timedelta(days=7)
    elif period == "30d":
        cutoff = now - timedelta(days=30)
    else:
        return data

    result = []
    for rec in data:
        ts = rec.get("timestamp", "")
        try:
            rec_dt = datetime.fromisoformat(ts)
            if rec_dt.tzinfo is None:
                rec_dt = rec_dt.replace(tzinfo=KST)
            else:
                rec_dt = rec_dt.astimezone(KST)
            if rec_dt >= cutoff:
                result.append(rec)
        except (ValueError, TypeError):
            continue

    return result


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FMIndex — 펨코지수 대시보드</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
:root {
  --bg: #0f1117; --fg: #e4e6eb; --card: #1a1d27; --border: #2a2d3a;
  --accent: #4488ff; --green: #44bb44; --red: #ff4444; --yellow: #ffaa00;
  --muted: #888; --radius: 8px;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: 'Malgun Gothic','Segoe UI',sans-serif; background: var(--bg); color: var(--fg); padding: 16px; }
h1 { font-size: 18px; margin-bottom: 12px; }
h2 { font-size: 14px; margin: 16px 0 8px; color: var(--muted); }
.cards { display: grid; grid-template-columns: repeat(auto-fill,minmax(180px,1fr)); gap: 10px; margin-bottom: 16px; }
.card { background: var(--card); border: 1px solid var(--border); border-radius: var(--radius); padding: 12px 14px; }
.card .label { font-size: 11px; color: var(--muted); margin-bottom: 4px; }
.card .value { font-size: 22px; font-weight: bold; }
.card .sub { font-size: 11px; color: var(--muted); margin-top: 2px; }
.card .value.green { color: var(--green); }
.card .value.red { color: var(--red); }
.card .value.yellow { color: var(--yellow); }
.chart-container { background: var(--card); border: 1px solid var(--border); border-radius: var(--radius); padding: 16px; margin-bottom: 16px; }
.chart-container canvas { max-height: 320px; }
.period-btns { display: flex; gap: 6px; margin-bottom: 10px; }
.period-btns button { background: var(--card); border: 1px solid var(--border); color: var(--fg); padding: 4px 12px; border-radius: 4px; cursor: pointer; font-size: 12px; }
.period-btns button.active { background: var(--accent); border-color: var(--accent); }
.eval-card { background: var(--card); border: 1px solid var(--border); border-radius: var(--radius); padding: 14px; margin-bottom: 16px; }
.eval-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.eval-row { display: flex; justify-content: space-between; font-size: 13px; padding: 3px 0; }
.eval-row .match { color: var(--green); }
.eval-row .nomatch { color: var(--red); }
.badge { display: inline-block; font-size: 11px; padding: 2px 8px; border-radius: 10px; margin-left: 8px; vertical-align: middle; }
.badge.sample { background: #3a3320; color: var(--yellow); border: 1px solid #5a4d2a; }
.badge.real { background: #14331c; color: var(--green); border: 1px solid #245a2e; }
.empty { text-align: center; padding: 40px; color: var(--muted); }
</style>
</head>
<body>
<h1>📊 FMIndex — 펨코지수 대시보드<span id="modeBadge"></span></h1>
<div id="dashboard"></div>
<script>
let chart = null;
let allData = [];
let currentPeriod = '24h';

async function loadData() {
  try {
    const res = await fetch('/api/data.json');
    allData = await res.json();
    render();
  } catch(e) {
    document.getElementById('dashboard').innerHTML = '<div class="empty">데이터를 불올 수 없습니다. pipeline을 먼저 실행하세요.</div>';
  }
}

function filterByPeriod(data, period) {
  if (period === 'all') return data;
  const now = new Date();
  let cutoff;
  if (period === '24h') {
    cutoff = new Date(now.getTime() - 24 * 60 * 60 * 1000);
  } else if (period === '7d') {
    cutoff = new Date(now.getTime() - 7 * 24 * 60 * 60 * 1000);
  } else if (period === '30d') {
    cutoff = new Date(now.getTime() - 30 * 24 * 60 * 60 * 1000);
  } else {
    return data;
  }
  return data.filter(d => new Date(d.timestamp) >= cutoff);
}

function render() {
  const joined = allData.joined || [];
  const overnight = allData.overnight || null;
  const summary = allData.summary || {};

  const dataMode = summary.dataMode || 'unknown';
  const modeBadge = document.getElementById('modeBadge');
  if (modeBadge) {
    modeBadge.textContent = dataMode === 'sample' ? '샘플 데이터' : dataMode === 'real' ? '실시간 데이터' : dataMode;
    modeBadge.className = 'badge ' + (dataMode === 'sample' ? 'sample' : dataMode === 'real' ? 'real' : '');
  }

  if (joined.length === 0) {
    document.getElementById('dashboard').innerHTML = '<div class="empty">데이터가 없습니다.</div>';
    return;
  }
  const filtered = filterByPeriod(joined, currentPeriod);
  const latest = filtered[filtered.length - 1] || {};
  const prev = filtered[filtered.length - 2] || {};

  const latestFm = latest.fmIndex;
  const prevFm = prev.fmIndex;
  const fmChange = (latestFm != null && prevFm != null) ? (latestFm - prevFm).toFixed(2) : '0.00';

  let html = '<div class="cards">';
  html += card('현재 펨코지수', latestFm != null ? latestFm.toFixed(1) : '-', '', '');
  html += card('코스피 정규화', latest.marketNormalized != null ? latest.marketNormalized.toFixed(2) : '-', '', '', '기준=100');
  html += card('코스피 변동률', latest.marketChangeRate != null ? latest.marketChangeRate.toFixed(2) + '%' : '-', '', '');
  html += card('분석 게시글', latest.postCount != null ? latest.postCount : '0', '', '', '신뢰도: ' + (latest.confidence != null ? (latest.confidence * 100).toFixed(0) + '%' : '-'));
  html += card('마지막 데이터', latest.timestamp ? latest.timestamp.slice(11, 16) : '-', '', '', latest.timestamp ? latest.timestamp.slice(0, 10) : '');
  html += '</div>';

  html += '<div class="period-btns">';
  for (const p of ['24h','7d','30d','all']) {
    html += '<button class="' + (currentPeriod === p ? 'active' : '') + '" onclick="setPeriod(\\''+p+'\\')">' + p + '</button>';
  }
  html += '</div>';

  html += '<div class="chart-container"><canvas id="mainChart"></canvas></div>';

  if (overnight) {
    html += '<h2>야간 심리 vs 다음 장 결과</h2>';
    html += '<div class="eval-card"><div class="eval-grid">';
    html += '<div><div class="eval-row"><span>야간 상태</span><b>' + (overnight.overnight.status || '-') + '</b></div>';
    html += '<div class="eval-row"><span>사유</span><b>' + (overnight.overnight.reason || '-') + '</b></div></div>';
    html += '</div></div>';
  }

  if (summary && summary.totalAnalyzed) {
    html += '<h2>표본과 신뢰도</h2>';
    html += '<div class="cards">';
    html += card('분석 글', summary.totalPosts||0, '', '', '');
    html += card('분석 댓글', summary.totalComments||0, '', '', '');
    html += card('유효 판정', summary.totalAnalyzed||0, '', '', '');
    html += card('평균 신뢰도', summary.avgConfidence ? (summary.avgConfidence*100).toFixed(0)+'%' : '-', '', '', '');
    html += '</div>';
  }

  document.getElementById('dashboard').innerHTML = html;
  renderChart(filtered);
}

function card(label, value, cls, sub) {
  return '<div class="card"><div class="label">'+label+'</div><div class="value '+(cls||'')+'\">'+value+'</div>'+(sub?'<div class="sub">'+sub+'</div>':'')+'</div>';
}

function setPeriod(p) { currentPeriod = p; render(); }

function renderChart(data) {
  const ctx = document.getElementById('mainChart');
  if (!ctx) return;
  const labels = data.map(d => d.timestamp ? d.timestamp.slice(11,16) : '');
  // null fmIndex must be kept as null so Chart.js renders a gap (spanGaps: false).
  const fmData = data.map(d => d.fmIndex);
  const mkData = data.map(d => d.marketNormalized != null ? d.marketNormalized : 100);

  if (chart) chart.destroy();
  chart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: labels,
      datasets: [
        { label: '펨코지수', data: fmData, borderColor: '#4488ff', backgroundColor: 'rgba(68,136,255,0.1)', yAxisID: 'y', tension: 0.3, fill: true, spanGaps: false },
        { label: '코스피(정규화)', data: mkData, borderColor: '#ffaa00', backgroundColor: 'rgba(255,170,0,0.1)', yAxisID: 'y1', tension: 0.3, fill: false, spanGaps: false }
      ]
    },
    options: {
      responsive: true,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        tooltip: { callbacks: {
          label: function(ctx) {
            const d = data[ctx.dataIndex];
            let s = ctx.dataset.label + ': ' + (ctx.parsed.y != null ? ctx.parsed.y.toFixed(2) : 'N/A');
            if (d && d.marketChangeRate != null) s += ' (코스피 ' + d.marketChangeRate.toFixed(2) + '%)';
            if (d && d.postCount != null) s += ' | 게시글 ' + d.postCount;
            return s;
          }
        }}
      },
      scales: {
        x: { ticks: { color: '#888', maxTicksLimit: 12 } },
        y: { position: 'left', min: 0, max: 100, title: { display: true, text: '펨코지수', color: '#4488ff' }, ticks: { color: '#4488ff' } },
        y1: { position: 'right', title: { display: true, text: '코스피', color: '#ffaa00' }, ticks: { color: '#ffaa00' }, grid: { drawOnChartArea: false } }
      }
    }
  });
}

loadData();
</script>
</body>
</html>"""


def generate_dashboard_data(
    joined: List[Dict[str, Any]],
    overnight: Optional[Dict[str, Any]] = None,
    summary: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Generate the dashboard data JSON."""
    return {
        "joined": joined,
        "overnight": overnight,
        "summary": summary or {},
    }


def write_dashboard_files(
    output_dir: str,
    joined: List[Dict[str, Any]],
    overnight: Optional[Dict[str, Any]] = None,
    summary: Optional[Dict[str, Any]] = None,
) -> str:
    """Write dashboard HTML and data JSON to output directory.

    Returns the path to the HTML file.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    data = generate_dashboard_data(joined, overnight, summary)
    data_path = out / "api" / "data.json"
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    html_path = out / "index.html"
    html_path.write_text(DASHBOARD_HTML, encoding="utf-8")

    return str(html_path)


def serve_dashboard(directory: str, port: int = 8420, host: str = "127.0.0.1") -> None:
    """Serve the dashboard directory on localhost."""
    os.chdir(directory)

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=directory, **kwargs)

    print(f"FMIndex dashboard: http://localhost:{port}")
    HTTPServer((host, port), Handler).serve_forever()
