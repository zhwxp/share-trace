/**
 * 构建期数据入口。只读 data/，不碰数据库。
 * 校验放在这里：数据对不上就让构建失败，不要静默出一个错的站。
 */
import fs from 'node:fs';
import path from 'node:path';

const ROOT = process.cwd();
const DATA = path.resolve(ROOT, 'data');

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

  // ---- 沪市合计（异动／历史页用）
  const shEtfs = etfs.filter((e) => e.market === 'SH');
  const byDate = new Map();
  for (const e of shEtfs) {
    for (const r of etfSeries.get(e.code) ?? []) {
      let a = byDate.get(r.date);
      if (!a) byDate.set(r.date, (a = { date: r.date, flow: 0, nIn: 0, nOut: 0, n: 0 }));
      a.flow += r.flow ?? 0;
      a.n += 1;
      if ((r.flow ?? 0) > 0) a.nIn += 1;
      else if ((r.flow ?? 0) < 0) a.nOut += 1;
    }
  }
  const shDaily = [...byDate.values()].sort((a, b) => (a.date < b.date ? -1 : 1));
  for (const d of shDaily) d.flow = round(d.flow, 2);

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

/** 最新交易日的沪市合计与近 5／20 日 */
export function marketToday() {
  const { shDaily, latest } = load();
  const upto = shDaily.filter((d) => d.date <= latest);
  const last = upto[upto.length - 1] ?? null;
  const sum = (arr) => (arr.length ? round(arr.reduce((s, d) => s + d.flow, 0), 2) : null);
  return {
    date: last?.date ?? latest,
    flow: last?.flow ?? null,
    nIn: last?.nIn ?? 0,
    nOut: last?.nOut ?? 0,
    n: last?.n ?? 0,
    d5: sum(upto.slice(-5)),
    d20: sum(upto.slice(-20)),
    history: upto,
  };
}

/** 最新交易日的 ETF 异动，按 |flow| 排序 */
export function movers(limit = 5) {
  const { etfs, etfSeries, latest } = load();
  const out = [];
  for (const e of etfs) {
    const rows = etfSeries.get(e.code) ?? [];
    const last = rows[rows.length - 1];
    if (!last || last.date !== latest) continue;
    out.push({ etf: e, ...last });
  }
  out.sort((a, b) => Math.abs(b.flow ?? 0) - Math.abs(a.flow ?? 0));
  return out.slice(0, limit);
}

/** 当月触发的事件 */
export function eventsOn(date) {
  return load().events.filter((e) => e.date === date);
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
    })),
  };
}
