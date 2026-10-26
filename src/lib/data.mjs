/**
 * 构建期数据入口。只读 data/，不碰数据库。
 * 校验放在这里：数据对不上就让构建失败，不要静默出一个错的站。
 */
import fs from 'node:fs';
import path from 'node:path';

const ROOT = process.cwd();
const DATA = path.resolve(ROOT, 'data');

/** 后验收益窗口（交易日）。与 ingest.py 的 POST_WINDOWS 必须一致。 */
export const WINDOWS = [20, 60, 120, 240, 360, 480, 600];

function readJson(p) {
  return JSON.parse(fs.readFileSync(path.join(DATA, p), 'utf8'));
}

function readCsv(p, numeric) {
  const text = fs.readFileSync(path.join(DATA, p), 'utf8').trim();
  if (!text) return [];
  const lines = text.split('\n');
  const head = lines[0].split(',');
  const rows = new Array(lines.length - 1);
  for (let i = 1; i < lines.length; i++) {
    const cells = lines[i].split(',');
    const row = {};
    for (let k = 0; k < head.length; k++) {
      const v = cells[k];
      row[head[k]] = numeric.includes(head[k]) ? (v === '' ? null : Number(v)) : v;
    }
    rows[i - 1] = row;
  }
  return rows;
}

function readJsonl(p) {
  const text = fs.readFileSync(path.join(DATA, p), 'utf8').trim();
  if (!text) return [];
  return text.split('\n').map((l) => JSON.parse(l));
}

const INDEX_NUMERIC = ['o', 'h', 'l', 'c', 'flow', 'n_in', 'n_out', 'n_etf', 'scale'];
const ETF_NUMERIC = ['close', 'share', 'flow', 'd_share'];

let cache = null;

export function load() {
  if (cache) return cache;

  const meta = readJson('meta.json');
  const indices = readJson('indices.json').indices;
  const etfs = readJson('etfs.json').etfs;
  const events = readJsonl('events.jsonl');
  const latest = meta.latest_trade_date;

  const indexSeries = new Map();
  for (const it of indices) {
    const rows = readCsv(path.join('index', `${it.slug}.csv`), INDEX_NUMERIC);
    indexSeries.set(it.slug, rows);
  }

  const etfSeries = new Map();
  for (const e of etfs) {
    const p = path.join('etf', `${e.code}.csv`);
    etfSeries.set(e.code, fs.existsSync(path.join(DATA, p)) ? readCsv(p, ETF_NUMERIC) : []);
  }

  // ---- 校验：宁可构建失败，也不要出一个口径错的站
  const problems = [];
  const staleDeclared = meta.stale?.by_index ?? {};
  for (const it of indices) {
    const rows = indexSeries.get(it.slug);
    if (!rows.length) {
      problems.push(`${it.slug}: data/index/${it.slug}.csv 为空`);
      continue;
    }
    const last = rows[rows.length - 1].date;
    if (last !== latest && !staleDeclared[it.slug]) {
      problems.push(`${it.slug}: K 线止于 ${last}，最新交易日是 ${latest}，且未在 meta.stale 里声明`);
    }
    const m = meta.indices?.[it.slug];
    if (!m) problems.push(`${it.slug}: meta.json 里没有这个指数的解析结果`);
    else if (m.rows !== rows.length) {
      problems.push(`${it.slug}: meta 记 ${m.rows} 行，CSV 实际 ${rows.length} 行，ingest 与数据不同步`);
    }
  }
  const slugs = new Set(indices.map((i) => i.slug));
  for (const e of events) {
    if (!slugs.has(e.slug)) problems.push(`events.jsonl: 出现未知指数 ${e.slug}`);
  }
  if (problems.length) {
    throw new Error(`数据校验失败（先跑 python3 ingest.py）：\n  - ${problems.join('\n  - ')}`);
  }

  // ---- 全市场合计：57 只一起算，深市 10 只 2026-10-21 起才有份额
  const byDate = new Map();
  const shOnly = new Map();
  for (const e of etfs) {
    for (const r of etfSeries.get(e.code) ?? []) {
      let a = byDate.get(r.date);
      if (!a) byDate.set(r.date, (a = { date: r.date, flow: 0, nIn: 0, nOut: 0, n: 0 }));
      a.flow += r.flow ?? 0;
      a.n += 1;
      if ((r.flow ?? 0) > 0) a.nIn += 1;
      else if ((r.flow ?? 0) < 0) a.nOut += 1;

      if (e.market === 'SH') {
        let b = shOnly.get(r.date);
        if (!b) shOnly.set(r.date, (b = { date: r.date, flow: 0, n: 0 }));
        b.flow += r.flow ?? 0;
        b.n += 1;
      }
    }
  }
  const daily = [...byDate.values()].sort((a, b) => (a.date < b.date ? -1 : 1));
  for (const d of daily) d.flow = round(d.flow, 2);
  const shDaily = [...shOnly.values()].sort((a, b) => (a.date < b.date ? -1 : 1));

  const eventsBySlug = new Map();
  for (const ev of events) {
    if (!eventsBySlug.has(ev.slug)) eventsBySlug.set(ev.slug, []);
    eventsBySlug.get(ev.slug).push(ev);
  }

  cache = {
    meta,
    indices,
    etfs,
    events,
    eventsBySlug,
    indexSeries,
    etfSeries,
    daily,
    shDaily,
    latest,
    byCode: new Map(etfs.map((e) => [e.code, e])),
    bySlug: new Map(indices.map((i) => [i.slug, i])),
  };
  return cache;
}

export function round(x, n = 2) {
  const f = 10 ** n;
  return Math.round(x * f) / f;
}

export function median(arr) {
  if (!arr.length) return null;
  const s = arr.slice().sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

/** 单指数的当期口径：当日／近 5 日／近 20 日净申赎 */
export function indexStats(slug) {
  const { indexSeries, eventsBySlug, meta } = load();
  const rows = indexSeries.get(slug) ?? [];
  const flows = rows.filter((r) => r.flow !== null);
  const sum = (arr) => (arr.length ? round(arr.reduce((s, r) => s + r.flow, 0), 2) : null);
  const last = flows[flows.length - 1] ?? null;
  const evs = (eventsBySlug.get(slug) ?? []).slice().sort((a, b) => (a.date < b.date ? 1 : -1));
  return {
    rows,
    flowDays: flows.length,
    lastDate: last?.date ?? null,
    today: last ? round(last.flow, 2) : null,
    d5: sum(flows.slice(-5)),
    d20: sum(flows.slice(-20)),
    scale: last?.scale ?? null,
    nEtf: last?.n_etf ?? null,
    events: evs,
    lastEvent: evs[0] ?? null,
    shortHistory: (meta.indices?.[slug]?.flow_days ?? 0) < meta.params.min_hist_obs,
  };
}

/** 最新交易日的全市场合计与近 5／20 日 */
export function marketToday() {
  const { daily, latest } = load();
  const upto = daily.filter((d) => d.date <= latest);
  const last = upto[upto.length - 1] ?? null;
  const sum = (arr) => (arr.length ? round(arr.reduce((s, d) => s + d.flow, 0), 2) : null);
  // 沪市 47 只在册时 n=47；深市 10 只进合计的第一天 n 会跳到 57
  const szFirst = daily.find((d) => d.n > 47)?.date ?? null;
  return {
    date: last?.date ?? latest,
    flow: last?.flow ?? null,
    nIn: last?.nIn ?? 0,
    nOut: last?.nOut ?? 0,
    n: last?.n ?? 0,
    d5: sum(upto.slice(-5)),
    d20: sum(upto.slice(-20)),
    history: upto,
    szFirst,
  };
}

/** 当月触发的事件 */
export function eventsOn(date) {
  return load().events.filter((e) => e.date === date);
}

/** 连续同向天数（截至最新交易日），用于首页的一句话结论 */
export function streak() {
  const { daily, latest } = load();
  const upto = daily.filter((d) => d.date <= latest);
  if (!upto.length) return { days: 0, sign: 0 };
  const sign = Math.sign(upto[upto.length - 1].flow);
  let n = 0;
  for (let i = upto.length - 1; i >= 0 && Math.sign(upto[i].flow) === sign; i--) n++;
  return { days: n, sign };
}

/**
 * 一组事件的窗口统计：命中率、中位收益、相对该指数「任意一天买入」基准的超额。
 * 卖点的 post 已经取过负号（看跌为正），基准也要按方向取号，否则超额会算反。
 */
function windowStats(list, meta) {
  return WINDOWS.map((w) => {
    const vals = [];
    const excess = [];
    for (const e of list) {
      const v = e.post?.[String(w)];
      if (v === null || v === undefined) continue;
      vals.push(v);
      const b = meta.indices?.[e.slug]?.bench?.[String(w)];
      if (b !== null && b !== undefined) excess.push(v - e.side * b);
    }
    const hit = vals.filter((v) => v > 0).length;
    return {
      w,
      n: vals.length,
      hit,
      rate: vals.length ? hit / vals.length : null,
      med: vals.length ? round(median(vals), 2) : null,
      excess: excess.length ? round(median(excess), 2) : null,
    };
  });
}

/** 信号有效性：全样本、买点、卖点，各自按窗口拆 */
export function signalStats() {
  const { events, meta } = load();
  const buys = events.filter((e) => e.side > 0);
  const sells = events.filter((e) => e.side < 0);
  return {
    total: events.length,
    buys: buys.length,
    sells: sells.length,
    first: events[0]?.date ?? null,
    all: windowStats(events, meta),
    buy: windowStats(buys, meta),
    sell: windowStats(sells, meta),
  };
}

/** 按指数看命中率（样本太少的不上，免得一个事件就 100%） */
export function byIndexStats(minN = 5) {
  const { events, meta, bySlug } = load();
  const groups = new Map();
  for (const e of events) {
    if (!groups.has(e.slug)) groups.set(e.slug, []);
    groups.get(e.slug).push(e);
  }
  const rows = [];
  for (const [slug, list] of groups) {
    if (list.length < minN) continue;
    const w = windowStats(list, meta).find((x) => x.w === 360);
    rows.push({
      slug,
      name: bySlug.get(slug)?.name ?? slug,
      cat: bySlug.get(slug)?.cat ?? '',
      n: list.length,
      buys: list.filter((e) => e.side > 0).length,
      rate: w.rate,
      med: w.med,
      excess: w.excess,
      matured: w.n,
    });
  }
  return rows.sort((a, b) => (b.rate ?? 0) - (a.rate ?? 0));
}

/** 按历史强度分三档：同一指数内部 |当日金额| 排名前 1/3、中 1/3、后 1/3。
 *  买卖点分开算——两个方向的信号基数差一倍，混在一起分层会被构成带偏。
 *  n_events < 3 的指数分不出三档，直接排除。 */
export function strengthStats() {
  const { events, meta } = load();
  const split = (list) => {
    const bands = { strong: [], mid: [], weak: [] };
    for (const e of list) {
      if (!e.rank || !e.n_events || e.n_events < 3) continue;
      const p = e.rank / e.n_events;
      bands[p <= 1 / 3 ? 'strong' : p <= 2 / 3 ? 'mid' : 'weak'].push(e);
    }
    return ['strong', 'mid', 'weak'].map((k) => ({
      key: k,
      n: bands[k].length,
      windows: windowStats(bands[k], meta),
    }));
  };
  return {
    buy: split(events.filter((e) => e.side > 0)),
    sell: split(events.filter((e) => e.side < 0)),
  };
}

/** 详情页图表的原始序列：K 线、申赎、事件、公告锚点。
 *  均线与 20 日累计在浏览器里算——派生序列不进 payload，能省三成体积。 */
export function chartPayload(slug) {
  const { indexSeries, meta, eventsBySlug } = load();
  const rows = indexSeries.get(slug) ?? [];
  const dates = rows.map((r) => r.date);
  const kline = rows.map((r) => [round(r.o, 2), round(r.c, 2), round(r.l, 2), round(r.h, 2)]);
  const flows = rows.map((r) => (r.flow === null ? null : round(r.flow, 2)));

  return {
    dates,
    kline,
    flows,
    ann: meta.announcements.map((a) => a.date),
    events: (eventsBySlug.get(slug) ?? []).map((e) => ({
      date: e.date,
      side: e.side,
      amount: e.amount,
      total: e.total,
      days: e.days,
      reson: e.reson,
      n: e.n,
      rank: e.rank,
      nEvents: e.n_events,
      post20: e.post20 ?? null,
    })),
  };
}
