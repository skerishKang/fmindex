"use strict";

const STORAGE_KEYS = {
  theme: "fmindex.theme",
  colorMode: "fmindex.colorMode",
  riseColor: "fmindex.riseColor",
  fallColor: "fmindex.fallColor",
};

const state = {
  data: { joined: [], overnight: null, summary: {} },
  market: "KOSPI",
  period: "24h",
  view: "companion",
  theme: localStorage.getItem(STORAGE_KEYS.theme) || "light",
  colorMode: localStorage.getItem(STORAGE_KEYS.colorMode) || "kr",
  riseColor: localStorage.getItem(STORAGE_KEYS.riseColor) || "#d84a4a",
  fallColor: localStorage.getItem(STORAGE_KEYS.fallColor) || "#356ac3",
  chart: null,
};

const byId = (id) => document.getElementById(id);
const all = (selector) => Array.from(document.querySelectorAll(selector));

function setText(id, value) {
  const node = byId(id);
  if (node) node.textContent = value == null ? "—" : String(value);
}

function formatNumber(value, digits = 1) {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "—";
}

function formatPercent(value, digits = 0) {
  return typeof value === "number" && Number.isFinite(value) ? `${(value * 100).toFixed(digits)}%` : "—";
}

function formatDateTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("ko-KR", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function applyTheme(theme) {
  state.theme = theme === "dark" ? "dark" : "light";
  document.documentElement.dataset.theme = state.theme;
  localStorage.setItem(STORAGE_KEYS.theme, state.theme);

  const toggle = byId("themeToggle");
  if (toggle) {
    const isDark = state.theme === "dark";
    toggle.setAttribute("aria-pressed", String(isDark));
    toggle.setAttribute("aria-label", isDark ? "라이트 모드로 전환" : "다크 모드로 전환");
  }

  if (state.chart) renderChart();
}

function colorPreset(mode) {
  if (mode === "us") return { rise: "#2f9b64", fall: "#d84a4a" };
  if (mode === "custom") return { rise: state.riseColor, fall: state.fallColor };
  return { rise: "#d84a4a", fall: "#356ac3" };
}

function applyColorMode(mode) {
  state.colorMode = ["kr", "us", "custom"].includes(mode) ? mode : "kr";
  const colors = colorPreset(state.colorMode);
  document.documentElement.style.setProperty("--rise", colors.rise);
  document.documentElement.style.setProperty("--fall", colors.fall);
  localStorage.setItem(STORAGE_KEYS.colorMode, state.colorMode);

  all('input[name="colorMode"]').forEach((input) => {
    input.checked = input.value === state.colorMode;
  });

  const custom = byId("customColorFields");
  if (custom) custom.hidden = state.colorMode !== "custom";

  updateCurrentIndex();
  if (state.chart) renderChart();
}

function showSection(sectionId) {
  all(".page-section").forEach((section) => {
    const active = section.id === sectionId;
    section.hidden = !active;
    section.classList.toggle("is-active", active);
  });

  all("[data-section]").forEach((button) => {
    const active = button.dataset.section === sectionId;
    button.classList.toggle("is-active", active);
    if (button.matches(".nav-item")) button.setAttribute("aria-current", active ? "page" : "false");
  });

  if (sectionId === "today" && state.chart) {
    window.setTimeout(() => state.chart.resize(), 0);
  }
}

function filterByPeriod(records, period) {
  if (period === "all" || records.length === 0) return records.slice();
  const validDates = records
    .map((record) => new Date(record.timestamp))
    .filter((date) => !Number.isNaN(date.getTime()));
  if (validDates.length === 0) return [];

  const latest = new Date(Math.max(...validDates.map((date) => date.getTime())));
  const periodMs = period === "24h"
    ? 24 * 60 * 60 * 1000
    : period === "7d"
      ? 7 * 24 * 60 * 60 * 1000
      : 30 * 24 * 60 * 60 * 1000;
  const cutoff = latest.getTime() - periodMs;

  return records.filter((record) => {
    const date = new Date(record.timestamp);
    return !Number.isNaN(date.getTime()) && date.getTime() >= cutoff;
  });
}

function marketRecords() {
  const records = Array.isArray(state.data.joined) ? state.data.joined : [];
  const filtered = filterByPeriod(records, state.period);
  if (state.market === "KOSPI") return filtered;

  return filtered.map((record) => ({
    ...record,
    market: "NASDAQ",
    marketNormalized: null,
    marketChangeRate: null,
    dataMode: "unavailable",
  }));
}

function latestSentimentRecords() {
  return marketRecords().filter((record) => record.hasSentiment !== false && record.fmIndex != null);
}

function sentimentTone(value) {
  if (typeof value !== "number") return { label: "데이터 대기", className: "tone-badge--neutral" };
  if (value >= 65) return { label: "강한 긍정", className: "tone-badge--positive" };
  if (value > 55) return { label: "긍정", className: "tone-badge--positive" };
  if (value <= 35) return { label: "강한 부정", className: "tone-badge--negative" };
  if (value < 45) return { label: "부정", className: "tone-badge--negative" };
  return { label: "중립", className: "tone-badge--neutral" };
}

function updateCurrentIndex() {
  const records = latestSentimentRecords();
  const latest = records.at(-1) || null;
  const previous = records.at(-2) || null;
  const current = latest?.fmIndex;
  const change = typeof current === "number" && typeof previous?.fmIndex === "number"
    ? current - previous.fmIndex
    : null;

  setText("currentFmIndex", formatNumber(current, 1));
  setText("hourlyChange", change == null ? "—" : `${change > 0 ? "+" : ""}${change.toFixed(1)}`);
  setText("postCount", `${latest?.postCount ?? 0}개`);
  setText("confidenceValue", formatPercent(latest?.confidence, 0));
  setText("providerValue", state.data.summary?.provider || "—");
  setText("lastUpdated", formatDateTime(latest?.timestamp || state.data.summary?.updatedAt));

  const changeNode = byId("hourlyChange");
  if (changeNode) {
    changeNode.classList.toggle("is-rise", change != null && change > 0);
    changeNode.classList.toggle("is-fall", change != null && change < 0);
  }

  const tone = sentimentTone(current);
  const badge = byId("sentimentStateBadge");
  if (badge) {
    badge.textContent = tone.label;
    badge.className = `tone-badge ${tone.className}`;
  }

  updateInsight(latest, change);
}

function updateInsight(latest, change) {
  if (!latest || latest.fmIndex == null) {
    setText("insightTitle", "현재 시간대의 심리 표본이 없습니다.");
    setText("insightBody", "누락값을 중립값 50으로 채우지 않고 차트 공백으로 표시합니다.");
    return;
  }

  const tone = sentimentTone(latest.fmIndex).label;
  const movement = change == null
    ? "직전 비교 데이터가 없습니다"
    : change > 0
      ? `직전 시간보다 ${change.toFixed(1)}포인트 높아졌습니다`
      : change < 0
        ? `직전 시간보다 ${Math.abs(change).toFixed(1)}포인트 낮아졌습니다`
        : "직전 시간과 같은 수준입니다";

  if (state.market === "NASDAQ") {
    setText("insightTitle", `현재 커뮤니티 심리는 ${tone}입니다.`);
    setText("insightBody", `${movement}. 나스닥 시장 데이터는 아직 연동되지 않아 시장 방향 비교는 제공하지 않습니다.`);
    return;
  }

  const marketText = latest.marketChangeRate == null
    ? "시장 변동 데이터가 없습니다"
    : `동시간 코스피 변화는 ${latest.marketChangeRate > 0 ? "+" : ""}${latest.marketChangeRate.toFixed(2)}%입니다`;
  setText("insightTitle", `현재 커뮤니티 심리는 ${tone}입니다.`);
  setText("insightBody", `${movement}. ${marketText}. 현재 값은 샘플 구조 검증용이며 예측 정확도를 의미하지 않습니다.`);
}

function updateModeBadge() {
  const mode = state.data.summary?.dataMode || "unavailable";
  const headerBadge = byId("headerModeBadge");
  if (headerBadge) {
    headerBadge.textContent = mode === "real" ? "REAL DATA" : mode === "sample" ? "SAMPLE DATA" : "DATA UNAVAILABLE";
    headerBadge.className = `status-badge ${mode === "real" ? "status-badge--real" : mode === "sample" ? "status-badge--sample" : "status-badge--muted"}`;
  }
  const sampleNotice = byId("sampleNotice");
  if (sampleNotice) sampleNotice.hidden = mode === "real";
}

function updateSessionPanel() {
  const isKospi = state.market === "KOSPI";
  setText("sessionEyebrow", isKospi ? "국내시장" : "미국시장");
  setText("sessionTitle", isKospi ? "야간 심리와 다음 국내장" : "한국 주간 심리와 미국장 결과");
  setText("sessionMiddleLabel", isKospi ? "야간선물" : "미국장 개장");
  setText("sessionPrimary", isKospi ? "실제 거래일·야간선물 데이터 연동 전입니다." : "실제 나스닥 세션 데이터 연동 전입니다.");
  setText(
    "sessionDescription",
    isKospi
      ? "국내장 마감 이후 심리, 야간선물, 다음 날 시가·장초·종가를 순서대로 연결할 예정입니다."
      : "한국시간 주간 심리와 미국장 개장·장초·종가를 연결할 예정입니다."
  );

  const overnight = state.data.overnight || {};
  const status = overnight.status || "unavailable";
  const badge = byId("sessionStatusBadge");
  if (badge) {
    badge.textContent = status === "available" ? "연동 완료" : "연동 전";
    badge.className = `status-badge ${status === "available" ? "status-badge--real" : "status-badge--muted"}`;
  }
}

function updateStatusPanel() {
  const summary = state.data.summary || {};
  const records = marketRecords();
  const latest = records.at(-1) || {};
  const hasSentiment = records.some((record) => record.hasSentiment !== false && record.fmIndex != null);
  const dataMode = state.market === "NASDAQ" ? "unavailable" : summary.dataMode || latest.dataMode || "unavailable";
  const source = state.market === "NASDAQ" ? "미연동" : latest.source || summary.marketSource || "sample-data";
  const updated = latest.timestamp || summary.updatedAt;

  setText("statusMarket", state.market);
  setText("statusDataMode", dataMode);
  setText("statusSource", source);
  setText("statusSentiment", hasSentiment ? "있음" : "없음");
  setText("statusMethodology", summary.methodologyVersion || "fmindex-v1");
  setText("detailMarket", state.market);
  setText("detailDataMode", dataMode);
  setText("detailUpdated", formatDateTime(updated));
  setText("detailProvider", summary.provider || "—");
  setText("detailOvernight", state.data.overnight?.status || "unavailable");
}

function updateChartStatus(records) {
  let message = "";
  if (state.market === "NASDAQ") {
    message = "나스닥 시장 데이터 미연동 · 현재는 펨코지수만 표시합니다.";
  } else if (records.length === 0) {
    message = "선택 기간에 표시할 데이터가 없습니다.";
  } else if (state.view === "lead") {
    message = "선행 관계 계산은 실제 축적 데이터가 확보된 뒤 제공합니다. 현재는 동일 시계열을 유지합니다.";
  } else if (state.view === "divergence") {
    message = "펨코지수와 시장 변화 방향이 반대인 지점을 원형 마커로 강조합니다.";
  } else {
    message = "동일한 시간축에서 커뮤니티 심리와 시장 흐름을 비교합니다.";
  }
  setText("chartStatus", message);
}

function divergencePointRadii(records) {
  if (state.view !== "divergence") return records.map(() => 0);
  return records.map((record, index) => {
    if (index === 0 || record.fmIndex == null || records[index - 1]?.fmIndex == null || record.marketChangeRate == null) return 0;
    const sentimentDelta = record.fmIndex - records[index - 1].fmIndex;
    const opposite = sentimentDelta !== 0 && record.marketChangeRate !== 0 && Math.sign(sentimentDelta) !== Math.sign(record.marketChangeRate);
    return opposite ? 5 : 0;
  });
}

function renderChart() {
  const canvas = byId("mainChart");
  if (!canvas || typeof Chart === "undefined") {
    setText("chartStatus", "Chart.js를 불러오지 못했습니다. 네트워크 또는 정적 자산 상태를 확인하세요.");
    return;
  }

  const records = marketRecords();
  updateChartStatus(records);
  const labelFormatter = new Intl.DateTimeFormat("ko-KR", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
  const labels = records.map((record) => {
    const date = new Date(record.timestamp);
    return Number.isNaN(date.getTime()) ? "—" : labelFormatter.format(date);
  });
  const fmValues = records.map((record) => record.fmIndex == null ? null : record.fmIndex);
  const marketValues = records.map((record) => record.marketNormalized == null ? null : record.marketNormalized);
  const pointRadius = divergencePointRadii(records);
  const gridColor = cssVar("--border");
  const muted = cssVar("--muted");
  const text = cssVar("--text");
  const fmColor = cssVar("--fm-line");
  const marketColor = cssVar("--market-line");

  if (state.chart) state.chart.destroy();
  state.chart = new Chart(canvas, {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: "펨코지수",
          data: fmValues,
          borderColor: fmColor,
          backgroundColor: `${fmColor}1f`,
          yAxisID: "sentiment",
          borderWidth: 2.3,
          tension: 0.28,
          fill: true,
          spanGaps: false,
          pointRadius,
          pointHoverRadius: 5,
          pointBackgroundColor: fmColor,
        },
        {
          label: state.market === "KOSPI" ? "코스피 정규화" : "나스닥 정규화",
          data: marketValues,
          borderColor: marketColor,
          backgroundColor: "transparent",
          yAxisID: "market",
          borderWidth: 2,
          tension: 0.22,
          fill: false,
          spanGaps: false,
          pointRadius: 0,
          pointHoverRadius: 4,
          borderDash: state.market === "NASDAQ" ? [6, 5] : [],
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      animation: { duration: 260 },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: cssVar("--surface"),
          borderColor: gridColor,
          borderWidth: 1,
          titleColor: text,
          bodyColor: text,
          displayColors: true,
          callbacks: {
            afterBody(items) {
              const record = records[items[0]?.dataIndex];
              if (!record) return [];
              return [
                `게시글 ${record.postCount ?? 0}개`,
                `신뢰도 ${formatPercent(record.confidence, 0)}`,
                `데이터 ${record.dataMode || state.data.summary?.dataMode || "unavailable"}`,
              ];
            },
          },
        },
      },
      scales: {
        x: {
          grid: { color: gridColor, drawBorder: false },
          ticks: { color: muted, maxTicksLimit: 10, maxRotation: 0, font: { size: 10 } },
        },
        sentiment: {
          position: "left",
          min: 0,
          max: 100,
          grid: { color: gridColor, drawBorder: false },
          ticks: { color: muted, stepSize: 25, font: { size: 10 } },
          title: { display: true, text: "펨코지수", color: fmColor, font: { size: 10, weight: "600" } },
        },
        market: {
          position: "right",
          grid: { drawOnChartArea: false, drawBorder: false },
          ticks: { color: muted, font: { size: 10 } },
          title: { display: true, text: state.market, color: marketColor, font: { size: 10, weight: "600" } },
        },
      },
    },
  });
}

function renderAll() {
  updateModeBadge();
  updateCurrentIndex();
  updateSessionPanel();
  updateStatusPanel();
  setText("marketLegendLabel", state.market === "KOSPI" ? "코스피" : "나스닥");
  renderChart();
}

function setActiveButton(selector, dataKey, value) {
  all(selector).forEach((button) => {
    button.classList.toggle("is-active", button.dataset[dataKey] === value);
    button.setAttribute("aria-pressed", String(button.dataset[dataKey] === value));
  });
}

function bindEvents() {
  all("[data-section]").forEach((button) => button.addEventListener("click", () => showSection(button.dataset.section)));

  all("[data-market]").forEach((button) => button.addEventListener("click", () => {
    state.market = button.dataset.market;
    setActiveButton("[data-market]", "market", state.market);
    renderAll();
  }));

  all("[data-period]").forEach((button) => button.addEventListener("click", () => {
    state.period = button.dataset.period;
    setActiveButton("[data-period]", "period", state.period);
    renderAll();
  }));

  all("[data-view]").forEach((button) => button.addEventListener("click", () => {
    state.view = button.dataset.view;
    setActiveButton("[data-view]", "view", state.view);
    renderChart();
  }));

  byId("themeToggle")?.addEventListener("click", () => applyTheme(state.theme === "light" ? "dark" : "light"));

  const settingsPanel = byId("settingsPanel");
  const settingsToggle = byId("settingsToggle");
  const closeSettings = () => {
    if (!settingsPanel) return;
    settingsPanel.hidden = true;
    settingsToggle?.setAttribute("aria-expanded", "false");
  };
  settingsToggle?.addEventListener("click", () => {
    if (!settingsPanel) return;
    settingsPanel.hidden = !settingsPanel.hidden;
    settingsToggle.setAttribute("aria-expanded", String(!settingsPanel.hidden));
  });
  byId("settingsClose")?.addEventListener("click", closeSettings);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeSettings();
  });

  all('input[name="colorMode"]').forEach((input) => input.addEventListener("change", () => applyColorMode(input.value)));

  byId("riseColor")?.addEventListener("input", (event) => {
    state.riseColor = event.target.value;
    localStorage.setItem(STORAGE_KEYS.riseColor, state.riseColor);
    if (state.colorMode === "custom") applyColorMode("custom");
  });
  byId("fallColor")?.addEventListener("input", (event) => {
    state.fallColor = event.target.value;
    localStorage.setItem(STORAGE_KEYS.fallColor, state.fallColor);
    if (state.colorMode === "custom") applyColorMode("custom");
  });
}

async function loadData() {
  const response = await fetch("/api/data.json", { cache: "no-store" });
  if (!response.ok) throw new Error(`데이터 요청 실패: ${response.status}`);
  const payload = await response.json();
  state.data = {
    joined: Array.isArray(payload.joined) ? payload.joined : [],
    overnight: payload.overnight || null,
    summary: payload.summary || {},
  };
}

async function boot() {
  applyTheme(state.theme);
  const riseInput = byId("riseColor");
  const fallInput = byId("fallColor");
  if (riseInput) riseInput.value = state.riseColor;
  if (fallInput) fallInput.value = state.fallColor;
  applyColorMode(state.colorMode);
  bindEvents();

  try {
    await loadData();
    renderAll();
  } catch (error) {
    console.error(error);
    setText("chartStatus", "대시보드 데이터를 불러오지 못했습니다. pipeline을 먼저 실행하세요.");
    setText("insightTitle", "데이터 연결을 확인해야 합니다.");
    setText("insightBody", error instanceof Error ? error.message : "알 수 없는 오류");
    updateSessionPanel();
    updateStatusPanel();
  }
}

document.addEventListener("DOMContentLoaded", boot);
