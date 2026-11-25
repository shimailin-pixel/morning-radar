/* 每日投研雷达 · 前端交互 */
'use strict';

const state = {
  overview: null,
  sectors: [],
  picks: {},
  currentStock: null,
  currentSector: '',
  klineChart: null,
  sectorChart: null,
  sectorView: 'treemap',
  aiCache: {},
  activeTab: 'radar',
  brief: null,
  watchlist: null,
  positionAdvice: {},   // code -> 持仓去留诊断结果（缓存，避免重复烧 ARTi）
  abCache: {},          // kind -> ARTi 官方晨报（避免重复请求）
  abKind: 'morning',
  dipCache: null,       // 低吸观察池
  dipGroup: 'b',        // a=超跌型 b=缩量企稳型
  aiKind: 'news',       // 当前选中的分析标签
  artiReal: false,      // 是否配置了真实 ARTi key
  holdingCodes: null,   // 当前持仓代码集合(纯6位)，用于低吸池"加入持仓"按钮状态
};

const $ = (s) => document.querySelector(s);
const el = (id) => document.getElementById(id);
const fmt = (v, d = 2) => (v === null || v === undefined || isNaN(v)) ? '-' : Number(v).toFixed(d);
const escHtml = s => String(s == null ? '' : s).replace(/[&<>"']/g, c =>
  ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const colorOf = (v) => v > 0 ? '#d9423a' : v < 0 ? '#1a9d63' : '#9aa1ac';
function mixColor(light, dark, t) {
  const parse = (h) => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16));
  const a = parse(light), b = parse(dark);
  const c = a.map((v, i) => Math.round(v + (b[i] - v) * t));
  return '#' + c.map(v => v.toString(16).padStart(2, '0')).join('');
}

/* ---------- 极简 markdown 渲染（逐行处理，避免冗余空段） ---------- */
function renderMarkdown(text) {
  if (!text) return '';
  const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const inline = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  const lines = text.split('\n');
  const out = [];
  let inList = false;
  const closeList = () => { if (inList) { out.push('</ul>'); inList = false; } };
  for (const raw of lines) {
    const line = raw.trim();
    if (!line) { continue; }
    if (/^#{1,3} /.test(line)) {
      closeList();
      out.push('<h2>' + inline(line.replace(/^#{1,3} /, '')) + '</h2>');
    } else if (/^&gt; /.test(esc(line)) || line.startsWith('> ')) {
      closeList();
      out.push('<blockquote>' + inline(line.replace(/^&gt; |^> /, '')) + '</blockquote>');
    } else if (/^[-*] /.test(line) || /^\d+\. /.test(line)) {
      if (!inList) { out.push('<ul>'); inList = true; }
      out.push('<li>' + inline(line.replace(/^([-*]|\d+\.) /, '')) + '</li>');
    } else {
      closeList();
      out.push('<p>' + inline(line) + '</p>');
    }
  }
  closeList();
  return out.join('');
}

/* ---------- 首屏加载 ---------- */
async function loadOverview() {
  try {
    const r = await fetch('/api/overview');
    state.overview = await r.json();
    state.sectors = state.overview.sectors;
    state.picks = state.overview.picks;
    el('update-time').textContent = '更新于 ' + state.overview.updated;
    renderBrief(state.overview.brief);
    renderSectors();
    renderPickGroups();
  } catch (e) {
    el('brief-bar').innerHTML = '<div class="brief-loading">加载失败：' + e.message + '</div>';
  }
}

async function loadStatus() {
  try {
    const r = await fetch('/api/status');
    const s = await r.json();
    const badge = el('status-badge');
    const realArti = s.arti_mode !== 'mock';
    badge.className = 'badge badge-real';
    badge.textContent = realArti ? '数据引擎 + ARTi' : '数据引擎 · 本地';
    state.artiReal = realArti;
    updateAiModeBadge();
    if (s.db_mode) {
      el('footer-source').textContent = `行情：${s.db_mode}${s.db_date ? ' · 截至 ' + s.db_date : ''} · 新闻：实时抓取 · 分析：数据驱动`;
    }
  } catch (e) {}
}

function renderBrief(md) {
  el('brief-body').innerHTML = renderMarkdown(md);
}

function toggleBrief() {
  const bar = el('brief-bar');
  const collapsed = bar.classList.toggle('collapsed');
  el('brief-toggle').textContent = collapsed ? '展开 ▾' : '收起 ▴';
}

/* ---------- 板块轮动 ---------- */
function renderSectors() {
  if (state.sectorView === 'treemap') renderTreemap();
  else renderSectorList();
}

function renderTreemap() {
  const box = el('sector-treemap');
  if (!state.sectors.length) { box.innerHTML = '<div class="empty-state">暂无板块数据</div>'; return; }
  if (!state.sectorChart) state.sectorChart = echarts.init(box);
  // 显式颜色映射（红涨绿跌），不依赖 visualMap 的嵌套取值
  const maxAbs = Math.max(1, ...state.sectors.map(s => Math.abs(s.change_pct)));
  const data = state.sectors.map(s => {
    const chg = s.change_pct;
    // 按涨跌幅深浅插值
    const intensity = Math.min(1, Math.abs(chg) / maxAbs);
    let color;
    if (chg > 0) color = mixColor('#f4d4d1', '#d9423a', intensity);
    else if (chg < 0) color = mixColor('#d3ecdf', '#1a9d63', intensity);
    else color = '#eef0f3';
    return {
      name: s.name,
      value: Math.max(1, Math.round(s.turnover || 50)),
      chg: chg,
      score: s.momentum_score,
      itemStyle: { color: color },
    };
  });
  state.sectorChart.setOption({
    tooltip: {
      formatter: (p) => {
        const d = p.data || {};
        const chg = d.chg;
        return `<b>${p.name}</b><br/>涨跌：<span style="color:${colorOf(chg)}">${chg > 0 ? '+' : ''}${fmt(chg)}%</span><br/>轮动评分：${fmt(d.score, 1)}`;
      },
    },
    series: [{
      type: 'treemap',
      roam: false,
      nodeClick: false,
      breadcrumb: { show: false },
      label: {
        show: true,
        formatter: (p) => {
          const chg = (p.data && p.data.chg) || 0;
          return `${p.name}\n${chg > 0 ? '+' : ''}${fmt(chg)}%`;
        },
        fontSize: 11,
        color: '#1f2329',
      },
      itemStyle: { borderColor: '#fff', borderWidth: 2, gapWidth: 2 },
      data: data,
    }],
  }, true);
  state.sectorChart.off('click');
  state.sectorChart.on('click', (p) => {
    if (p.data && p.data.name) openSector(p.data.name);
  });
}

function renderSectorList() {
  const box = el('sector-list');
  const sorted = [...state.sectors].sort((a, b) => b.momentum_score - a.momentum_score);
  const maxAbs = Math.max(1, ...sorted.map(s => Math.abs(s.change_pct)));
  box.innerHTML = sorted.map(s => {
    const chg = s.change_pct;
    const w = Math.abs(chg) / maxAbs * 50;
    const barStyle = chg >= 0
      ? `left:50%;width:${w}%;background:#d9423a`
      : `right:50%;width:${w}%;background:#1a9d63`;
    return `<div class="sector-row" data-name="${s.name}">
      <span class="sector-row-name">${s.name}</span>
      <span class="sector-row-bar"><i style="${barStyle}"></i></span>
      <span class="sector-row-chg ${chg > 0 ? 'up' : chg < 0 ? 'down' : 'flat'}">${chg > 0 ? '+' : ''}${fmt(chg)}%</span>
      <span class="sector-row-score">${fmt(s.momentum_score, 0)}分</span>
    </div>`;
  }).join('');
  box.querySelectorAll('.sector-row').forEach(row => {
    row.addEventListener('click', () => openSector(row.dataset.name));
  });
}

async function openSector(name) {
  el('picks-subtitle').textContent = name + ' · 技术面精选';
  const box = el('picks-container');
  box.innerHTML = '<div class="empty-state">正在分析 ' + name + ' 成分股...</div>';
  try {
    const r = await fetch('/api/sector/' + encodeURIComponent(name) + '/stocks');
    const data = await r.json();
    state.picks[name] = data;
    renderPickGroups();
  } catch (e) {
    box.innerHTML = '<div class="empty-state">加载失败：' + e.message + '</div>';
  }
}

function renderPickGroups() {
  const box = el('picks-container');
  const top = state.overview ? state.overview.top_sectors : Object.keys(state.picks);
  let html = '';
  top.forEach(name => {
    const g = state.picks[name];
    if (!g || !g.stocks || !g.stocks.length) return;
    const src = g.data_source === '模拟K线' ? '<span class="glow">模拟数据</span>' : '';
    html += `<div class="pick-group">
      <div class="pick-group-head">
        <h3>${name} ${src}</h3>
        <span class="gcount">评估${g.evaluated}只 · 精选${g.stocks.length}只</span>
      </div>`;
    g.stocks.forEach((s, i) => {
      // 优先展示带数值的差异化标签；无则退回文字信号
      const tags = (s.highlights && s.highlights.length ? s.highlights : s.signals).slice(0, 4);
      const sigs = tags.map(x => `<span class="sig-tag">${x}</span>`).join('');
      // 五维评分条：直观看出每只股票强在哪
      const dimMax = {趋势:30, 动量:25, 量能:20, 位置:15, 超跌反弹:10};
      const bars = s.dims && Object.keys(s.dims).length
        ? `<div class="dim-bars">${Object.entries(dimMax).map(([k,mx]) => {
            const v = s.dims[k] || 0, pct = Math.round(v / mx * 100);
            return `<span class="dim-bar" title="${k} ${v}/${mx}"><i style="width:${pct}%;background:${pct>=70?'#d9534f':pct>=40?'#e8a33d':'#c8cdd6'}"></i></span>`;
          }).join('')}</div>` : '';
      html += `<div class="stock-card" data-code="${s.code}" data-name="${s.name}" data-sector="${name}">
        <div class="stock-rank">${i + 1}</div>
        <div class="stock-main">
          <div class="stock-name">${s.name} <span class="stock-code">${s.code}</span></div>
          <div class="stock-signals">${sigs}</div>
          ${bars}
        </div>
        <div class="stock-right">
          <div class="stock-score">${s.score}</div>
          <div class="stock-score-label">技术评分</div>
          <div class="stock-chg ${s.change_pct > 0 ? 'up' : s.change_pct < 0 ? 'down' : 'flat'}">${s.change_pct > 0 ? '+' : ''}${fmt(s.change_pct)}%</div>
        </div>
      </div>`;
    });
    html += '</div>';
  });
  box.innerHTML = html || '<div class="empty-state">暂无精选个股</div>';
  box.querySelectorAll('.stock-card').forEach(card => {
    card.addEventListener('click', () => {
      openStock(card.dataset.code, card.dataset.name, card.dataset.sector);
    });
  });
}

/* ---------- 股票详情抽屉 ---------- */
async function openStock(code, name, sector) {
  state.currentStock = { code, name, sector };
  state.currentSector = sector;
  state.aiCache = {};
  // 从全市场精选 / 低吸观察池两处找详情（低吸股票不在 picks 里）
  let detail = null;
  Object.values(state.picks).forEach(g => {
    (g.stocks || []).forEach(s => { if (s.code === code) detail = s; });
  });
  if (!detail && state.dipCache) {
    const hit = [...(state.dipCache.group_a || []), ...(state.dipCache.group_b || [])]
      .find(x => x.code === code);
    if (hit) {
      detail = {
        ...hit,
        highlights: [
          '量比' + fmt(hit.volr, 2),
          '近3日+' + fmt(hit.ret3, 1) + '%',
          '换手' + fmt(hit.turnover, 2) + '%',
          '板块资金+' + fmt(hit.sector_flow, 2) + '亿',
        ],
      };
    }
  }
  el('d-stock-name').textContent = name;
  el('d-stock-code').textContent = code;
  el('d-sector').textContent = sector;
  el('d-score').textContent = detail ? detail.score : '-';
  if (detail) {
    const dTags = (detail.highlights && detail.highlights.length ? detail.highlights : detail.signals).slice(0, 5);
    el('d-signals').innerHTML = dTags.map(x => `<span class="sig-chip">${x}</span>`).join('');
    const extraStat = detail.volr != null
      ? `<div class="meta-stat"><span class="l">量比</span><span class="v">${fmt(detail.volr, 2)}</span></div>
         <div class="meta-stat"><span class="l">换手</span><span class="v">${fmt(detail.turnover, 2)}%</span></div>`
      : `<div class="meta-stat"><span class="l">RSI</span><span class="v">${fmt(detail.rsi, 0)}</span></div>`;
    el('d-stats').innerHTML = `
      <div class="meta-stat"><span class="l">现价</span><span class="v">${fmt(detail.close)}</span></div>
      <div class="meta-stat"><span class="l">近20日</span><span class="v ${detail.ret20 > 0 ? 'up' : 'down'}">${detail.ret20 > 0 ? '+' : ''}${fmt(detail.ret20)}%</span></div>
      ${extraStat}`;
  } else {
    el('d-signals').innerHTML = '';
    el('d-stats').innerHTML = '';
  }
  el('ai-content').innerHTML = '<div class="ai-placeholder">点击上方标签生成对应分析（首次生成需几秒）</div>';
  document.querySelectorAll('.ai-tab').forEach(t => t.classList.toggle('active', t.dataset.kind === 'news'));
  el('drawer-overlay').classList.add('show');
  el('drawer').classList.add('show');
  loadNews(code);
  loadAI('news');  // 默认加载新闻分析，打开即见内容
  await loadKline(code);
  // 抽屉滑入动画期间容器宽度可能未定型，动画结束后再 resize 一次，防止白屏/错位
  setTimeout(() => { try { state.klineChart && state.klineChart.resize(); } catch (e) {} }, 350);
}

function closeDrawer() {
  el('drawer-overlay').classList.remove('show');
  el('drawer').classList.remove('show');
}

async function loadKline(code) {
  const box = el('kline-chart');
  // 每次重建：先销毁旧 ECharts 实例。
  // 否则 innerHTML 清空会删掉 canvas，但旧实例仍绑定已失效的 DOM，第二次打开就白屏。
  if (state.klineChart) {
    try { state.klineChart.dispose(); } catch (e) {}
    state.klineChart = null;
  }
  box.innerHTML = '<div class="ai-loading"><div class="spinner"></div>加载K线数据...</div>';
  try {
    const r = await fetch('/api/kline/' + code + '?days=120');
    const data = await r.json();
    box.innerHTML = '';
    state.klineChart = echarts.init(box);
    drawKline(data.kline);
    el('chart-source').textContent = 'K线数据源：' + (data.source || '-') + ' · 前复权';
  } catch (e) {
    box.innerHTML = '<div class="ai-placeholder">K线加载失败：' + e.message + '</div>';
  }
}

let currentIndicator = 'ma';
let currentKline = null;

function drawKline(rows) {
  currentKline = rows;
  const dates = rows.map(r => r.date);
  const ohlc = rows.map(r => [r.open, r.close, r.low, r.high]);
  const volumes = rows.map(r => ({
    value: r.volume,
    itemStyle: { color: r.close >= r.open ? '#d9423a' : '#1a9d63' },
  }));

  const option = {
    animation: false,
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      backgroundColor: 'rgba(255,255,255,.96)',
      borderColor: '#e8eaed',
      textStyle: { color: '#1f2329', fontSize: 11 },
    },
    legend: { data: [], top: 4, textStyle: { fontSize: 10, color: '#5f6672' } },
    grid: [
      { left: 52, right: 18, top: 30, height: '52%' },
      { left: 52, right: 18, top: '68%', height: '12%' },
      { left: 52, right: 18, top: '82%', height: '13%' },
    ],
    xAxis: [
      { type: 'category', data: dates, gridIndex: 0, axisLabel: { show: false }, axisLine: { lineStyle: { color: '#e8eaed' } } },
      { type: 'category', data: dates, gridIndex: 1, axisLabel: { show: false }, axisTick: { show: false } },
      { type: 'category', data: dates, gridIndex: 2, axisLabel: { fontSize: 10, color: '#9aa1ac' }, axisTick: { show: false } },
    ],
    yAxis: [
      { scale: true, gridIndex: 0, splitLine: { lineStyle: { color: '#f0f2f5' } }, axisLabel: { fontSize: 10, color: '#9aa1ac' } },
      { scale: true, gridIndex: 1, splitLine: { show: false }, axisLabel: { show: false } },
      { scale: true, gridIndex: 2, splitLine: { show: false }, axisLabel: { fontSize: 10, color: '#9aa1ac' } },
    ],
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    series: [
      {
        name: 'K线', type: 'candlestick', data: ohlc,
        xAxisIndex: 0, yAxisIndex: 0,
        itemStyle: { color: '#d9423a', color0: '#1a9d63', borderColor: '#d9423a', borderColor0: '#1a9d63' },
      },
      {
        name: '成交量', type: 'bar', data: volumes,
        xAxisIndex: 1, yAxisIndex: 1,
      },
    ],
  };

  const line = (name, key, color, grid, extra = {}) => ({
    name, type: 'line', data: rows.map(r => r[key]),
    xAxisIndex: grid, yAxisIndex: grid,
    smooth: true, showSymbol: false,
    lineStyle: { width: 1.2, color },
    itemStyle: { color }, ...extra,
  });

  if (currentIndicator === 'ma') {
    option.legend.data = ['MA5', 'MA10', 'MA20', 'MA60'];
    option.series.push(
      line('MA5', 'ma5', '#d98a12', 0),
      line('MA10', 'ma10', '#2b5aed', 0),
      line('MA20', 'ma20', '#6c4bd8', 0),
      line('MA60', 'ma60', '#5f6672', 0)
    );
  } else if (currentIndicator === 'boll') {
    option.legend.data = ['BOLL上轨', '中轨', '下轨'];
    option.series.push(
      line('BOLL上轨', 'boll_up', '#d9423a', 0),
      line('中轨', 'boll_mid', '#5f6672', 0, { lineStyle: { width: 1, color: '#5f6672', type: 'dashed' } }),
      line('下轨', 'boll_low', '#1a9d63', 0)
    );
  } else if (currentIndicator === 'macd') {
    option.legend.data = ['DIF', 'DEA', 'MACD'];
    option.grid[2] = { left: 52, right: 18, top: '82%', height: '13%' };
    option.series.push(
      line('DIF', 'dif', '#d98a12', 2),
      line('DEA', 'dea', '#2b5aed', 2),
      {
        name: 'MACD', type: 'bar', xAxisIndex: 2, yAxisIndex: 2,
        data: rows.map(r => ({
          value: r.macd,
          itemStyle: { color: r.macd >= 0 ? '#d9423a' : '#1a9d63' },
        })),
      }
    );
  } else if (currentIndicator === 'rsi') {
    option.legend.data = ['RSI(14)'];
    option.yAxis[2] = { min: 0, max: 100, gridIndex: 2, splitLine: { show: false }, axisLabel: { fontSize: 10, color: '#9aa1ac' } };
    option.series.push(line('RSI(14)', 'rsi', '#6c4bd8', 2));
    option.series.push({
      name: '超买线', type: 'line', xAxisIndex: 2, yAxisIndex: 2,
      data: rows.map(() => 70), symbol: 'none',
      lineStyle: { color: '#d9423a', type: 'dashed', width: 1 },
    });
    option.series.push({
      name: '超卖线', type: 'line', xAxisIndex: 2, yAxisIndex: 2,
      data: rows.map(() => 30), symbol: 'none',
      lineStyle: { color: '#1a9d63', type: 'dashed', width: 1 },
    });
  } else if (currentIndicator === 'kdj') {
    option.legend.data = ['K', 'D', 'J'];
    option.series.push(
      line('K', 'k', '#d98a12', 2),
      line('D', 'd', '#2b5aed', 2),
      line('J', 'j', '#6c4bd8', 2)
    );
  }

  state.klineChart.setOption(option, true);
}

/* ---------- AI 三维分析 ---------- */
// 徽标随当前标签切换：ARTi 独立研报 tab 强调纯联网，其余三个为本地数据+ARTi 增强
function updateAiModeBadge() {
  const badge = el('ai-mode');
  if (!badge) return;
  if (state.aiKind === 'arti') {
    badge.textContent = state.artiReal ? 'ARTi 联网研报' : 'ARTi 未接入 · 本地';
    badge.classList.add('ai-badge-live');
  } else {
    badge.textContent = state.artiReal ? '本地数据 + ARTi' : '本地数据引擎';
    badge.classList.remove('ai-badge-live');
  }
}

// 本地数据结果秒回；ARTi 深度解读在后台生成，拿到后追加到同一块内容里。
async function loadAI(kind) {
  if (!state.currentStock) return;
  const box = el('ai-content');
  const code = state.currentStock.code;
  state.aiKind = kind;
  updateAiModeBadge();
  // ARTi 独立研报：纯 ARTi 内容，走专门的一套加载/轮询/重试
  if (kind === 'arti') return loadArtiTab(code, box);
  const key = code + ':' + kind;
  const label = ({news:'新闻分析',report:'投研报告',council:'大师总结',arti:'ARTi 智能解读'}[kind]) || '分析';
  if (state.aiCache[key]) { box.innerHTML = renderMarkdown(state.aiCache[key]); pollArti(code, kind, key, box); return; }
  box.innerHTML = '<div class="ai-loading"><div class="spinner"></div>正在汇总本地真实数据…</div>';
  try {
    const url = '/api/analyze/' + code + '?sector=' + encodeURIComponent(state.currentSector) + '&kind=' + kind;
    const r = await fetch(url);
    const data = await r.json();
    let content = data.content || '（无内容）';
    state.aiCache[key] = content;
    box.innerHTML = renderMarkdown(content);
    // 本地结果已可见；若 ARTi 正在后台增强，挂一个轻量状态条并轮询补齐
    if (data.arti_pending) {
      appendArtiHint(box, 'ARTi 智能解读生成中（通常 10~30 秒）…');
      pollArti(code, kind, key, box);
    }
  } catch (e) {
    box.innerHTML = '<div class="ai-placeholder">生成失败：' + e.message + '</div>';
  }
}

function appendArtiHint(box, text) {
  if (!box) return;
  const old = box.querySelector('.arti-pending-hint');
  if (old) old.remove();
  const hint = document.createElement('div');
  hint.className = 'arti-pending-hint';
  hint.innerHTML = '<span class="spinner sm"></span>' + text;
  box.appendChild(hint);
}

// 轮询后台 ARTi 增强结果，最多约 60 秒。拿到后整段重渲染，避免重复拼接。
async function pollArti(code, kind, cacheKey, box) {
  // 若用户已切走股票/标签，停止轮询，避免把内容写到别的面板
  const stillActive = () => state.currentStock && state.currentStock.code === code
    && el('ai-content') === box && box.isConnected;
  let lastInfo = null;
  for (let i = 0; i < 40; i++) {
    await new Promise(res => setTimeout(res, 2500));
    if (!stillActive()) return;
    try {
      const d = await fetch('/api/analyze/' + code + '/arti?kind=' + kind).then(x => x.json());
      if (d.ready && d.content) {
        const base = state.aiCache[cacheKey] || '';
        // 用主接口缓存的纯本地结果做 base（若之前已拼过则去掉旧的 ARTi 段）
        const cleanBase = base.split('\n\n---\n\n### ARTi 智能解读')[0];
        const full = cleanBase + '\n\n---\n\n### ARTi 智能解读\n\n' + d.content;
        state.aiCache[cacheKey] = full;
        box.innerHTML = renderMarkdown(full);
        return;
      }
      lastInfo = d;
      // 重试中：把"第N次尝试"实时反馈到提示条，不让用户以为卡死
      if (d.running && d.attempts > 0) {
        const hint = box.querySelector('.arti-pending-hint:not(.arti-failed)');
        if (hint) hint.innerHTML = '<span class="spinner sm"></span>ARTi 服务端波动，正在第 ' +
          (d.attempts + 1) + ' 次尝试（最多 3 次）…';
      }
      // 后台已判定失败（拒答/502等），不再干等满 60 秒，直接展示原因+重试
      if (d.failed && d.reason) {
        showArtiFailure(box, d.reason, d.attempts, code, kind, cacheKey);
        return;
      }
    } catch (e) { /* 网络抖动继续轮询 */ }
  }
  // 超时：把提示换成降级说明 + 重试按钮
  if (stillActive()) {
    const reason = lastInfo && lastInfo.reason ? lastInfo.reason : 'ARTi 服务端繁忙或响应超时';
    showArtiFailure(box, reason, lastInfo && lastInfo.attempts, code, kind, cacheKey);
  }
}

function showArtiFailure(box, reason, attempts, code, kind, cacheKey) {
  if (!box || !box.isConnected) return;
  const old = box.querySelector('.arti-pending-hint');
  if (old) old.remove();
  const hint = document.createElement('div');
  hint.className = 'arti-pending-hint arti-failed';
  const is502 = /502|timeout|超时|timed out/i.test(reason || '');
  const attemptTxt = attempts ? '（已重试 ' + attempts + ' 次）' : '';
  const explain = is502
    ? 'ARTi 服务端当前不稳定' + attemptTxt + '。上面本地数据分析不受影响，稍后点「重试解读」通常可成功。'
    : 'ARTi 深度解读未完成' + attemptTxt + '：' + escapeHtml(reason || '服务端繁忙') + '。本地数据分析不受影响。';
  hint.innerHTML = '<span>⚠ ' + explain + '</span>' +
    '<button class="arti-retry-btn" type="button">重试解读</button>';
  hint.querySelector('.arti-retry-btn').addEventListener('click', async () => {
    delete state.aiCache[cacheKey];
    const btn = hint.querySelector('.arti-retry-btn');
    btn.disabled = true; btn.textContent = '重试中…';
    try {
      const url = '/api/analyze/' + code + '?sector=' + encodeURIComponent(state.currentSector) +
        '&kind=' + kind + '&force=1';
      const data = await fetch(url).then(x => x.json());
      const content = data.content || '（无内容）';
      const cleanBase = content.split('\n\n---\n\n### ARTi 智能解读')[0];
      state.aiCache[cacheKey] = cleanBase;
      box.innerHTML = renderMarkdown(cleanBase);
      appendArtiHint(box, 'ARTi 重新生成中…');
      pollArti(code, kind, cacheKey, box);
    } catch (e) {
      btn.disabled = false; btn.textContent = '重试解读';
    }
  });
  box.appendChild(hint);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

/* ---------- ARTi 智能解读（独立 tab，纯 ARTi 内容） ---------- */
const artiTabActive = (code, box) => state.currentStock && state.currentStock.code === code
  && el('ai-content') === box && box.isConnected;

async function loadArtiTab(code, box) {
  const key = code + ':arti';
  // 已成功过的专属研报，直接展示
  if (state.aiCache[key]) { box.innerHTML = renderMarkdown(state.aiCache[key]); return; }
  box.innerHTML = '<div class="ai-loading"><div class="spinner"></div>正在向 ARTi 请求独立研报…</div>';

  // 兜底：若新闻/报告/大师里已有成功的 ARTi 解读，先展示，避免空白干等（服务端不稳定）
  let reused = null;
  try {
    const others = await Promise.all(['news', 'report', 'council'].map(k =>
      fetch('/api/analyze/' + code + '/arti?kind=' + k).then(x => x.json()).catch(() => ({}))));
    reused = others.find(d => d.ready && d.content);
  } catch (e) {}
  if (!artiTabActive(code, box)) return;

  // 专属研报若已生成，直接显示
  let st = {};
  try { st = await fetch('/api/analyze/' + code + '/arti?kind=arti').then(x => x.json()); } catch (e) {}
  if (st.ready && st.content) {
    state.aiCache[key] = st.content;
    box.innerHTML = renderMarkdown(st.content);
    return;
  }
  if (!artiTabActive(code, box)) return;

  if (reused && reused.content) {
    box.innerHTML = '<div class="arti-reuse-hint">先展示已核实的 ARTi 解读；专属研报仍在后台生成…</div>'
      + renderMarkdown(reused.content);
  } else {
    box.innerHTML = '<div class="ai-loading"><div class="spinner"></div>ARTi 正在联网生成独立研报…</div>';
  }
  // force=1 清掉历史失败状态，重新提交后台增强
  try {
    await fetch('/api/analyze/' + code + '?sector=' + encodeURIComponent(state.currentSector) + '&kind=arti&force=1');
  } catch (e) {}
  if (!artiTabActive(code, box)) return;
  appendArtiHint(box, 'ARTi 独立研报生成中（通常 10~40 秒）…');
  pollArtiTab(code, box, key);
}

async function pollArtiTab(code, box, key) {
  let lastInfo = null;
  for (let i = 0; i < 40; i++) {
    await new Promise(res => setTimeout(res, 2500));
    if (!artiTabActive(code, box)) return;
    try {
      const d = await fetch('/api/analyze/' + code + '/arti?kind=arti').then(x => x.json());
      if (d.ready && d.content) {
        state.aiCache[key] = d.content;
        box.innerHTML = renderMarkdown(d.content);   // 纯 ARTi，不拼本地 base
        return;
      }
      lastInfo = d;
      if (d.running && d.attempts > 0) {
        const hint = box.querySelector('.arti-pending-hint:not(.arti-failed)');
        if (hint) hint.innerHTML = '<span class="spinner sm"></span>ARTi 服务端波动，正在第 '
          + (d.attempts + 1) + ' 次尝试（最多 3 次）…';
      }
      if (d.failed && d.reason) {
        showArtiFailureTab(box, d.reason, d.attempts, code, key);
        return;
      }
    } catch (e) { /* 网络抖动继续轮询 */ }
  }
  if (artiTabActive(code, box)) {
    const reason = lastInfo && lastInfo.reason ? lastInfo.reason : 'ARTi 服务端繁忙或响应超时';
    showArtiFailureTab(box, reason, lastInfo && lastInfo.attempts, code, key);
  }
}

function showArtiFailureTab(box, reason, attempts, code, key) {
  if (!box || !box.isConnected) return;
  const old = box.querySelector('.arti-pending-hint');
  if (old) old.remove();
  const hint = document.createElement('div');
  hint.className = 'arti-pending-hint arti-failed';
  const is502 = /502|timeout|超时|timed out/i.test(reason || '');
  const attemptTxt = attempts ? '（已重试 ' + attempts + ' 次）' : '';
  const explain = is502
    ? 'ARTi 服务端当前不稳定' + attemptTxt + '，独立研报暂时没能生成。稍后点「重试解读」通常可成功。'
    : 'ARTi 独立研报未完成' + attemptTxt + '：' + escapeHtml(reason || '服务端繁忙') + '。';
  hint.innerHTML = '<span>⚠ ' + explain + '</span>'
    + '<button class="arti-retry-btn" type="button">重试解读</button>';
  hint.querySelector('.arti-retry-btn').addEventListener('click', async () => {
    delete state.aiCache[key];
    const btn = hint.querySelector('.arti-retry-btn');
    btn.disabled = true; btn.textContent = '重试中…';
    try {
      await fetch('/api/analyze/' + code + '?sector=' + encodeURIComponent(state.currentSector)
        + '&kind=arti&force=1');
      appendArtiHint(box, 'ARTi 重新生成中…');
      pollArtiTab(code, box, key);
    } catch (e) {
      btn.disabled = false; btn.textContent = '重试解读';
    }
  });
  box.appendChild(hint);
}

/* ---------- ARTi 官方晨报 ---------- */
async function loadArtiBrief(force = false) {
  const kind = state.abKind;
  const content = el('ab-content');
  if (!force && state.abCache[kind]) {
    renderArtiBrief(state.abCache[kind]);
    return;
  }
  content.innerHTML = '<div class="ai-loading"><div class="spinner"></div>正在读取 ARTi 官方晨报…</div>';
  el('ab-title').textContent = '正在加载晨报…';
  el('ab-meta').textContent = '';
  el('ab-summary').innerHTML = '';
  try {
    const url = '/api/arti-brief' + (force ? '?force=1' : '') + (kind !== 'morning' ? (force ? '&' : '?') + 'kind=' + kind : '');
    const d = await fetch(url).then(x => x.json());
    if (!d.ok || !d.html) {
      content.innerHTML = '<div class="ai-placeholder">晨报暂不可用：' + (d.error || '暂无内容') + '</div>';
      return;
    }
    state.abCache[kind] = d;
    renderArtiBrief(d);
  } catch (e) {
    content.innerHTML = '<div class="ai-placeholder">晨报加载失败：' + e.message + '</div>';
  }
}

function renderArtiBrief(d) {
  el('ab-title').textContent = d.title || 'ARTi 晨报';
  const tags = [d.date, d.from_cache ? '缓存' : '实时', d.fallback ? '最近一期' : '', d.kind === 'morning' ? '早报' : '晚报'].filter(Boolean);
  el('ab-meta').textContent = tags.join(' · ');
  if (d.summary) {
    el('ab-summary').innerHTML = '<strong>一句话摘要：</strong>' + escapeHtml(d.summary);
  }
  el('ab-content').innerHTML = sanitizeBriefHtml(d.html || '');
  // 原文链接指向对应日期的官方页面
  try {
    const day = (d.slug || '').replace(/^(morning|evening)-/, '');
    el('ab-origin').href = 'https://www.artifin.ai/briefs/' + (d.kind === 'morning' ? 'morning' : 'evening') + '-' + day + '?lang=zh-Hans';
  } catch (_) {}
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// 清洗官方 body_html：保留语义标签，去掉 aside 装饰块和 class/aria 属性，
// 并把行情 code 行按涨跌着红绿色（A股习惯：涨红跌绿）。
function sanitizeBriefHtml(html) {
  if (!html) return '';
  let s = html;
  // 去掉整块 aside（数据区间说明，页面已用 meta 展示）
  s = s.replace(/<aside\b[^>]*>[\s\S]*?<\/aside>/gi, '');
  // 去所有属性，仅保留裸标签，避免官方样式类名污染
  s = s.replace(/<(h2|h3|h4|p|ol|ul|li|strong|code|br|em|b)(\s[^>]*)?>/gi, '<$1>');
  // 行情 code 行：给涨跌数字上色
  s = s.replace(/<code>([^<]*)<\/code>/g, (m, t) => {
    const up = /[+＋]\s*\d|📈/.test(t);
    const down = /[-－]\s*\d|📉/.test(t);
    const cls = up ? 'ab-up' : down ? 'ab-down' : '';
    return '<code' + (cls ? ' class="' + cls + '"' : '') + '>' + t + '</code>';
  });
  // 白名单：仅放行上述裸标签 + 文本
  const allowed = /^<\/?(h2|h3|h4|p|ol|ul|li|strong|code|br|em|b)(\s+class="ab-(up|down)")?>$/i;
  s = s.replace(/<\/?[a-zA-Z][^>]*>/g, tag => allowed.test(tag) ? tag : '');
  // 👉 提示箭头加个视觉区分
  s = s.replace(/👉/g, '<span class="ab-arrow">👉</span>');
  return s;
}

/* ---------- 低吸观察池 ---------- */
async function loadDip(force = false) {
  const table = el('dip-table');
  if (!force && state.dipCache) { renderDip(); return; }
  table.innerHTML = '<div class="ai-loading"><div class="spinner"></div>正在筛选低吸候选…</div>';
  try {
    const r = await fetch('/api/dip-picks' + (force ? '?force=1' : ''));
    const d = await r.json();
    if (!d.ok) { table.innerHTML = '<div class="ai-placeholder">筛选失败：' + (d.error || '未知错误') + '</div>'; return; }
    state.dipCache = d;
    renderDip();
  } catch (e) {
    table.innerHTML = '<div class="ai-placeholder">加载失败：' + e.message + '</div>';
  }
}

/* 真资金流入板块：完整列出，不再塞在免责声明的一行小字里 */
function renderDipFlow(d) {
  const box = el('dip-flow-sectors');
  if (!box) return;
  const fs = d.flow_sectors || d.positive_sectors || [];
  if (!fs.length) {
    box.innerHTML = '<div class="dip-flow-empty">实时板块资金：<b>今日没有净流入为正的板块</b> —— 资金普遍流出时，宁可空仓等待，不做无资金认可的反转。</div>';
    return;
  }
  const counts = {};
  [...(d.group_a || []), ...(d.group_b || [])].forEach(r => {
    [r.sector, ...((r.sector_detail || []).map(x => x.name))].filter(Boolean)
      .forEach(n => { counts[n] = (counts[n] || 0) + 1; });
  });
  box.innerHTML = `
    <div class="dip-flow-head">
      <h3>真资金流入板块 <span>${fs.length} 个</span></h3>
      <span class="dip-flow-src">${escHtml(d.flow_source || '实时资金')} · 净流入=主力资金(f62)</span>
    </div>
    <div class="dip-flow-grid">
      ${fs.map(s => `
        <div class="dip-flow-card">
          <div class="dfc-name">${escHtml(s.name)}</div>
          <div class="dfc-flow">+${fmt(s.flow, 2)}<small>亿</small></div>
          <div class="dfc-extra">${s.change_pct != null ? (s.change_pct > 0 ? '+' : '') + fmt(s.change_pct, 2) + '%' : ''}${counts[s.name] ? ' · 候选' + counts[s.name] + '只' : ''}</div>
        </div>`).join('')}
    </div>`;
}

// 拉取当前持仓代码集合(纯6位)，用于"加入持仓"按钮状态；失败时按钮仍可用
async function loadHoldingCodes() {
  try {
    const d = await fetch('/api/watchlist').then(x => x.json());
    state.holdingCodes = new Set((d.stocks || []).map(s => String(s.code || '').split('.')[0]));
  } catch (e) {
    state.holdingCodes = new Set();
  }
  renderDip();
}

const toPureCode = c => String(c || '').split('.')[0];

// 从低吸池把一只票加入持仓（去重 + 代码规范化在后端做）
async function addDipToHold(btn, code, name, sector, note, cost) {
  const pure = toPureCode(code);
  if (btn.disabled) return;
  const oldText = btn.textContent;
  btn.disabled = true; btn.textContent = '加入中…';
  try {
    const r = await fetch('/api/watchlist/add', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code, name, note: note || sector || '', cost: cost ?? null }),
    });
    const d = await r.json();
    if (d.ok || d.already) {
      btn.textContent = '✓ 已加入';
      btn.classList.add('dip-added');
      if (!state.holdingCodes) state.holdingCodes = new Set();
      state.holdingCodes.add(pure);
      // 持仓简报作废，下次切到"我的持仓"重新加载，纳入新加入的股票
      state.brief = null;
      state.watchlist = null;
    } else {
      btn.textContent = oldText; btn.disabled = false;
      alert(d.message || '加入失败');
    }
  } catch (e) {
    btn.textContent = oldText; btn.disabled = false;
    alert('加入失败：' + e.message);
  }
}

function renderDip() {
  const d = state.dipCache;
  if (!d) return;
  // 首次渲染异步拉取持仓代码；拿到后会再 renderDip 一次刷新按钮状态
  if (state.holdingCodes === null) { loadHoldingCodes(); return; }
  const list = state.dipGroup === 'a' ? (d.group_a || []) : (d.group_b || []);
  const cntA = d.count_a != null ? d.count_a : (d.group_a || []).length;
  const cntB = d.count_b != null ? d.count_b : (d.group_b || []).length;
  el('dip-meta').innerHTML = '数据日 ' + (d.date || '—') +
    ' · 技术候选 <b>' + (d.candidates_before_flow != null ? d.candidates_before_flow : '—') + '</b> 只 → 板块资金过滤后：超跌型 <b>' + cntA + '</b> / 缩量企稳型 <b>' + cntB + '</b> 只' +
    (d.from_cache ? ' · 缓存' : '') + '　<span class="dip-click-hint">↗ 点击股票查看K线与详情</span>';
  el('dip-note').innerHTML = '⚠️ ' + (d.note || '') +
    '<br>技术门槛：量比≤0.85、近3日转涨、换手≤3%' +
    (state.dipGroup === 'a' ? '、20日跌幅≤−8%' : '') + '；且所属板块真资金净流入>0（' + (d.flow_source || '同花顺实时') + '）。';
  renderDipFlow(d);
  if (!list.length) {
    el('dip-table').innerHTML = '<div class="ai-placeholder" style="padding:32px 20px;text-align:center;line-height:1.9">' +
      '<div style="font-size:28px;margin-bottom:8px">🛡️</div>' +
      '<b>当前无符合标的</b><br>' +
      '<span style="color:var(--text-3);font-size:12.5px">' +
      (state.dipGroup === 'a'
        ? '今日没有「超跌 + 所属板块真资金净流入」共振的个股。多数超跌股所在板块资金仍在流出——宁缺毋滥，继续等待。'
        : '今日没有「缩量企稳 + 板块真资金流入」的个股。') +
      '</span><br><span style="color:var(--text-3);font-size:12px">可切换到另一组，或点「刷新」更新</span></div>';
    return;
  }
  const sign = v => (v > 0 ? 'up' : v < 0 ? 'down' : '');
  el('dip-table').innerHTML = `
    <table class="dip-grid">
      <thead><tr>
        <th>股票</th><th>板块</th><th>板块真资金</th><th>现价</th>
        <th>20日涨跌</th><th>近3日</th><th>量比</th><th>换手率</th><th>成交额</th><th>评分</th><th>操作</th>
      </tr></thead>
      <tbody>${list.map(r => `
        <tr class="dip-row" data-code="${escHtml(r.code)}" data-name="${escHtml(r.name)}" data-sector="${escHtml(r.sector || '')}">
          <td class="dip-name"><b>${escHtml(r.name)}</b><span>${escHtml(r.code)}</span></td>
          <td class="dip-sec">${escHtml(r.sector || '—')}${(r.sector_detail || []).length
            ? '<small>' + r.sector_detail.slice(0, 3).map(x => escHtml(x.name) + ' +' + fmt(x.flow, 2) + '亿').join('<br>') + '</small>'
            : ''}</td>
          <td class="up">+${r.sector_flow}亿</td>
          <td>¥${r.close}</td>
          <td class="${sign(r.ret20)}">${r.ret20 > 0 ? '+' : ''}${r.ret20}%</td>
          <td class="${sign(r.ret3)}">${r.ret3 > 0 ? '+' : ''}${r.ret3}%</td>
          <td>${r.volr}</td>
          <td>${r.turnover}%</td>
          <td>${r.amount}亿</td>
          <td><span class="dip-score">${r.score}</span></td>
          <td><button class="dip-add-btn${state.holdingCodes.has(toPureCode(r.code)) ? ' dip-added' : ''}"
            type="button" data-code="${escHtml(r.code)}" data-name="${escHtml(r.name)}"
            data-sector="${escHtml(r.sector || '')}" data-cost="${escHtml(r.close)}"
            ${state.holdingCodes.has(toPureCode(r.code)) ? 'disabled' : ''}>${
            state.holdingCodes.has(toPureCode(r.code)) ? '✓ 已加入' : '＋ 加入持仓'}</button></td>
        </tr>`).join('')}</tbody>
    </table>`;
  el('dip-table').querySelectorAll('.dip-row').forEach(row => {
    row.addEventListener('click', () => openStock(row.dataset.code, row.dataset.name, row.dataset.sector));
  });
  // "加入持仓"按钮：阻止冒泡，避免触发整行的打开K线
  el('dip-table').querySelectorAll('.dip-add-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      addDipToHold(btn, btn.dataset.code, btn.dataset.name, btn.dataset.sector, undefined, btn.dataset.cost);
    });
  });
}

/* ---------- 方向一：我的持仓简报 ---------- */
async function loadBrief() {
  try {
    const r = await fetch('/api/brief');
    const b = await r.json();
    state.brief = b;
    renderBriefPortfolio(b);
    // 异动红点
    el('tab-alert-dot').classList.toggle('show', b.alert_count > 0);
  } catch (e) {
    el('brief-headline').textContent = '简报加载失败：' + e.message;
  }
}

function renderBriefPortfolio(b) {
  el('brief-trade-date').textContent = b.trade_date || '--';
  el('brief-headline').textContent = b.headline || '今日简报';
  el('brief-alert-count').textContent = b.alert_count ?? 0;
  el('brief-watch-count').textContent = b.watch_count ?? 0;
  el('brief-subline').textContent = `基于 ${b.trade_date || '最新交易日'} 收盘数据 · 生成于 ${b.generated_at}`;

  // 静默时段提示
  const q = el('quiet-notice');
  if (b.quiet_now) {
    q.textContent = '⏸ 当前处于静默时段（午间休市）——简报照常生成，但不推送打扰提醒。';
    q.classList.remove('hidden');
  } else {
    q.classList.add('hidden');
  }

  // 一句话摘要 chips
  el('brief-oneliners').innerHTML = (b.one_liners || [])
    .map(l => {
      const idx = l.indexOf('：');
      const name = idx > 0 ? l.slice(0, idx) : '';
      const rest = idx > 0 ? l.slice(idx + 1) : l;
      return `<span class="oneliner-chip"><b>${name}</b>${rest}</span>`;
    }).join('');

  const items = b.items || [];
  const box = el('brief-items');
  if (!items.length) {
    box.innerHTML = '';
    el('portfolio-empty').classList.remove('hidden');
    return;
  }
  el('portfolio-empty').classList.add('hidden');
  box.innerHTML = items.map(it => {
    const topLevel = it.changes[0]?.level || 'low';
    const badges = it.changes.slice(0, 4).map(c =>
      `<span class="ac-badge ${c.level}" title="${c.detail || ''}">${c.text}</span>`).join('');
    const chgCls = it.change_pct > 0 ? 'up' : it.change_pct < 0 ? 'down' : 'flat';
    // 逐条列出异动原因（带真实数值的归因），而非只显示第一条
    const reasons = (it.changes || []).map(c =>
      `<div class="ac-reason"><span class="ac-reason-tag">${c.text}</span>${c.detail || ''}</div>`).join('');
    const hasPnl = it.pnl_pct !== null && it.pnl_pct !== undefined && it.cost;
    const pnlCls = it.pnl_pct > 0 ? 'up' : it.pnl_pct < 0 ? 'down' : 'flat';
    const meta = [
      it.sector ? `<span>板块 <b>${it.sector} ${it.sector_change > 0 ? '+' : ''}${fmt(it.sector_change)}%</b>${it.sector_relation ? ` · ${it.sector_relation}` : ''}</span>` : '',
      `<span>技术评分 <b style="color:var(--accent)">${it.score ?? '-'}</b></span>`,
      `<span>近20日 <b class="${it.ret20 > 0 ? 'up' : it.ret20 < 0 ? 'down' : ''}">${it.ret20 > 0 ? '+' : ''}${fmt(it.ret20)}%</b></span>`,
      `<span>量比 <b>${fmt(it.vol_ratio, 1)}</b></span>`,
      hasPnl
        ? `<span>成本 <b>${fmt(it.cost)}</b> · 盈利 <b class="${pnlCls}">${it.pnl_pct > 0 ? '+' : ''}${fmt(it.pnl_pct)}%</b></span>` : '',
    ].filter(Boolean).join('');
    // 卡片头部醒目的持仓盈亏徽章：基于成本价与现价计算（涨红跌绿）
    const pnlBadge = hasPnl
      ? `<div class="ac-pnl ${pnlCls}">
           <span class="ac-pnl-label">持仓盈亏</span>
           <span class="ac-pnl-val">${it.pnl_pct > 0 ? '+' : ''}${fmt(it.pnl_pct)}%</span>
           <span class="ac-pnl-status">${it.pnl_pct > 0 ? '浮盈' : it.pnl_pct < 0 ? '浮亏' : '持平'} · 现价${fmt(it.close)} / 成本${fmt(it.cost)}</span>
         </div>` : '';
    // 五维评分条：与精选卡片同一套口径，简报里也能看出强在哪
    const dimMax = {趋势:30, 动量:25, 量能:20, 位置:15, 超跌反弹:10};
    const bars = it.dims && Object.keys(it.dims).length
      ? `<div class="dim-bars" style="margin-top:10px">${Object.entries(dimMax).map(([k,mx]) => {
          const v = it.dims[k] || 0, pct = Math.round(v / mx * 100);
          return `<span class="dim-bar" title="${k} ${v}/${mx}"><i style="width:${pct}%;background:${pct>=70?'#d9534f':pct>=40?'#e8a33d':'#c8cdd6'}"></i></span>`;
        }).join('')}</div>` : '';
    return `<div class="alert-card level-${topLevel}" data-code="${it.code}" data-name="${it.name}">
      <div class="ac-head">
        <div><span class="ac-name">${it.name}</span><span class="ac-code">${it.code}</span></div>
        <div class="ac-price"><div class="ac-close">${fmt(it.close)}</div>
          <div class="ac-chg ${chgCls}">${it.change_pct > 0 ? '+' : ''}${fmt(it.change_pct)}%</div></div>
      </div>
      ${pnlBadge}
      <div class="ac-badges">${badges}</div>
      <div class="ac-reasons">${reasons}</div>
      ${bars}
      <div class="ac-meta">${meta}</div>
      <button class="pa-toggle" data-code="${it.code}" type="button">🤖 ARTi 持仓去留诊断 <span class="pa-caret">▾</span></button>
      <div class="pa-panel hidden" id="pa-panel-${it.code}"></div>
    </div>`;
  }).join('');
  box.querySelectorAll('.alert-card').forEach(card => {
    card.addEventListener('click', (e) => {
      // 诊断按钮/面板内的点击不触发打开个股抽屉
      if (e.target.closest('.pa-toggle') || e.target.closest('.pa-panel')) return;
      openStock(card.dataset.code, card.dataset.name, '');
    });
  });
  box.querySelectorAll('.pa-toggle').forEach(btn => {
    btn.addEventListener('click', () => togglePositionAdvice(btn));
  });
}

/* ---------- ARTi 持仓去留诊断（按卡片展开） ---------- */
function togglePositionAdvice(btn) {
  const code = btn.dataset.code;
  const panel = el('pa-panel-' + code);
  if (!panel) return;
  const willOpen = panel.classList.contains('hidden');
  panel.classList.toggle('hidden', !willOpen);
  btn.classList.toggle('open', willOpen);
  if (willOpen && !panel.dataset.loaded) loadPositionAdvice(code, panel, btn);
}

async function loadPositionAdvice(code, panel, btn, force) {
  const holding = (state.brief?.items || []).find(i => i.code === code);
  if (!holding) { panel.innerHTML = '<div class="pa-loading">未找到该持仓数据</div>'; return; }
  panel.innerHTML = '<div class="pa-loading"><span class="pa-spinner"></span>ARTi 正在分析持仓移动与去留…</div>';
  btn.disabled = true;
  try {
    const url = '/api/position/advice' + (force ? '?force=true' : '');
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(holding),
    });
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    const sourceTag = d.source || '';
    const isMock = /mock|本地规则/i.test(sourceTag);
    const hasFull = !!d.full_content && d.full_content !== d.content;
    panel.dataset.brief = d.content || '';
    panel.dataset.full = d.full_content || d.content || '';
    panel.dataset.expanded = '0';
    panel.innerHTML =
      `<div class="pa-src">${isMock ? '⚙️' : '✦'} ${sourceTag}${d.cached ? ' · 缓存' : ''} · ${d.generated_at || ''}` +
      `<span class="pa-actions">` +
        (hasFull ? `<button class="pa-expand" type="button">完整 ▾</button>` : '') +
        `<button class="pa-refresh" type="button" title="重新分析">↻ 重算</button></span></div>` +
      `<div class="pa-body">${renderMarkdown(d.content || '暂无建议')}</div>`;
    panel.dataset.loaded = '1';
    panel.querySelector('.pa-refresh').addEventListener('click', (e) => {
      e.stopPropagation();
      loadPositionAdvice(code, panel, btn, true);
    });
    const expandBtn = panel.querySelector('.pa-expand');
    if (expandBtn) expandBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const expanded = panel.dataset.expanded === '1';
      const body = panel.querySelector('.pa-body');
      if (expanded) {
        body.innerHTML = renderMarkdown(panel.dataset.brief);
        expandBtn.textContent = '完整 ▾';
        panel.dataset.expanded = '0';
      } else {
        body.innerHTML = renderMarkdown(panel.dataset.full);
        expandBtn.textContent = '精简 ▴';
        panel.dataset.expanded = '1';
      }
    });
  } catch (e) {
    panel.innerHTML = `<div class="pa-loading">诊断失败：${e.message}</div>`;
  } finally {
    btn.disabled = false;
  }
}

/* ---------- 自选股管理 ---------- */
async function loadWatchlist() {
  try {
    const r = await fetch('/api/watchlist');
    state.watchlist = await r.json();
    renderWatchlistEditor();
  } catch (e) {}
}

function renderWatchlistEditor() {
  const wl = state.watchlist;
  if (!wl) return;
  const tbody = el('wl-tbody');
  tbody.innerHTML = (wl.stocks || []).map((s, i) => `
    <tr data-i="${i}">
      <td><input class="wl-code" value="${s.code || ''}" placeholder="600519"></td>
      <td><input class="wl-name" value="${s.name || ''}" placeholder="名称"></td>
      <td><input class="wl-cost" value="${s.cost ?? ''}" placeholder="留空"></td>
      <td><input class="wl-note" value="${s.note || ''}" placeholder="备注"></td>
      <td><button class="wl-del" title="删除">×</button></td>
    </tr>`).join('');
  // 阈值配置
  const a = wl.alert || {};
  const cfg = [
    ['change_pct', '涨跌幅阈值(%)', 'number', a.change_pct ?? 3],
    ['vol_ratio', '量比阈值', 'number', a.vol_ratio ?? 1.8],
    ['ma_break', '均线突破/跌破', 'checkbox', a.ma_break ?? true],
    ['macd_cross', 'MACD金叉死叉', 'checkbox', a.macd_cross ?? true],
    ['news', '检查本地新闻', 'checkbox', a.news ?? true],
    ['max_items', '最多展示条数', 'number', a.max_items ?? 5],
  ];
  el('wl-alert-config').innerHTML = '<h4>异动提醒阈值</h4><div class="wl-config-grid">' +
    cfg.map(([k, label, type, v]) => `
      <label class="wl-config-item">
        <input type="${type}" data-key="${k}" ${type === 'checkbox' ? (v ? 'checked' : '') : `value="${v}"`}>
        <span>${label}</span>
      </label>`).join('') + '</div>';
}

function collectWatchlist() {
  const stocks = [];
  el('wl-tbody').querySelectorAll('tr').forEach(tr => {
    const code = tr.querySelector('.wl-code').value.trim();
    if (!code) return;
    const costVal = tr.querySelector('.wl-cost').value.trim();
    stocks.push({
      code,
      name: tr.querySelector('.wl-name').value.trim() || code,
      cost: costVal ? Number(costVal) : null,
      note: tr.querySelector('.wl-note').value.trim(),
    });
  });
  const alert = {};
  el('wl-alert-config').querySelectorAll('input[data-key]').forEach(inp => {
    alert[inp.dataset.key] = inp.type === 'checkbox' ? inp.checked : Number(inp.value);
  });
  return { stocks, alert };
}

async function saveWatchlist() {
  const payload = collectWatchlist();
  try {
    const r = await fetch('/api/watchlist', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    el('wl-tip').textContent = d.message || '已保存';
    state.watchlist = payload;
    await loadBrief();
  } catch (e) {
    el('wl-tip').textContent = '保存失败：' + e.message;
  }
}

/* ---------- 视图切换 ---------- */
function switchTab(tab) {
  state.activeTab = tab;
  document.querySelectorAll('.main-tab').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  el('radar-view').classList.toggle('hidden', tab !== 'radar');
  el('portfolio-view').classList.toggle('hidden', tab !== 'portfolio');
  el('artibrief-view').classList.toggle('hidden', tab !== 'artibrief');
  el('dip-view').classList.toggle('hidden', tab !== 'dip');
  if (tab !== 'portfolio') el('watchlist-panel').classList.add('hidden');
  if (tab === 'portfolio' && !state.brief) loadBrief();
  if (tab === 'artibrief' && !state.abCache[state.abKind]) loadArtiBrief();
  if (tab === 'dip' && !state.dipCache) loadDip();
}

/* ---------- 本地新闻 ---------- */
async function loadNews(code) {
  const box = el('news-content');
  box.innerHTML = '<div class="ai-placeholder">加载中...</div>';
  try {
    const r = await fetch('/api/news/' + code + '?days=30&limit=15');
    const d = await r.json();
    const news = d.news || [];
    if (!news.length) {
      box.innerHTML = '<div class="news-empty">本地资讯库暂无该股近期新闻</div>';
      return;
    }
    box.innerHTML = news.map(n => `
      <div class="news-item">
        <div class="news-title">${n.title}</div>
        <div class="news-meta">
          <span>${n.published_at}</span>
          <span>${n.source || ''}</span>
          <span class="news-sent-${n.sentiment || ''}">${n.sentiment || ''} ${n.sentiment_score ? '(' + n.sentiment_score + ')' : ''}</span>
          ${n.important ? '<span style="color:var(--up)">重要</span>' : ''}
        </div>
      </div>`).join('');
  } catch (e) {
    box.innerHTML = '<div class="news-empty">加载失败：' + e.message + '</div>';
  }
}

/* ---------- 事件绑定 ---------- */
function bindEvents() {
  el('brief-head').addEventListener('click', toggleBrief);

  // 主导航页签
  document.querySelectorAll('.main-tab').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });
  // ARTi 晨报：早晚报切换 + 强制刷新
  document.querySelectorAll('.ab-kind-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.ab-kind-btn').forEach(b => b.classList.toggle('active', b === btn));
      state.abKind = btn.dataset.kind;
      loadArtiBrief();
    });
  });
  if (el('ab-refresh')) el('ab-refresh').addEventListener('click', () => loadArtiBrief(true));
  // 低吸观察池：分组切换 + 强制刷新
  document.querySelectorAll('#dip-kind-switch .ab-kind-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('#dip-kind-switch .ab-kind-btn').forEach(b => b.classList.toggle('active', b === btn));
      state.dipGroup = btn.dataset.group;
      renderDip();
    });
  });
  if (el('dip-refresh')) el('dip-refresh').addEventListener('click', () => loadDip(true));
  el('watchlist-edit-btn').addEventListener('click', () => {
    const panel = el('watchlist-panel');
    const show = panel.classList.contains('hidden');
    panel.classList.toggle('hidden', !show);
    if (show && !state.watchlist) loadWatchlist();
  });
  el('wl-close').addEventListener('click', () => el('watchlist-panel').classList.add('hidden'));
  el('wl-add').addEventListener('click', () => {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td><input class="wl-code" placeholder="600519"></td>
      <td><input class="wl-name" placeholder="名称"></td>
      <td><input class="wl-cost" placeholder="留空"></td>
      <td><input class="wl-note" placeholder="备注"></td>
      <td><button class="wl-del" title="删除">×</button></td>`;
    el('wl-tbody').appendChild(tr);
    tr.querySelector('.wl-del').addEventListener('click', () => tr.remove());
    tr.querySelector('.wl-code').focus();
  });
  el('wl-save').addEventListener('click', saveWatchlist);
  // 删除按钮（已渲染的行）
  document.addEventListener('click', (e) => {
    if (e.target.classList.contains('wl-del')) e.target.closest('tr').remove();
  });

  document.querySelectorAll('#sector-view-toggle .seg-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('#sector-view-toggle .seg-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      state.sectorView = btn.dataset.view;
      el('sector-treemap').classList.toggle('hidden', state.sectorView !== 'treemap');
      el('sector-list').classList.toggle('hidden', state.sectorView !== 'list');
      renderSectors();
    });
  });

  document.querySelectorAll('#indicator-tabs .ind-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('#indicator-tabs .ind-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      currentIndicator = btn.dataset.ind;
      if (currentKline) drawKline(currentKline);
    });
  });

  document.querySelectorAll('#ai-tabs .ai-tab').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('#ai-tabs .ai-tab').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      loadAI(btn.dataset.kind);
    });
  });

  el('drawer-close').addEventListener('click', closeDrawer);
  el('drawer-overlay').addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });

  el('refresh-btn').addEventListener('click', async () => {
    const btn = el('refresh-btn');
    btn.textContent = '↻ 刷新中...';
    btn.disabled = true;
    try {
      await fetch('/api/refresh', { method: 'POST' });
      await loadOverview();
    } finally {
      btn.textContent = '↻ 刷新';
      btn.disabled = false;
    }
  });

  window.addEventListener('resize', () => {
    if (state.sectorChart) state.sectorChart.resize();
    if (state.klineChart) state.klineChart.resize();
  });
}

/* ---------- 启动 ---------- */
document.addEventListener('DOMContentLoaded', () => {
  bindEvents();
  loadStatus();
  loadOverview();
  loadBrief();
});
