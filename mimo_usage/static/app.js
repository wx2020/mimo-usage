/* MiMo 用量看板前端：调 /api/v1/overview（+ usage/trend 年份聚合），
   渲染两排 stats（额度/积分口径 + token 口径）+ Token 每日柱图 + 月账单柱图。
   数据一律来自上游真实字段，缺数据时分母为空则显示 "--"。 */

"use strict";

const REFRESH_SECONDS = 30;
const API = "/api/v1";
const API_KEY_STORAGE = "mimo-usage-api-key";

const state = {
  timer: null,
  errors: [],
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

const pad2 = (n) => String(n).padStart(2, "0");

/* 官方 Token Plan 单价（Credits / token）：
   pro 系（v2.6-pro/v2.5-pro）命中 2.5 / 未命中 300 / 输出 600；
   flash 系（v2.6-flash/v2.5）命中 2 / 未命中 100 / 输出 200。
   来源：mimo.mi.com 文档《Token Plan · 个人版 · 额度消耗规则》
   （ASR 30M Credits/小时、TTS 免费；夜间 0:00-8:00 另有 0.8x 系数） */
const CREDIT_RATES = {
  "mimo-v2.6-pro": [2.5, 300, 600],
  "mimo-v2.5-pro": [2.5, 300, 600],
  "mimo-v2.6-flash": [2, 100, 200],
  "mimo-v2.5": [2, 100, 200],
};

/* 按官方单价把用量行折算成 Credits（未识别模型跳过，不猜） */
function creditsOf(rows) {
  let total = 0;
  for (const row of Array.isArray(rows) ? rows : []) {
    const rate = CREDIT_RATES[row && row.model];
    if (!rate) continue;
    total += (Number(row.inputHitToken) || 0) * rate[0]
      + (Number(row.inputMissToken) || 0) * rate[1]
      + (Number(row.outputToken) || 0) * rate[2];
  }
  return total;
}

function cstToday() {
  const { year, month, day } = cstParts();
  return `${year}-${pad2(month)}-${pad2(day)}`;
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

/* ---------- 两排 stats ---------- */

function shortModel(name) {
  return String(name || "").replace(/^mimo-/, "");
}

function renderStats(data, yearlyRows) {
  const detail = data.tokenPlanDetail || {};
  const planUsage = data.tokenPlanUsage || {};
  const trend = Array.isArray(data.usageTrend) ? data.usageTrend : [];

  // 总额度（plan_total_token 与 monthUsage 同源；percent 用 used/limit 自算，弃上游粗值 0.01）
  const planItems = (planUsage.usage && planUsage.usage.items) || [];
  const planItem = planItems.find((item) => item && item.name === "plan_total_token")
    || ((planUsage.monthUsage && planUsage.monthUsage.items) || [])[0]
    || null;
  const limit = planItem ? Number(planItem.limit) || 0 : 0;
  const used = planItem ? Number(planItem.used) || 0 : 0;
  const usedRatio = limit > 0 ? used / limit : null;
  const remaining = limit > 0 ? Math.max(limit - used, 0) : null;

  // 到期与剩余天数
  const endRaw = detail.currentPeriodEnd ? String(detail.currentPeriodEnd) : null;
  const endDate = endRaw ? new Date(endRaw.replace(" ", "T")) : null;
  const validEnd = endDate && !Number.isNaN(endDate.getTime()) ? endDate : null;
  const daysLeft = validEnd ? Math.max(Math.ceil((validEnd.getTime() - Date.now()) / 86400000), 1) : null;

  // 天限额 = 剩余额度 ÷ 剩余天数（动态剩余日均）
  const dailyQuota = remaining !== null && daysLeft ? remaining / daysLeft : null;

  // 今日 / 本月聚合（usageTrend 为按日行）
  const sum = (rows, key) => rows.reduce((acc, row) => acc + (Number(row[key]) || 0), 0);
  const today = cstToday();
  const todayRows = trend.filter((row) => row && row.date === today);
  const todayTokens = sum(todayRows, "totalToken");
  const todayRequests = sum(todayRows, "requestCount");
  const monthTokens = sum(trend, "totalToken");
  const monthRequests = sum(trend, "requestCount");
  const todayCredits = creditsOf(todayRows);
  const monthCredits = creditsOf(trend);

  // 全期 tokens = 本月（新鲜月度数据）+ 按年聚合行中剔除本月的部分
  // （按年行有 600s 缓存，直接相加会出现"总 < 本月"的时序倒挂）
  const { year: nowYear, month: nowMonth } = cstParts();
  const monthKey = `${nowYear}-${pad2(nowMonth)}`;
  const historicTokens = (Array.isArray(yearlyRows) ? yearlyRows : [])
    .filter((row) => row && row.date !== monthKey)
    .reduce((acc, row) => acc + (Number(row.totalToken) || 0), 0);
  const allTimeTokens = monthTokens + historicTokens;

  // 每日聚合（模型级分布由趋势图/模型分布图各自处理）
  const perDay = new Map();
  for (const row of trend) {
    if (!row || !row.date) continue;
    perDay.set(row.date, (perDay.get(row.date) || 0) + (Number(row.totalToken) || 0));
  }
  let peak = null;
  for (const [date, tokens] of perDay) if (!peak || tokens > peak.tokens) peak = { date, tokens };

  // 折算对账：当月折算 vs 官方 used
  const creditDelta = monthCredits - used;
  const creditTitle = `官方 used ${num(used, 0)} · 当月折算 ${num(monthCredits, 0)}`
    + `（偏差 ${creditDelta >= 0 ? "+" : ""}${num(creditDelta, 0)} Credits；官方单价表口径，未含夜间 0.8x）`;

  // 今日额度 = 今日积分 ÷ 天限额（动态剩余日均）
  const todayRatio = dailyQuota ? todayCredits / dailyQuota : null;

  // 顶部大卡片：额度主口径（今日额度在前，总额度在后）
  const hero = $("hero");
  hero.replaceChildren(
    statCard("今日额度", todayRatio != null ? (todayRatio * 100).toFixed(todayRatio >= 10 ? 0 : 2) + "%" : "--",
      todayCredits ? `今日 ${compactNum(todayCredits)} Credits` : "今日暂无用量",
      {
        bar: todayRatio == null ? undefined : Math.min(todayRatio, 1),
        warn: todayRatio != null && todayRatio > 1,
        title: "今日积分 ÷ 天限额（剩余额度 ÷ 剩余天数）；>100% 为超过日均摊平额度，属正常"
          + `；${creditTitle}`,
      }),
    statCard("总额度", usedRatio != null ? (usedRatio * 100).toFixed(2) + "%" : "--",
      limit > 0 ? `已用 ${compactNum(used)} / ${compactNum(limit)} Credits` : "额度未知",
      {
        bar: usedRatio == null ? undefined : usedRatio,
        title: "套餐总额度（Credits）用量" + (remaining != null ? `，剩余 ${num(remaining, 0)}` : ""),
      })
  );

  const row1 = $("stats");
  row1.replaceChildren(
    statCard("天限额", dailyQuota != null ? compactNum(dailyQuota) + " /天" : "--",
      "剩余额度 ÷ 剩余天数",
      { title: "动态剩余日均：剩余额度摊到套餐到期日" }),
    statCard("剩余额度", remaining != null ? compactNum(remaining) : "--",
      daysLeft != null ? `剩 ${daysLeft} 天` : "",
      { title: "总额度剩余（Credits）" })
  );

  const row2 = $("stats-2");
  row2.replaceChildren(
    statCard("今日 tokens", compactNum(todayTokens), `请求 ${num(todayRequests, 0)} 次`),
    statCard("本月 tokens", compactNum(monthTokens),
      `日均 ${compactNum(monthTokens / (perDay.size || 1))} · ${perDay.size} 天 · ${num(monthRequests, 0)} 请求`),
    statCard("总 Tokens", compactNum(allTimeTokens), "全期（本月 + 历史月度）",
      { title: "本月按日行 + 按年聚合行（剔除本月，近两年）" }),
    statCard("单日峰值", peak ? compactNum(peak.tokens) : "--", peak ? peak.date : "")
  );
}

/* ---------- 柱状图（yaxis 三档刻度 + 虚线网格 + 0 值留空列） ---------- */

function niceCeil(value) {
  if (!Number.isFinite(value) || value <= 0) return 1;
  const base = Math.pow(10, Math.floor(Math.log10(value)));
  for (const step of [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) {
    if (step * base >= value) return step * base;
  }
  return 10 * base;
}

/* Asia/Shanghai 的今天 */
function cstParts() {
  const now = new Date();
  const cst = new Date(now.getTime() + (now.getTimezoneOffset() + 480) * 60000);
  return { year: cst.getFullYear(), month: cst.getMonth() + 1, day: cst.getDate() };
}

function currentYearMonth() {
  const parts = cstParts();
  return { year: parts.year, month: parts.month };
}

/* 近 N 天窗口（含今天）的每日 × 模型数据；无数据日留空白列 */
function buildTrendWindow(rows, days = 30) {
  const byDate = new Map();
  for (const row of Array.isArray(rows) ? rows : []) {
    if (!row || !row.date) continue;
    const entry = byDate.get(row.date) || { total: 0, models: new Map() };
    const tokens = Number(row.totalToken) || 0;
    entry.total += tokens;
    const model = row.model || "?";
    entry.models.set(model, (entry.models.get(model) || 0) + tokens);
    byDate.set(row.date, entry);
  }

  const { year, month, day } = cstParts();
  const todayUtc = new Date(Date.UTC(year, month - 1, day));
  const points = [];
  for (let offset = days - 1; offset >= 0; offset--) {
    const cursor = new Date(todayUtc.getTime() - offset * 86400000);
    const key = cursor.toISOString().slice(0, 10);
    const hit = byDate.get(key);
    const models = hit
      ? [...hit.models.entries()].map(([model, tokens]) => ({ model, tokens }))
      : [];
    const total = hit ? hit.total : 0;
    const detail = models
      .slice()
      .sort((a, b) => b.tokens - a.tokens)
      .map((m) => `${shortModel(m.model)} ${compactNum(m.tokens)}`)
      .join(" \u00b7 ");
    points.push({
      key,
      label: key.slice(5),
      value: total,
      models,
      title: hit
        ? `${key}\uff1a${total.toLocaleString("zh-CN")} tokens\uff08${detail}\uff09`
        : `${key}\uff1a0 tokens\uff08\u65e0\u8c03\u7528\uff09`,
    });
  }
  return points;
}

/* 窗口内每模型合计（占比降序；并列按模型名保证稳定） */
function aggregateModelTotals(points) {
  const totals = new Map();
  for (const point of points) {
    for (const { model, tokens } of point.models || []) {
      totals.set(model, (totals.get(model) || 0) + tokens);
    }
  }
  return [...totals.entries()]
    .map(([model, tokens]) => ({ model, tokens }))
    .sort((a, b) => b.tokens - a.tokens || a.model.localeCompare(b.model));
}

/* 占比档位：rank 0 = 占比最高（取最深色） */
function rankedModels(points) {
  const rows = aggregateModelTotals(points);
  const total = rows.reduce((acc, row) => acc + row.tokens, 0);
  return rows.map((row, rank) => ({
    ...row,
    rank,
    share: total ? row.tokens / total : 0,
  }));
}

/* ---------- Token 用量趋势（近 30 天 · 按模型堆叠） ---------- */

/* 占比档位配色：占最多的最深。档位深浅**随参与上色的模型数**自适应——
   模型少时用友好橙（独苗 = 主橙），多模型共图才逐步加深，最深 #b83a09 仅 6 款同图出现。
   趋势堆叠图与模型分布图共用同一档位语义 → 两图同模型同色。 */
const MODEL_PALETTES = {
  1: ["#ff824b"],
  2: ["#f26a1b", "#ffb98f"],
  3: ["#e8651f", "#ff9f6b", "#ffd0b0"],
  4: ["#dc520f", "#ff8f52", "#ffb98f", "#ffddc7"],
  5: ["#cf4a0d", "#f26a1b", "#ff9f6b", "#ffc39c", "#ffe2cf"],
  6: ["#b83a09", "#dc520f", "#f26a1b", "#ff824b", "#ffab7d", "#ffd0b0"],
};
const MODEL_OTHER_COLOR = "#cbd2dc";
const MODEL_COLOR_SLOTS = 6;

/* rank 0 = 占比最高（该档位组里最深） */
function colorForRank(rank, coloredCount) {
  const slots = Math.max(1, Math.min(coloredCount || 1, MODEL_COLOR_SLOTS));
  const palette = MODEL_PALETTES[slots] || MODEL_PALETTES[MODEL_COLOR_SLOTS];
  return rank < palette.length ? palette[rank] : MODEL_OTHER_COLOR;
}

function renderTokenTrend(points) {
  const chart = $("trend-chart");
  const yaxis = $("trend-yaxis");
  const axis = $("trend-axis");
  const legend = $("trend-legend");
  chart.replaceChildren();
  yaxis.replaceChildren();
  axis.replaceChildren();
  axis.className = "axis axis-edge";
  if (legend) legend.replaceChildren();

  const ranked = rankedModels(points);
  const coloredCount = Math.min(ranked.length, MODEL_COLOR_SLOTS);
  const colored = ranked.slice(0, coloredCount);
  const rest = ranked.slice(coloredCount);

  if (!points.length || points.every((point) => point.value === 0)) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "\u8fd1 30 \u5929\u6682\u65e0 Token \u7528\u91cf";
    chart.append(empty);
    yaxis.append(document.createElement("span"));
    return;
  }

  const max = niceCeil(Math.max(...points.map((point) => point.value), 0));
  const grid = document.createElement("div");
  grid.className = "plot-grid";
  grid.append(document.createElement("em"), document.createElement("em"), document.createElement("em"));
  chart.append(grid);

  const cols = document.createElement("div");
  cols.className = "cols";
  for (const point of points) {
    const col = document.createElement("div");
    col.className = point.value > 0 ? "col stack" : "col zero";
    col.title = point.title;
    if (point.value > 0) {
      const perModel = new Map((point.models || []).map(({ model, tokens }) => [model, tokens]));
      // 先入者在底（column-reverse）：Top-6 按占比降序，最深色在最底
      for (const { model, rank } of colored) {
        const tokens = perModel.get(model) || 0;
        if (tokens <= 0) continue;
        const segment = document.createElement("i");
        segment.style.height = ((tokens / max) * 100).toFixed(3) + "%";
        segment.style.background = colorForRank(rank, coloredCount);
        col.append(segment);
      }
      // 「其他（第 7 名及以后）」并为一段中性灰
      if (rest.length) {
        const otherTokens = rest.reduce((acc, item) => acc + (perModel.get(item.model) || 0), 0);
        if (otherTokens > 0) {
          const segment = document.createElement("i");
          segment.style.height = ((otherTokens / max) * 100).toFixed(3) + "%";
          segment.style.background = MODEL_OTHER_COLOR;
          col.append(segment);
        }
      }
    }
    cols.append(col);
  }
  chart.append(cols);

  for (const value of [max, max / 2, 0]) {
    const tick = document.createElement("span");
    tick.textContent = compactNum(value);
    yaxis.append(tick);
  }

  const peak = points.reduce((best, item) => (item.value > best.value ? item : best), points[0]);
  const total = points.reduce((sum, item) => sum + item.value, 0);
  const left = document.createElement("span");
  left.textContent = points[0].label;
  const middle = document.createElement("span");
  middle.textContent = `\u6bcf\u65e5\u5cf0\u503c ${compactNum(peak.value)}\uff08${peak.label}\uff09\u00b7 \u5408\u8ba1 ${compactNum(total)} tokens \u00b7 ${ranked.length} \u4e2a\u6a21\u578b`;
  const right = document.createElement("span");
  right.textContent = points[points.length - 1].label;
  axis.append(left, middle, right);

  if (legend) {
    for (const { model, rank, share } of colored) {
      const item = document.createElement("span");
      const swatch = document.createElement("i");
      swatch.className = "swatch";
      swatch.style.background = colorForRank(rank, coloredCount);
      const name = document.createElement("em");
      name.textContent = `${shortModel(model)} ${(share * 100).toFixed(1)}%`;
      item.append(swatch, name);
      legend.append(item);
    }
    if (rest.length) {
      const item = document.createElement("span");
      const swatch = document.createElement("i");
      swatch.className = "swatch";
      swatch.style.background = MODEL_OTHER_COLOR;
      const name = document.createElement("em");
      name.textContent = `\u5176\u4ed6\uff08${rest.length} \u6b3e\uff09`;
      item.append(swatch, name);
      legend.append(item);
    }
  }
}

/* 模型分布（近 30 天合计 · 水平条形图；全部模型逐行，色＝占比档位） */
function renderModelBreakdown(points) {
  const host = $("model-bars");
  host.replaceChildren();
  const ranked = rankedModels(points);
  if (!ranked.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "\u8fd1 30 \u5929\u6682\u65e0\u6a21\u578b\u6570\u636e";
    host.append(empty);
    return;
  }
  const max = Math.max(...ranked.map((row) => row.tokens), 1);
  const coloredCount = Math.min(ranked.length, MODEL_COLOR_SLOTS);
  for (const { model, tokens, rank, share } of ranked) {
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
    value.textContent = `${compactNum(tokens)} \u00b7 ${(share * 100).toFixed(1)}%`;
    row.append(name, track, value);
    host.append(row);
  }
}

function renderAccountCards(data) {
  const host = $("account");
  host.replaceChildren();
  const profile = data.profile || {};
  const plan = data.tokenPlanDetail || {};
  const verification = data.verification || {};
  const rate = (data.usage && data.usage.accountRateLimit) || {};

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
  kvRow(accountCard, "ID", profile.userId || "--");
  kvRow(accountCard, "手机", profile.phone || "--");
  kvRow(accountCard, "邮箱", profile.email || "--");
  kvRow(accountCard, "微信", profile.weixin ? "已绑定" : "未绑定");
  kvRow(accountCard, "实名认证", verifyText);

  // 限速（来自 usage.accountRateLimit，搭在 /api/v1/usage 段里）
  const rateCard = card("限速");
  kvRow(rateCard, "请求 TPM", rate.tpm != null ? compactNum(rate.tpm) : "--");
  kvRow(rateCard, "请求 RPM", rate.rpm != null ? num(rate.rpm, 0) : "--");
  kvRow(rateCard, "查询 TPM", rate.queryTpm != null ? compactNum(rate.queryTpm) : "--");
  kvRow(rateCard, "并发", rate.concurrency != null ? num(rate.concurrency, 0) : "--");

  host.append(planCard, accountCard, rateCard);
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

/* 指定窗口的用量统计行：带 month → 按日行；只带 year → 按（月）聚合行 */
async function fetchTrendRows(year, month) {
  const query = month ? `year=${year}&month=${month}` : `year=${year}`;
  try {
    const response = await fetch(`${API}/usage/trend?${query}`, fetchOptions());
    if (!response.ok) return [];
    const body = await response.json();
    return Array.isArray(body.data) ? body.data : [];
  } catch (_) {
    return [];
  }
}

function previousYearMonth({ year, month }) {
  return month === 1 ? { year: year - 1, month: 12 } : { year, month: month - 1 };
}

async function load() {
  const updated = $("updated");
  updated.textContent = "加载中…";
  try {
    const ym = currentYearMonth();
    const prev = previousYearMonth(ym);
    const [response, prevMonthRows, curYearRows, prevYearRows] = await Promise.all([
      fetch(`${API}/overview?year=${ym.year}&month=${ym.month}`, fetchOptions()),
      fetchTrendRows(prev.year, prev.month),
      fetchTrendRows(ym.year),
      fetchTrendRows(ym.year - 1),
    ]);
    if (!response.ok) {
      let message = `HTTP ${response.status}`;
      let type = "";
      try {
        const body = await response.json();
        message = (body.error && body.error.message) || message;
        type = (body.error && body.error.type) || "";
      } catch (_) { /* ignore */ }
      // 认证失败两种：没带 key（403 forbidden）、带了但不对（401 unauthorized）
      if (type === "unauthorized" || type === "forbidden") {
        throw new Error(
          "未授权：请用 " + window.location.pathname + "?key=你的密钥 打开一次本页"
          + "（换过密钥也要重新打开一次）"
        );
      }
      throw new Error(message);
    }
    const payload = await response.json();
    const data = payload.data || {};
    // 全期 tokens：近两年按（月）聚合行求和
    const yearlyRows = [...curYearRows, ...prevYearRows];
    // 近 30 天窗口：当月按日行 + 上月按日行，由 buildTrendWindow 裁到窗口
    const windowRows = [
      ...(Array.isArray(data.usageTrend) ? data.usageTrend : []),
      ...prevMonthRows,
    ];
    const points = buildTrendWindow(windowRows, 30);
    state.reauth = null;
    renderStats(data, yearlyRows);
    renderTokenTrend(points);
    renderModelBreakdown(points);
    renderAccountCards(data);
    renderErrors(payload.meta);

    const plan = (data.tokenPlanDetail && data.tokenPlanDetail.planName) || "--";
    $("plan").textContent = plan;
    updated.textContent = `更新于 ${(payload.meta && payload.meta.generatedAt) || new Date().toISOString()}`;
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
}

boot();
