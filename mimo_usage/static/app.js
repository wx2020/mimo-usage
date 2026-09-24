/* MiMo 用量看板前端：只调两个接口渲染全部 UI——
   /api/v1/summary（套餐 + 顶部卡片 + 账户 + 限速）
   /api/v1/usage（近 30 天趋势/模型分布 chart + 限速/插件用量）
   所有业务计算（Credits 折算、百分比、剩余天数、天限额、token 汇总、峰值、
   时间窗口、模型占比排序）都在后端算好；前端只做纯渲染与格式化。 */

"use strict";

const REFRESH_SECONDS = 30;
const API = "/api/v1";
const API_KEY_STORAGE = "mimo-usage-api-key";

const state = {
  timer: null,
  errors: [],
  reauth: null,
};

/* 支持用 /dashboard?key=xxx 打开一次：密钥存进 localStorage 后立刻从地址栏抹掉，
   后续 API 请求只走 X-API-Key 请求头（cookie 的 Path=/dashboard，不会带到 /api/*）。 */
(function bootstrapApiKey() {
  const url = new URL(window.location.href);
  const fromUrl = url.searchParams.get("key");
  if (!fromUrl) return;
  try {
    localStorage.setItem(API_KEY_STORAGE, fromUrl);
  } catch (_) { /* 隐私模式等 */ }
  url.searchParams.delete("key");
  window.history.replaceState({}, "", url.pathname + url.search + url.hash);
})();

const apiKey = () => {
  try {
    return localStorage.getItem(API_KEY_STORAGE) || "";
  } catch (_) {
    return "";
  }
};

function $(id) {
  return document.getElementById(id);
}

function num(value, digits) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "--";
  return n.toLocaleString("zh-CN", { maximumFractionDigits: digits ?? 2 });
}

function compactNum(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "--";
  if (n >= 1e8) return (n / 1e8).toFixed(2) + " 亿";
  if (n >= 1e4) return (n / 1e4).toFixed(1) + " 万";
  return n.toLocaleString("zh-CN");
}

function fmtPercent(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "--";
  return n.toFixed(2) + "%";
}

/* 时间统一按 Asia/Shanghai 呈现为 "YYYY-MM-DD HH:mm:ss"（不随设备时区变） */
function formatCstTimestamp(value) {
  const date = value ? new Date(value) : new Date();
  if (Number.isNaN(date.getTime())) return String(value || "--");
  const parts = {};
  for (const part of new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
    hourCycle: "h23",
  }).formatToParts(date)) {
    parts[part.type] = part.value;
  }
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
}

function statCard(label, value, sub, opts = {}) {
  const div = document.createElement("div");
  div.className = "stat" + (opts.warn ? " warn" : "");
  if (opts.title) div.title = opts.title;
  const labelEl = document.createElement("div");
  labelEl.className = "label";
  labelEl.textContent = label;
  const valueEl = document.createElement("div");
  valueEl.className = "value";
  valueEl.textContent = value;
  div.append(labelEl, valueEl);
  if (sub) {
    const subEl = document.createElement("div");
    subEl.className = "sub";
    subEl.textContent = sub;
    div.append(subEl);
  }
  if (typeof opts.bar === "number" && Number.isFinite(opts.bar)) {
    const bar = document.createElement("div");
    bar.className = "bar";
    const fill = document.createElement("i");
    fill.style.width = (Math.max(0, Math.min(opts.bar, 1)) * 100).toFixed(2) + "%";
    bar.append(fill);
    div.append(bar);
  }
  return div;
}

function kvRow(host, key, value) {
  const row = document.createElement("div");
  row.className = "kv-row";
  const k = document.createElement("span");
  k.className = "k";
  k.textContent = key;
  const v = document.createElement("span");
  v.className = "v";
  v.textContent = value;
  row.append(k, v);
  host.append(row);
}

function card(title) {
  const div = document.createElement("div");
  div.className = "card";
  const h = document.createElement("h3");
  h.textContent = title;
  div.append(h);
  return div;
}

function shortModel(name) {
  return String(name || "").replace(/^mimo-/, "");
}

/* ---------- 占比档位配色（占最多的最深；深浅随参与上色模型数自适应） ---------- */

const MODEL_PALETTES = {
  1: ["#ff824b"],
  2: ["#f26a1b", "#ffb98f"],
  3: ["#e8651f", "#ff9f6b", "#ffd0b0"],
  4: ["#dc520f", "#ff8f52", "#ffb98f", "#ffddc7"],
  5: ["#cf4a0d", "#f26a1b", "#ff9f6b", "#ffc39c", "#ffe2cf"],
  6: ["#b83a09", "#dc520f", "#f26a1b", "#ff824b", "#ffab7d", "#ffd0b0"],
};
/* 深色背景下整体提亮一档，保证最小档也有足够对比（同样"占最多最深"） */
const DARK_MODEL_PALETTES = {
  1: ["#ff9a5c"],
  2: ["#ff8f52", "#ffc49b"],
  3: ["#ff824b", "#ffb98f", "#ffe0c8"],
  4: ["#f2742f", "#ffa06b", "#ffc9a5", "#ffe6d2"],
  5: ["#f26a1b", "#ff9a5c", "#ffb98f", "#ffd5b8", "#fff0e4"],
  6: ["#e8651f", "#ff8f52", "#ffab7d", "#ffc6a0", "#ffddc4", "#fff2e8"],
};
const MODEL_OTHER_LIGHT = "#cbd2dc";
const MODEL_OTHER_DARK = "#4d5663";
const MODEL_COLOR_SLOTS = 6;
//: 段高不足 ~1px（0.6% of max）就不渲染，避免 0.2px 发丝线伪影
const MIN_SEGMENT_RATIO = 0.006;

const prefersDark = () =>
  !!(window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);

function modelPalettes() {
  return prefersDark() ? DARK_MODEL_PALETTES : MODEL_PALETTES;
}

function otherColor() {
  return prefersDark() ? MODEL_OTHER_DARK : MODEL_OTHER_LIGHT;
}

function colorForRank(rank, coloredCount) {
  const slots = Math.max(1, Math.min(coloredCount || 1, MODEL_COLOR_SLOTS));
  const table = modelPalettes();
  const palette = table[slots] || table[MODEL_COLOR_SLOTS];
  return rank < palette.length ? palette[rank] : otherColor();
}

/* ---------- 顶部卡片（数值全部来自 /api/v1/summary） ---------- */

function renderSummary(data) {
  const cards = data.cards || {};
  const today = cards.today || {};
  const total = cards.total || {};
  const tokens = cards.tokens || {};
  const credits = cards.credits || {};
  const plan = data.plan || {};

  const creditTitle = credits.month != null
    ? `；官方 used ${num(credits.used, 0)} · 当月折算 ${num(credits.month, 0)}`
      + `（偏差 ${credits.delta >= 0 ? "+" : ""}${num(credits.delta, 0)} Credits；`
      + "官方单价表口径，未含夜间 0.8x）"
    : "";

  $("hero").replaceChildren(
    statCard("今日额度", fmtPercent(today.percent),
      today.credits ? `今日 ${compactNum(today.credits)} Credits` : "今日暂无用量",
      {
        bar: today.bar,
        warn: !!today.warn,
        title: "今日积分 ÷ 天限额（剩余额度 ÷ 剩余天数）；>100% 为超过日均摊平额度，属正常"
          + creditTitle,
      }),
    statCard("总额度", fmtPercent(total.percent),
      total.limit > 0 ? `已用 ${compactNum(total.used)} / ${compactNum(total.limit)} Credits` : "额度未知",
      {
        bar: total.bar,
        title: "套餐总额度（Credits）用量" + (total.remaining != null ? `，剩余 ${num(total.remaining, 0)}` : ""),
      })
  );

  $("stats").replaceChildren(
    statCard("天限额", today.dailyQuota != null ? compactNum(today.dailyQuota) + " /天" : "--",
      "剩余额度 ÷ 剩余天数",
      { title: "动态剩余日均：剩余额度摊到套餐到期日" }),
    statCard("剩余额度", total.remaining != null ? compactNum(total.remaining) : "--",
      total.days != null ? `剩 ${total.days} 天` : "",
      { title: "总额度剩余（Credits）" })
  );

  const peak = tokens.peak;
  $("stats-2").replaceChildren(
    statCard("今日 Tokens", compactNum(tokens.today), `请求 ${num(tokens.todayRequests, 0)} 次`),
    statCard("本月 Tokens", compactNum(tokens.month),
      `日均 ${compactNum(tokens.monthAverage)} · ${tokens.monthDays || 0} 天 · ${num(tokens.monthRequests, 0)} 请求`),
    statCard("总 Tokens", compactNum(tokens.allTime), "全期（本月 + 历史月度）",
      { title: "本月按日行 + 按年聚合行（剔除本月，近两年）" }),
    statCard("单日峰值", peak ? compactNum(peak.tokens) : "--", peak ? peak.date : "")
  );

  $("plan").textContent = plan.planName || "--";
  renderAccountCards(data);
}

function renderAccountCards(data) {
  const host = $("account");
  host.replaceChildren();
  const plan = data.plan || {};
  const account = data.account || {};
  const verification = data.verification || {};
  const rate = data.rateLimit || {};

  const planCard = card("订阅");
  kvRow(planCard, "套餐", plan.planName || "--");
  kvRow(planCard, "编码", plan.planCode || "--");
  kvRow(planCard, "到期", plan.currentPeriodEnd || "--");
  kvRow(planCard, "自动续订", plan.enableAutoRenew ? "是" : "否");
  kvRow(planCard, "MiMo Claw", plan.clawEnabled ? "已开通" : "未开通");

  const verifyText = verification.state === "AUTHORIZED" ? "已认证"
    : verification.state === "NOT_AUTHORIZED" ? "未认证"
      : verification.state ? String(verification.state) : "--";

  const accountCard = card("账户");
  kvRow(accountCard, "ID", account.userId || "--");
  kvRow(accountCard, "手机", account.phone || "--");
  kvRow(accountCard, "邮箱", account.email || "--");
  kvRow(accountCard, "微信", account.weixin ? "已绑定" : "未绑定");
  kvRow(accountCard, "实名认证", verifyText);

  const rateCard = card("限速");
  kvRow(rateCard, "请求 TPM", rate.tpm != null ? compactNum(rate.tpm) : "--");
  kvRow(rateCard, "请求 RPM", rate.rpm != null ? num(rate.rpm, 0) : "--");
  kvRow(rateCard, "查询 TPM", rate.queryTpm != null ? compactNum(rate.queryTpm) : "--");
  kvRow(rateCard, "并发", rate.concurrency != null ? num(rate.concurrency, 0) : "--");

  host.append(planCard, accountCard, rateCard);
}

/* ---------- /api/v1/usage 的 chart（后端算好 points/models/totals/axisMax） ---------- */

function renderUsage(data) {
  const chart = data.chart || { points: [], models: [], totals: {}, axisMax: 1 };
  renderTokenTrend(chart);
  renderModelBreakdown(chart);
}

function pointTitle(point) {
  const detail = (point.models || [])
    .map((item) => `${shortModel(item.model)} ${compactNum(item.tokens)}`)
    .join(" \u00b7 ");
  if (!point.totalToken) return `${point.date}\uff1a0 tokens\uff08\u65e0\u8c03\u7528\uff09`;
  return `${point.date}\uff1a${point.totalToken.toLocaleString("zh-CN")} tokens\uff08${detail}\uff09`;
}

function renderTokenTrend(chart) {
  const points = chart.points || [];
  const models = chart.models || [];
  const totals = chart.totals || {};
  const chartEl = $("trend-chart");
  const yaxis = $("trend-yaxis");
  const axis = $("trend-axis");
  const legend = $("trend-legend");
  chartEl.replaceChildren();
  yaxis.replaceChildren();
  axis.replaceChildren();
  axis.className = "axis axis-edge";
  if (legend) legend.replaceChildren();

  const coloredCount = Math.min(models.length, MODEL_COLOR_SLOTS);
  const colored = models.slice(0, coloredCount);
  const rest = models.slice(coloredCount);

  if (!points.length || !totals.totalToken) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = `\u8fd1 ${chart.days || 30} \u5929\u6682\u65e0 Token \u7528\u91cf`;
    chartEl.append(empty);
    yaxis.append(document.createElement("span"));
    return;
  }

  const max = chart.axisMax || 1;
  const grid = document.createElement("div");
  grid.className = "plot-grid";
  grid.append(document.createElement("em"), document.createElement("em"), document.createElement("em"));
  chartEl.append(grid);

  const cols = document.createElement("div");
  cols.className = "cols";
  for (const point of points) {
    const col = document.createElement("div");
    col.className = point.totalToken > 0 ? "col" : "col zero";
    col.title = pointTitle(point);
    if (point.totalToken > 0) {
      const perModel = new Map((point.models || []).map(({ model, tokens }) => [model, tokens]));
      const bands = [];
      for (const { model, rank } of colored) {
        const tokens = perModel.get(model) || 0;
        if (tokens <= 0 || tokens / max < MIN_SEGMENT_RATIO) continue;
        bands.push({ color: colorForRank(rank, coloredCount), tokens });
      }
      if (rest.length) {
        const otherTokens = rest.reduce((acc, item) => acc + (perModel.get(item.model) || 0), 0);
        if (otherTokens > 0 && otherTokens / max >= MIN_SEGMENT_RATIO) {
          bands.push({ color: otherColor(), tokens: otherTokens });
        }
      }
      if (bands.length) {
        const rendered = bands.reduce((acc, band) => acc + band.tokens, 0);
        const bar = document.createElement("i");
        bar.style.height = ((rendered / max) * 100).toFixed(3) + "%";
        if (bands.length === 1) {
          bar.style.background = bands[0].color;
        } else {
          let acc = 0;
          const stops = bands.map((band) => {
            const from = (acc / rendered) * 100;
            acc += band.tokens;
            const to = (acc / rendered) * 100;
            return `${band.color} ${from.toFixed(4)}% ${to.toFixed(4)}%`;
          });
          bar.style.background = `linear-gradient(to top, ${stops.join(", ")})`;
        }
        col.append(bar);
      }
    }
    cols.append(col);
  }
  chartEl.append(cols);

  for (const value of [max, max / 2, 0]) {
    const tick = document.createElement("span");
    tick.textContent = compactNum(value);
    yaxis.append(tick);
  }

  const peak = totals.peak;
  const left = document.createElement("span");
  left.textContent = points[0].label;
  const middle = document.createElement("span");
  middle.textContent = `\u6bcf\u65e5\u5cf0\u503c ${peak ? compactNum(peak.tokens) : "--"}`
    + `\uff08${peak ? String(peak.date).slice(5) : "--"}\uff09\u00b7 \u5408\u8ba1 ${compactNum(totals.totalToken)} tokens`
    + ` \u00b7 ${totals.models || 0} \u4e2a\u6a21\u578b`;
  const right = document.createElement("span");
  right.textContent = points[points.length - 1].label;
  axis.append(left, middle, right);

  if (legend) {
    for (const { model, rank, sharePercent } of colored) {
      const item = document.createElement("span");
      const swatch = document.createElement("i");
      swatch.className = "swatch";
      swatch.style.background = colorForRank(rank, coloredCount);
      const name = document.createElement("em");
      name.textContent = `${shortModel(model)} ${sharePercent.toFixed(1)}%`;
      item.append(swatch, name);
      legend.append(item);
    }
    if (rest.length) {
      const item = document.createElement("span");
      const swatch = document.createElement("i");
      swatch.className = "swatch";
      swatch.style.background = otherColor();
      const name = document.createElement("em");
      name.textContent = `\u5176\u4ed6\uff08${rest.length} \u6b3e\uff09`;
      item.append(swatch, name);
      legend.append(item);
    }
  }
}

function renderModelBreakdown(chart) {
  const host = $("model-bars");
  host.replaceChildren();
  const models = chart.models || [];
  if (!models.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = `\u8fd1 ${chart.days || 30} \u5929\u6682\u65e0\u6a21\u578b\u6570\u636e`;
    host.append(empty);
    return;
  }
  const max = Math.max(...models.map((row) => row.tokens), 1);
  const coloredCount = Math.min(models.length, MODEL_COLOR_SLOTS);
  for (const { model, tokens, rank, sharePercent } of models) {
    const row = document.createElement("div");
    row.className = "mbar-row";
    const name = document.createElement("span");
    name.className = "mbar-name";
    name.textContent = shortModel(model);
    const track = document.createElement("div");
    track.className = "mbar-track";
    const fill = document.createElement("i");
    fill.style.width = ((tokens / max) * 100).toFixed(2) + "%";
    fill.style.background = colorForRank(rank, coloredCount);
    track.append(fill);
    const value = document.createElement("span");
    value.className = "mbar-value";
    value.textContent = `${compactNum(tokens)} \u00b7 ${sharePercent.toFixed(1)}%`;
    row.append(name, track, value);
    host.append(row);
  }
}

function renderErrors(meta) {
  const box = $("errors");
  const errors = (meta && meta.errors) || {};
  const parts = Object.entries(errors).map(([name, err]) => `${name}: ${err.message || err.type}`);
  box.textContent = parts.join("\n");
  const banner = $("banner");
  const reauth = state.reauth;
  if (reauth && reauth.coolingDown) {
    banner.hidden = false;
    banner.textContent = `自动续登冷却中：${reauth.lastError || "未知原因"}（${reauth.cooldownRemainingSeconds}s 后重试）`;
  } else if (parts.length) {
    banner.hidden = false;
    banner.textContent = "部分数据获取失败，详见页底。";
  } else {
    banner.hidden = true;
  }
}

/* ---------- 主流程 ---------- */

function fetchOptions() {
  const headers = { Accept: "application/json" };
  const key = apiKey();
  if (key) headers["X-API-Key"] = key;
  return { headers, credentials: "same-origin" };
}

async function readPayload(response) {
  let body = null;
  try {
    body = await response.json();
  } catch (_) { /* ignore */ }
  if (response.ok) return body || {};
  let message = `HTTP ${response.status}`;
  let type = "";
  if (body && body.error) {
    message = body.error.message || message;
    type = body.error.type || "";
  }
  if (type === "unauthorized" || type === "forbidden") {
    throw new Error(
      "未授权：请用 " + window.location.pathname + "?key=你的密钥 打开一次本页"
      + "（换过密钥也要重新打开一次）"
    );
  }
  throw new Error(message);
}

async function load() {
  const updated = $("updated");
  updated.textContent = "加载中…";
  try {
    const [summaryResponse, usageResponse] = await Promise.all([
      fetch(`${API}/summary`, fetchOptions()),
      fetch(`${API}/usage`, fetchOptions()),
    ]);
    const summaryPayload = await readPayload(summaryResponse);
    const usagePayload = await readPayload(usageResponse);

    state.reauth = null;
    renderSummary(summaryPayload.data || {});
    renderUsage(usagePayload.data || {});
    const errors = {
      ...((summaryPayload.meta && summaryPayload.meta.errors) || {}),
      ...((usagePayload.meta && usagePayload.meta.errors) || {}),
    };
    renderErrors({ errors });

    // 右上角：本次更新时间（Asia/Shanghai，YYYY-MM-DD HH:mm:ss）
    updated.textContent = `更新于 ${formatCstTimestamp(summaryPayload.meta && summaryPayload.meta.generatedAt)}`;
    state.errors = [];
  } catch (error) {
    state.errors = [String(error && error.message || error)];
    $("errors").textContent = state.errors.join("\n");
    $("banner").hidden = false;
    $("banner").textContent = "加载失败：" + state.errors[0];
    updated.textContent = "加载失败";
    // 续登状态从 healthz 补充
    try {
      const health = await fetch("/healthz").then((r) => r.json());
      state.reauth = health.reauth || null;
      renderErrors({ errors: {} });
    } catch (_) { /* ignore */ }
  }
}

function boot() {
  $("refresh").addEventListener("click", () => load());
  load();
  state.timer = setInterval(load, REFRESH_SECONDS * 1000);
  // 设备外观切换（浅/深色）时，图表配色需要按新主题重绘一次
  if (window.matchMedia) {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const onSchemeChange = () => load();
    if (typeof media.addEventListener === "function") media.addEventListener("change", onSchemeChange);
    else if (typeof media.addListener === "function") media.addListener(onSchemeChange);
  }
}

boot();
