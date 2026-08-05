/* ======================================================================
   FM INDEX — 시장심리 대시보드
   Chart-first light dashboard. Pure functions are exported for node tests.
   ====================================================================== */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.FMIndexUI = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  /* ---------------------------------------------------------------
     Settings & persistence (storage injectable for tests)
     --------------------------------------------------------------- */
  var STORAGE_KEYS = {
    theme: 'fmindex.theme',
    colorMode: 'fmindex.colorMode',
    customUp: 'fmindex.customUp',
    customDown: 'fmindex.customDown',
  };

  var COLOR_MODE_DEFAULTS = {
    kr:  { up: '#e53935', down: '#1e63c7' },
    us:  { up: '#2e9e5b', down: '#e53935' },
  };

  function defaultSettings() {
    return {
      theme: 'light',
      colorMode: 'kr',
      customUp: '#e53935',
      customDown: '#1e63c7',
      market: 'KOSPI',
      period: '24h',
      view: 'sync',
    };
  }

  function readSettings(storage) {
    var s = defaultSettings();
    if (!storage) return s;
    try {
      s.theme = storage.getItem(STORAGE_KEYS.theme) || s.theme;
      s.colorMode = storage.getItem(STORAGE_KEYS.colorMode) || s.colorMode;
      var cu = storage.getItem(STORAGE_KEYS.customUp);
      var cd = storage.getItem(STORAGE_KEYS.customDown);
      if (cu) s.customUp = cu;
      if (cd) s.customDown = cd;
    } catch (e) { /* storage unavailable */ }
    if (s.theme !== 'light' && s.theme !== 'dark') s.theme = 'light';
    if (s.colorMode !== 'kr' && s.colorMode !== 'us' && s.colorMode !== 'custom') s.colorMode = 'kr';
    return s;
  }

  function saveSettings(storage, settings) {
    if (!storage) return;
    try {
      storage.setItem(STORAGE_KEYS.theme, settings.theme);
      storage.setItem(STORAGE_KEYS.colorMode, settings.colorMode);
      storage.setItem(STORAGE_KEYS.customUp, settings.customUp);
      storage.setItem(STORAGE_KEYS.customDown, settings.customDown);
    } catch (e) { /* storage unavailable */ }
  }

  // URL query-param deep link: ?theme=dark&market=NASDAQ&colors=us&period=30d&view=divergence
  function readUrlParams(search) {
    var p = search || (typeof location !== 'undefined' ? location.search : '');
    var out = {};
    if (!p) return out;
    try {
      var qs = new URLSearchParams(p);
      var theme = qs.get('theme');
      if (theme === 'light' || theme === 'dark') out.theme = theme;
      var colors = qs.get('colors');
      if (colors === 'kr' || colors === 'us' || colors === 'custom') out.colorMode = colors;
      var market = qs.get('market');
      if (market === 'KOSPI' || market === 'NASDAQ') out.market = market;
      var period = qs.get('period');
      if (period === '24h' || period === '7d' || period === '30d' || period === 'all') out.period = period;
      var view = qs.get('view');
      if (view === 'sync' || view === 'lead' || view === 'divergence') out.view = view;
      var up = qs.get('up');
      var down = qs.get('down');
      if (up && /^#[0-9a-fA-F]{6}$/.test(up)) out.customUp = up;
      if (down && /^#[0-9a-fA-F]{6}$/.test(down)) out.customDown = down;
    } catch (e) { /* URLSearchParams unavailable */ }
    return out;
  }

  function upDownColors(settings) {
    var c = COLOR_MODE_DEFAULTS[settings.colorMode] || COLOR_MODE_DEFAULTS.kr;
    if (settings.colorMode === 'custom') {
      c = { up: settings.customUp || COLOR_MODE_DEFAULTS.kr.up,
            down: settings.customDown || COLOR_MODE_DEFAULTS.kr.down };
    }
    return c;
  }

  /* ---------------------------------------------------------------
     Pure data logic (node-testable)
     --------------------------------------------------------------- */

  // timestamp 기준 기간 필터 (레코드 개수 기준 아님)
  // 컷오프는 벽시계가 아니라 입력 데이터의 최신 유효 timestamp를 앵커로 사용한다.
  // 서버 filter_by_period와 동일한 경계 규칙: timestamp >= cutoff
  function filterByPeriod(data, period) {
    if (!period || period === 'all') return data;
    var hours = period === '24h' ? 24 : period === '7d' ? 168 : period === '30d' ? 720 : null;
    if (hours === null) return data;
    var valid = [];
    data.forEach(function (d) {
      var t = new Date(d.timestamp);
      if (!isNaN(t.getTime())) valid.push({ t: t, d: d });
    });
    if (!valid.length) return [];
    var latest = Math.max.apply(null, valid.map(function (v) { return v.t.getTime(); }));
    var cutoff = latest - hours * 60 * 60 * 1000;
    return valid
      .filter(function (v) { return v.t.getTime() >= cutoff; })
      .map(function (v) { return v.d; });
  }

  // 선택 기간 첫 close 기준 100 정규화
  function normalizeMarket(data) {
    if (!data.length) return [];
    var base = data[0].marketNormalized;
    if (!base || base === 0) base = 100;
    return data.map(function (d) {
      var v = d.marketNormalized;
      return v != null ? Math.round((v / base) * 10000) / 100 : null;
    });
  }

  // 시장 시계열: NASDAQ은 실제 시계열이 없으므로 KOSPI 값을 재라벨링하지 않고
  // null(unavailable)을 반환한다. KOSPI는 기존 정규화 시계열을 사용한다.
  function marketSeriesFor(joined, market) {
    if (market === 'NASDAQ') return (joined || []).map(function () { return null; });
    return normalizeMarket(joined || []);
  }

  // 기간 필터를 통과한 source를 기준으로 차트 시리즈를 구성한다.
  // 시장선도 펨코지수와 동일한 기간 필터(built.source)를 통과해야 시간축이 정렬된다.
  function buildChartSeries(joined, period, market) {
    var built = buildChartData(joined, period);
    return {
      labels: built.labels,
      fmData: built.fmData,
      marketData: marketSeriesFor(built.source, market),
      source: built.source
    };
  }

  function buildChartData(joined, period) {
    var filtered = filterByPeriod(joined, period);
    var labels = filtered.map(function (d) { return d.timestamp ? d.timestamp.slice(0, 16) : ''; });
    // null은 null로 유지 (gap), 0은 실제 0으로 유지
    var fmData = filtered.map(function (d) { return d.fmIndex != null ? d.fmIndex : null; });
    var marketData = normalizeMarket(filtered);
    return { labels: labels, fmData: fmData, marketData: marketData, source: filtered };
  }

  // 괴리 구간: 펨코 방향(>=50 상승) vs 시장 방향(변동률 부호)이 다른 지점
  function buildDivergence(joined, period) {
    var filtered = filterByPeriod(joined, period);
    var pts = [];
    filtered.forEach(function (d, i) {
      if (d.fmIndex == null || d.marketChangeRate == null) return;
      var fmDir = d.fmIndex >= 50 ? 1 : d.fmIndex < 50 ? -1 : 0;
      var mkDir = d.marketChangeRate > 0 ? 1 : d.marketChangeRate < 0 ? -1 : 0;
      if (fmDir !== 0 && mkDir !== 0 && fmDir !== mkDir) {
        pts.push({ x: i, y: d.fmIndex, fm: d.fmIndex, change: d.marketChangeRate });
      }
    });
    return pts;
  }

  function sentimentLabel(fm) {
    if (fm == null) return '데이터 없음';
    if (fm >= 75) return '매우 긍정';
    if (fm >= 60) return '긍정';
    if (fm > 40) return '중립';
    if (fm > 25) return '부정';
    return '강한 부정';
  }

  function sentimentClass(fm) {
    if (fm == null) return 'flat';
    if (fm >= 60) return 'up';
    if (fm <= 40) return 'down';
    return 'flat';
  }

  function fmChangeText(latest, prev) {
    if (!latest || latest.fmIndex == null) return '지난 1시간 –';
    if (!prev || prev.fmIndex == null) return '지난 1시간 –';
    var delta = Math.round((latest.fmIndex - prev.fmIndex) * 10) / 10;
    return '지난 1시간 ' + (delta > 0 ? '+' : '') + delta.toFixed(1);
  }

  function escapeHtml(str) {
    return String(str == null ? '' : str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function sampleBadgeVisible(dataMode) {
    return dataMode === 'sample';
  }

  // 오늘의 해석 — 데이터에서 파생된 문장 (가짜 수치 아님)
  function interpretToday(latest, summary) {
    if (!latest || latest.fmIndex == null) {
      return '아직 심리 데이터가 없습니다. 파이프라인이 데이터를 생성하면 여기에 해석이 표시됩니다.';
    }
    var label = sentimentLabel(latest.fmIndex);
    var parts = ['현재 펨코지수는 ' + latest.fmIndex.toFixed(1) + '로 ' + label + ' 구간입니다.'];
    if (summary && summary.totalAnalyzed) {
      parts.push('오늘 분석 표본은 ' + summary.totalAnalyzed + '건, 평균 신뢰도는 ' +
        Math.round((summary.avgConfidence || 0) * 100) + '%입니다.');
    }
    if (summary && summary.dataMode === 'sample') {
      parts.push('현재 화면은 샘플 데이터로 구조 검증용입니다. 실제 시장 데이터로 오인하지 마세요.');
    }
    return parts.join(' ');
  }

  // 야간(국내) / 주간(미국) 패널 — 실제 세션 데이터 없으면 unavailable 문구만
  function sessionPanelHtml(market, overnight, summary) {
    var overnightStatus = (overnight && overnight.overnight && overnight.overnight.status) || 'unavailable';
    if (market === 'NASDAQ') {
      var title = '한국 주간 심리와 미국장 결과';
      var cells = [
        ['한국시간 주간 펨코심리', '–'],
        ['미국장 개장', '–'],
        ['장초', '–'],
        ['종가', '–'],
        ['방향 일치', '–'],
      ];
      var note = '실제 나스닥 세션 데이터 연동 전입니다. 현재 화면은 구조 검증용 샘플이며, 실제 수치로 표시하지 않습니다.';
      return renderSessionPanel(title, cells, note, overnightStatus);
    }
    var title2 = '야간 심리와 다음 국내장';
    var cells2 = [
      ['국내장 마감 후~다음 개장 전 야간 심리', '–'],
      ['야간선물 데이터', '–'],
      ['다음 날 시가', '–'],
      ['장초 1시간', '–'],
      ['종가', '–'],
      ['방향 일치 여부', '–'],
    ];
    var note2 = '실제 거래일·야간선물 데이터 연동 전입니다. 현재 화면은 구조 검증용 샘플입니다.';
    return renderSessionPanel(title2, cells2, note2, overnightStatus);
  }

  function renderSessionPanel(title, cells, note, overnightStatus) {
    var statusBadge = overnightStatus === 'unavailable'
      ? '<span class="badge badge-unavailable">UNAVAILABLE</span>'
      : '<span class="badge badge-sample">' + escapeHtml(overnightStatus) + '</span>';
    var rows = cells.map(function (c) {
      return '<div class="session-cell"><div class="session-cell-label">' + escapeHtml(c[0]) +
        '</div><div class="session-cell-value">' + escapeHtml(c[1]) + '</div></div>';
    }).join('');
    return '<div class="session-title">' + escapeHtml(title) + ' ' + statusBadge + '</div>' +
      '<div class="session-grid">' + rows + '</div>' +
      '<p class="session-note">' + escapeHtml(note) + '</p>';
  }

  function directionMatchCardsHtml() {
    // 실제 계산 데이터가 없으면 대시보드는 — / 데이터 축적 중 / 유효 표본 0건 표시
    var items = ['다음 시가 방향 일치율', '장초 1시간 방향 일치율', '당일 종가 방향 일치율', '평균 선행 시간'];
    return items.map(function (label) {
      return '<div class="match-card card"><div class="match-label">' + escapeHtml(label) +
        '</div><div class="match-value">—</div>' +
        '<div class="match-sub">데이터 축적 중 · 유효 표본 0건</div></div>';
    }).join('');
  }

  // ------------------------------------------------------------------
  // Browser-only wiring (guarded so node require is safe)
  // ------------------------------------------------------------------
  if (typeof window !== 'undefined' && typeof document !== 'undefined') {
    (function () {
      var settings = readSettings(window.localStorage);
      var chart = null;
      var data = null;

      function applyTheme(theme) {
        document.documentElement.setAttribute('data-theme', theme);
        var sel = document.getElementById('themeSelect');
        if (sel) sel.value = theme;
        var btn = document.getElementById('themeToggle');
        if (btn) {
          btn.textContent = theme === 'dark' ? '☀️' : '🌙';
          btn.setAttribute('aria-pressed', theme === 'dark' ? 'true' : 'false');
          btn.setAttribute('aria-label', theme === 'dark' ? '라이트 모드로 전환' : '다크 모드로 전환');
        }
      }

      function applyColors() {
        var c = upDownColors(settings);
        var root = document.documentElement.style;
        root.setProperty('--up', c.up);
        root.setProperty('--down', c.down);
        root.setProperty('--up-soft', hexToRgba(c.up, 0.10));
        root.setProperty('--down-soft', hexToRgba(c.down, 0.10));
        var customRow = document.getElementById('customColorRow');
        if (customRow) customRow.classList.toggle('is-hidden', settings.colorMode !== 'custom');
        var cm = document.getElementById('colorModeSelect');
        if (cm) cm.value = settings.colorMode;
        var cu = document.getElementById('customUpColor');
        var cd = document.getElementById('customDownColor');
        if (cu) cu.value = settings.customUp;
        if (cd) cd.value = settings.customDown;
      }

      function hexToRgba(hex, alpha) {
        var m = /^#([0-9a-fA-F]{6})$/.exec(hex || '');
        if (!m) return 'rgba(0,0,0,0.1)';
        var n = parseInt(m[1], 16);
        return 'rgba(' + ((n >> 16) & 255) + ',' + ((n >> 8) & 255) + ',' + (n & 255) + ',' + alpha + ')';
      }

      function cssVar(name) {
        return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || '#888';
      }

      function switchSection(name) {
        document.querySelectorAll('.section').forEach(function (sec) {
          sec.classList.toggle('is-active', sec.id === 'section-' + name);
          sec.hidden = sec.id !== 'section-' + name;
        });
        document.querySelectorAll('.tab').forEach(function (t) {
          var active = t.getAttribute('data-section') === name;
          t.classList.toggle('is-active', active);
          if (active) t.setAttribute('aria-current', 'page'); else t.removeAttribute('aria-current');
        });
        document.querySelectorAll('.mobile-nav button').forEach(function (b) {
          b.classList.toggle('is-active', b.getAttribute('data-section') === name);
        });
      }

      function setControl(groupAttr, valueAttr, value) {
        document.querySelectorAll('[' + groupAttr + ']').forEach(function (b) {
          var active = b.getAttribute(valueAttr) === value;
          b.classList.toggle('is-active', active);
          b.setAttribute('aria-pressed', active ? 'true' : 'false');
        });
      }

      function renderStatus(summary) {
        function set(id, v) {
          var el = document.getElementById(id);
          if (el) el.textContent = v;
        }
        set('statusDataMode', summary.dataMode || 'unknown');
        set('dsDataMode', summary.dataMode || 'unknown');
        var source = (summary.instrument || '') + (summary.symbol && summary.symbol !== summary.instrument ? ' · ' + summary.symbol : '');
        set('statusMarketSource', source || '—');
        set('dsMarketSource', source || '—');
        set('statusSelectedMarket', settings.market);
        set('dsSelectedMarket', settings.market);
        set('statusProvider', summary.provider || '—');
        set('dsProvider', summary.provider || '—');
        set('statusMethodology', summary.methodologyVersion || '—');
        set('dsMethodology', summary.methodologyVersion || '—');
        var overnight = data && data.overnight && data.overnight.overnight;
        var overnightText = overnight ? (overnight.status + ' · ' + overnight.reason) : 'unavailable';
        set('statusOvernight', overnightText);
        set('dsOvernight', overnightText);
        set('statusUpdated', summary.lastUpdated || '—');
        set('dsUpdated', summary.lastUpdated || '—');
      }

      function render() {
        if (!data) return;
        var joined = data.joined || [];
        var summary = data.summary || {};
        var overnight = data.overnight || null;

        var filtered = filterByPeriod(joined, settings.period);
        var latest = filtered[filtered.length - 1] || {};
        var prev = filtered[filtered.length - 2] || {};
        var colors = upDownColors(settings);

        // SAMPLE DATA 배지
        var badge = document.getElementById('sampleBadge');
        var isSample = sampleBadgeVisible(summary.dataMode);
        if (badge) badge.classList.toggle('is-hidden', !isSample);

        // 현재 지수 카드
        var fmEl = document.getElementById('currentFmIndex');
        if (fmEl) fmEl.textContent = latest.fmIndex != null ? latest.fmIndex.toFixed(1) : '–';
        var sentEl = document.getElementById('currentSentiment');
        if (sentEl) {
          sentEl.textContent = sentimentLabel(latest.fmIndex);
          sentEl.className = 'index-hero-sentiment ' + sentimentClass(latest.fmIndex);
        }
        var subEl = document.getElementById('currentFmChange');
        if (subEl) subEl.textContent = fmChangeText(latest, prev);
        var statUpdated = document.getElementById('statUpdated');
        if (statUpdated) statUpdated.textContent = latest.timestamp ? latest.timestamp.slice(5, 16).replace('T', ' ') : '–';
        var statMarket = document.getElementById('statMarket');
        if (statMarket) statMarket.textContent = settings.market + ' · ' + (summary.dataMode || 'unknown');
        var statSample = document.getElementById('statSample');
        if (statSample) statSample.textContent = (summary.totalPosts != null ? summary.totalPosts : (latest.postCount != null ? latest.postCount : '–')) + ' 글';
        var statAnalyzed = document.getElementById('statAnalyzed');
        if (statAnalyzed) statAnalyzed.textContent = '유효 판정 ' + (summary.totalAnalyzed != null ? summary.totalAnalyzed : '–');
        var statConf = document.getElementById('statConfidence');
        if (statConf) statConf.textContent = summary.avgConfidence != null ? Math.round(summary.avgConfidence * 100) + '%' : (latest.confidence != null ? Math.round(latest.confidence * 100) + '%' : '–');
        var hasSentiment = joined.some(function (j) { return j.hasSentiment === true || j.hasSentiment === 'true' || j.hasSentiment === 1; });
        var statSent = document.getElementById('statSentiment');
        if (statSent) statSent.textContent = hasSentiment ? '존재' : '없음';
        var dsSent = document.getElementById('dsSentimentPresence');
        if (dsSent) dsSent.textContent = hasSentiment ? '존재' : '없음';
        var statusSent = document.getElementById('statusSentiment');
        if (statusSent) statusSent.textContent = hasSentiment ? '존재' : '없음';

        // 해석
        var interp = document.getElementById('todayInterpretation');
        if (interp) interp.textContent = interpretToday(latest, summary);

        // 방향 일치 카드 (항상 미축적 상태)
        var matchContainer = document.querySelector('.match-grid');
        if (matchContainer && !matchContainer.getAttribute('data-rendered')) {
          matchContainer.innerHTML = directionMatchCardsHtml();
          matchContainer.setAttribute('data-rendered', 'true');
        }

        // 세션 패널 (코스피 → 야간, 나스닥 → 미국장)
        var sessionEl = document.getElementById('sessionPanel');
        if (sessionEl) sessionEl.innerHTML = sessionPanelHtml(settings.market, overnight, summary);

        // 차트 타이틀
        var chartTitle = document.getElementById('chartTitle');
        var viewLabel = settings.view === 'lead' ? '선행' : settings.view === 'divergence' ? '괴리' : '동행';
        var marketLabel = settings.market === 'NASDAQ' ? '나스닥 (미연동)' : '코스피 (정규화)';
        if (chartTitle) chartTitle.textContent = '펨코지수 · ' + marketLabel + ' (' + viewLabel + ')';
        var marketLegend = document.getElementById('marketLegend');
        if (marketLegend) marketLegend.textContent = marketLabel;

        renderChart();
        renderViewNotice();
        renderStatus(summary);
        updateChartStatus();
      }

      function renderViewNotice() {
        var el = document.getElementById('viewNotice');
        if (!el) return;
        if (settings.view === 'lead') {
          el.classList.remove('is-hidden');
          el.textContent = '선행 관계는 실제 세션 데이터가 축적된 후 계산됩니다. 현재는 실데이터 계산 준비 중입니다.';
        } else if (settings.view === 'divergence') {
          var pts = buildDivergence(data.joined || [], settings.period);
          el.classList.remove('is-hidden');
          el.textContent = pts.length
            ? '괴리 구간 ' + pts.length + '곳을 마커로 표시했습니다 (샘플 기준).'
            : '현재 구간에서 산출 가능한 괴리 지점이 없습니다. 가짜 수치를 표시하지 않습니다.';
        } else {
          el.classList.add('is-hidden');
        }
      }

      function updateChartStatus() {
        var el = document.getElementById('chartStatus');
        if (!el) return;
        var marketText = settings.market === 'NASDAQ' ? '나스닥' : '코스피';
        var periodText = settings.period === 'all' ? '전체' : settings.period;
        var modeText = data && data.summary ? (data.summary.dataMode || 'unavailable') : 'unavailable';
        if (settings.market === 'NASDAQ') {
          el.textContent = marketText + ' · 기간 ' + periodText + ' · 시장선 unavailable (나스닥 데이터 연동 전)';
        } else {
          el.textContent = marketText + ' · 기간 ' + periodText + ' · 데이터 ' + modeText;
        }
      }

      function renderChart() {
        var ctx = document.getElementById('mainChart');
        if (!ctx || typeof Chart === 'undefined') return;
        var built = buildChartSeries(data.joined || [], settings.period, settings.market);
        var labels = built.labels;
        var fmData = built.fmData;
        var marketData = built.marketData;

        var fmColor = cssVar('--fm') || '#6a4dff';
        var marketColor = cssVar('--market') || '#e8930c';

        var datasets = [
          {
            label: '펨코지수',
            data: fmData,
            borderColor: fmColor,
            backgroundColor: hexToRgba(fmColor, 0.10),
            yAxisID: 'y',
            tension: 0.3,
            fill: true,
            spanGaps: false,
            pointRadius: 0,
          },
          {
            label: settings.market === 'NASDAQ' ? '나스닥 (미연동)' : '코스피 (정규화)',
            data: marketData,
            borderColor: marketColor,
            backgroundColor: hexToRgba(marketColor, 0.10),
            yAxisID: 'y1',
            tension: 0.3,
            fill: false,
            spanGaps: false,
            pointRadius: 0,
          },
        ];

        if (settings.view === 'divergence') {
          var pts = buildDivergence(data.joined || [], settings.period);
          var colors = upDownColors(settings);
          datasets.push({
            label: '괴리 지점',
            data: pts.map(function (p) { return { x: labels[p.x], y: p.y }; }),
            type: 'scatter',
            yAxisID: 'y',
            backgroundColor: hexToRgba(colors.down, 0.9),
            borderColor: colors.down,
            pointRadius: 5,
            showLine: false,
          });
        }

        if (chart) chart.destroy();
        chart = new Chart(ctx, {
          type: 'line',
          data: { labels: labels, datasets: datasets },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            plugins: {
              tooltip: {
                callbacks: {
                  label: function (item) {
                    var d = built.source[item.dataIndex] || {};
                    if (item.dataset.type === 'scatter') {
                      return '괴리: 펨코 ' + item.parsed.y + ' · 시장변동 ' + d.marketChangeRate + '%';
                    }
                    var v = item.parsed.y;
                    var s = item.dataset.label + ': ' + (v != null ? v.toFixed(2) : 'N/A');
                    if (settings.market !== 'NASDAQ' && d.marketChangeRate != null) s += ' (변동 ' + d.marketChangeRate.toFixed(2) + '%)';
                    if (d.postCount != null) s += ' | 게시글 ' + d.postCount;
                    return s;
                  },
                },
              },
            },
            scales: {
              x: { ticks: { maxTicksLimit: 10, color: cssVar('--muted') }, grid: { color: 'transparent' } },
              y: {
                position: 'left', min: 0, max: 100,
                title: { display: true, text: '펨코지수', color: fmColor },
                ticks: { color: fmColor },
              },
              y1: {
                position: 'right',
                title: { display: true, text: '시장(정규화)', color: marketColor },
                ticks: { color: marketColor },
                grid: { drawOnChartArea: false },
              },
            },
          },
        });
      }

      function wireEvents() {
        document.querySelectorAll('.tab, .mobile-nav button[data-section]').forEach(function (t) {
          t.addEventListener('click', function () { switchSection(t.getAttribute('data-section')); });
        });

        document.querySelectorAll('[data-market]').forEach(function (b) {
          b.addEventListener('click', function () {
            settings.market = b.getAttribute('data-market');
            setControl('data-market', 'data-market', settings.market);
            render();
          });
        });

        document.querySelectorAll('[data-period]').forEach(function (b) {
          b.addEventListener('click', function () {
            settings.period = b.getAttribute('data-period');
            setControl('data-period', 'data-period', settings.period);
            render();
          });
        });

        document.querySelectorAll('[data-view]').forEach(function (b) {
          b.addEventListener('click', function () {
            settings.view = b.getAttribute('data-view');
            setControl('data-view', 'data-view', settings.view);
            render();
          });
        });

        var themeToggle = document.getElementById('themeToggle');
        if (themeToggle) themeToggle.addEventListener('click', function () {
          settings.theme = settings.theme === 'dark' ? 'light' : 'dark';
          applyTheme(settings.theme);
          saveSettings(window.localStorage, settings);
          renderChart();
        });

        var settingsToggle = document.getElementById('settingsToggle');
        var panel = document.getElementById('settingsPanel');
        function setPanelOpen(open) {
          if (!panel) return;
          panel.classList.toggle('is-hidden', !open);
          if (settingsToggle) settingsToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
          if (!open && settingsToggle) settingsToggle.focus();
        }
        if (settingsToggle && panel) settingsToggle.addEventListener('click', function () {
          setPanelOpen(panel.classList.contains('is-hidden'));
        });
        document.addEventListener('keydown', function (e) {
          if (e.key === 'Escape' && panel && !panel.classList.contains('is-hidden')) {
            setPanelOpen(false);
          }
        });

        var themeSelect = document.getElementById('themeSelect');
        if (themeSelect) themeSelect.addEventListener('change', function () {
          settings.theme = themeSelect.value;
          applyTheme(settings.theme);
          saveSettings(window.localStorage, settings);
          renderChart();
        });

        var colorModeSelect = document.getElementById('colorModeSelect');
        if (colorModeSelect) colorModeSelect.addEventListener('change', function () {
          settings.colorMode = colorModeSelect.value;
          applyColors();
          saveSettings(window.localStorage, settings);
          render();
        });

        var cu = document.getElementById('customUpColor');
        var cd = document.getElementById('customDownColor');
        if (cu) cu.addEventListener('input', function () {
          settings.customUp = cu.value;
          if (settings.colorMode === 'custom') { applyColors(); saveSettings(window.localStorage, settings); render(); }
        });
        if (cd) cd.addEventListener('input', function () {
          settings.customDown = cd.value;
          if (settings.colorMode === 'custom') { applyColors(); saveSettings(window.localStorage, settings); render(); }
        });
      }

      function load() {
        var params = readUrlParams();
        Object.keys(params).forEach(function (k) { settings[k] = params[k]; });
        applyTheme(settings.theme);
        applyColors();
        wireEvents();
        switchSection('today');
        fetch('/api/data.json')
          .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
          .then(function (json) { data = json; render(); })
          .catch(function (e) {
            var el = document.getElementById('todayInterpretation');
            if (el) el.textContent = '데이터를 불러오지 못했습니다. pipeline을 먼저 실행하세요. (' + e.message + ')';
          });
      }

      if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', load);
      } else {
        load();
      }
    })();
  }

  /* ---------------- Public API (node tests) ---------------- */
  return {
    STORAGE_KEYS: STORAGE_KEYS,
    COLOR_MODE_DEFAULTS: COLOR_MODE_DEFAULTS,
    defaultSettings: defaultSettings,
    readSettings: readSettings,
    saveSettings: saveSettings,
    readUrlParams: readUrlParams,
    upDownColors: upDownColors,
    filterByPeriod: filterByPeriod,
    normalizeMarket: normalizeMarket,
    marketSeriesFor: marketSeriesFor,
    buildChartSeries: buildChartSeries,
    buildChartData: buildChartData,
    buildDivergence: buildDivergence,
    sentimentLabel: sentimentLabel,
    sentimentClass: sentimentClass,
    fmChangeText: fmChangeText,
    escapeHtml: escapeHtml,
    sampleBadgeVisible: sampleBadgeVisible,
    interpretToday: interpretToday,
    sessionPanelHtml: sessionPanelHtml,
    directionMatchCardsHtml: directionMatchCardsHtml,
  };
});
