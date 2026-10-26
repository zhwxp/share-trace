#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把站点需要的数据从 market.sqlite 导出到 data/，之后构建只读 data/。

数据源：~/.hermes/data/astock/market.sqlite（bars / fund_shares / calendar）
产物：
    data/indices.json      43 个指数 → ETF 映射（人工维护）
    data/etfs.json         57 只 ETF 元数据（人工维护）
    data/meta.json         信号参数、口径、覆盖缺口、解析后的 K 线来源（生成）
    data/index/<slug>.csv  date,o,h,l,c,flow,n_in,n_out,n_etf,scale
    data/etf/<code>.csv    date,close,share,flow,d_share
    data/events.jsonl      信号事件快照，一行一个，按日期升序

分工：所有换算（Δ份额×收盘价、沪市合计、K 线与流量对齐、后验收益）都在这里做完，
构建端只读、只排版。口径只写一处，换机器或上 CI 也能复现。

用法：
    python3 ingest.py            # 写 data/
    python3 ingest.py --stats    # 只打印信号统计，不写文件
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = Path.home() / ".hermes/data/astock/market.sqlite"
DATA = ROOT / "data"
SCHEMA_VERSION = 1

# ---- 信号参数（写进 meta.json，站点从数据里读口径，别只留在代码里） ----
FLOOR_YI = 2.0        # 单日净申赎绝对下限（亿元）
RATIO = 0.08          # 相对该指数 ETF 合计规模的占比
CAP_YI = 100.0        # 占比门槛的绝对上限（亿元）
MIN_OBS = 60          # 至少积累这么多交易日才允许出信号
MERGE_DAYS = 5        # 同向连续触发合并窗口（交易日）
MIN_HIST_OBS = 60     # ETF 至少有这么多天份额历史才进历史序列

# 份额折算日（2 份合 1 份会造出假申赎），这些日期的 Δ份额 置零
SPLIT_DATES = {
    "510310": {"2024-09-20"},
    "512100": {"2022-09-02"},
    "560010": {"2022-09-13"},
}

# 国家队公告时点（只做时间锚，图上竖虚线）
ANNOUNCEMENTS = [
    ("2015-07-08", "汇金：已买入 ETF"),
    ("2023-10-23", "汇金：买入 ETF 并将继续增持"),
    ("2024-02-06", "汇金：扩大 ETF 增持范围"),
    ("2025-04-07", "汇金：再次增持 ETF"),
    ("2026-07-20", "国新/诚通：增持央企与科技 ETF"),
]

CATS = ["A股宽基", "A股行业主题", "跨境", "商品"]


# ---------------------------------------------------------------- 数据层

def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_config():
    idx = json.loads((DATA / "indices.json").read_text())["indices"]
    etfs = json.loads((DATA / "etfs.json").read_text())["etfs"]
    by_code = {e["code"]: e for e in etfs}
    return idx, etfs, by_code


def load_etf_series(conn):
    """每只 ETF 的日度序列：date -> {close, share, d_share, flow}

    不用 v_etf_daily 视图：它的 join 只剥掉了 sh. 前缀，深市（sz.）ETF 会全部
    拿不到份额。这里直接用 bars + fund_shares 自己拼，相邻性靠交易日历判断。
    """
    cal = [r[0] for r in conn.execute("SELECT date FROM calendar WHERE is_open=1 ORDER BY date")]
    pos = {d: i for i, d in enumerate(cal)}

    shares = defaultdict(dict)
    for r in conn.execute("SELECT code, date, share_yi FROM fund_shares"):
        shares[r["code"]][r["date"]] = r["share_yi"]

    closes = defaultdict(dict)
    for r in conn.execute(
        "SELECT code, date, close FROM bars WHERE freq='d' AND adjust='none'"
    ):
        closes[r["code"].split(".")[-1]][r["date"]] = r["close"]

    series = defaultdict(list)
    for code in set(closes) & set(shares):
        dates = sorted(set(closes[code]) & set(shares[code]))
        prev_date = prev_share = None
        for d in dates:
            sh = shares[code][d]
            cl = closes[code][d]
            dsh = flow = 0.0
            if prev_date is not None and pos.get(d, -99) - pos.get(prev_date, -99) == 1:
                dsh = sh - prev_share
                flow = dsh * cl
            if d in SPLIT_DATES.get(code, ()):
                dsh = flow = 0.0
            series[code].append(
                {"date": d, "close": cl, "share": sh, "d_share": dsh, "flow": flow}
            )
            prev_date, prev_share = d, sh
    return series


def load_bars(conn, code):
    rows = conn.execute(
        "SELECT date, open, high, low, close FROM bars "
        "WHERE code=? AND freq='d' AND adjust='none' AND open IS NOT NULL "
        "ORDER BY date",
        (code,),
    ).fetchall()
    out = []
    for r in rows:
        if None in (r["open"], r["high"], r["low"], r["close"]):
            continue
        out.append(
            {"date": r["date"], "o": r["open"], "h": r["high"], "l": r["low"], "c": r["close"]}
        )
    return out


def aggregate(codes, series, min_hist=0):
    """把多只 ETF 的日度申赎加总成指数口径。

    min_hist>0 时只统计份额历史足够长的 ETF（深市 10 只只有最近几天，
    混进历史序列会让 5 日/20 日累计在尾部跳变）。
    """
    use = [c for c in codes if len(series.get(c, [])) >= max(min_hist, 1)]
    agg = {}
    for c in use:
        for row in series[c]:
            a = agg.setdefault(
                row["date"],
                {"flow": 0.0, "n": 0, "nin": 0, "nout": 0, "per": {}, "scale": 0.0},
            )
            a["flow"] += row["flow"]
            a["n"] += 1
            a["per"][c] = row["flow"]
            if row["close"] and row["share"]:
                a["scale"] += row["close"] * row["share"]
            if row["flow"] > 0:
                a["nin"] += 1
            elif row["flow"] < 0:
                a["nout"] += 1
    return use, agg


def detect_events(dates, agg):
    """单指数信号：单日净申赎 ≥ max(2 亿, 8% × 该指数 ETF 合计规模)，且封顶 100 亿。

    多 ETF 指数还要求同向只数 ≥ max(2, 60%)。5 个交易日内同向连续触发合并。
    """
    events = []
    for i, d in enumerate(dates):
        a = agg[d]
        if i < MIN_OBS:
            continue
        thr = min(max(FLOOR_YI, RATIO * a["scale"]), CAP_YI)
        f = a["flow"]
        if abs(f) < thr:
            continue
        side = 1 if f > 0 else -1
        if a["n"] >= 2:
            same = a["nin"] if side > 0 else a["nout"]
            if same < max(2, math.ceil(0.6 * a["n"])):
                continue
        top = sorted(a["per"].items(), key=lambda kv: -abs(kv[1]))[:3]
        item = {
            "date": d,
            "side": side,
            "amount": round(f, 2),
            "total": f,
            "days": 1,
            "reson": a["nin"] if side > 0 else a["nout"],
            "n": a["n"],
            "thr": round(thr, 1),
            "scale": round(a["scale"], 1),
            "top": [[c, round(v, 2)] for c, v in top],
            "idx": i,
            "span": [d, d],
        }
        if events and events[-1]["side"] == side and i - events[-1]["idx"] <= MERGE_DAYS:
            e = events[-1]
            e["days"] += 1
            e["total"] += f
            e["idx"] = i
            e["span"][1] = d
            if abs(f) > abs(e["amount"]):
                e.update(
                    date=d, amount=round(f, 2), top=item["top"], reson=item["reson"],
                    n=item["n"], thr=item["thr"], scale=item["scale"],
                )
        else:
            events.append(item)
    for e in events:
        e["total"] = round(e["total"], 2)
    return events


def post_return(kdates, kclose, ev_date, side, horizon=20):
    i = bisect_left(kdates, ev_date)
    if i >= len(kdates) or i + horizon >= len(kdates):
        return None
    r = (kclose[i + horizon] / kclose[i] - 1) * 100
    return round(r if side > 0 else -r, 2)  # 买点看涨、卖点看跌，正数=信号有效


# ---------------------------------------------------------------- 输出

def write_csv(path: Path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(",".join(header) + "\n")
        for r in rows:
            f.write(",".join("" if v is None else str(v) for v in r) + "\n")


def num(x, n=2):
    if x is None:
        return None
    return round(x, n)


def run(stats_only=False):
    indices, etfs, by_code = load_config()
    conn = connect()
    series = load_etf_series(conn)

    latest = max(r["date"] for rows in series.values() for r in rows[-1:])
    all_dates = sorted({r["date"] for rows in series.values() for r in rows})

    # ---- 每只 ETF 一条序列
    etf_rows = 0
    if not stats_only:
        for e in etfs:
            rows = series.get(e["code"], [])
            if not rows:
                continue
            write_csv(
                DATA / "etf" / f"{e['code']}.csv",
                ["date", "close", "share", "flow", "d_share"],
                [
                    (r["date"], num(r["close"], 4), num(r["share"], 4),
                     num(r["flow"], 2), num(r["d_share"], 4))
                    for r in rows
                ],
            )
            etf_rows += len(rows)

    # ---- 逐指数
    meta_idx = {}
    events_all = []
    summary = []
    for it in indices:
        codes = it["etfs"]
        use_hist, agg = aggregate(codes, series, MIN_HIST_OBS)

        if it["code"]:
            kcode = it["code"]
        else:
            p = it["proxy"]
            kcode = f"sh.{p}" if p[0] == "5" else f"sz.{p}"
        bars = load_bars(conn, kcode)
        ksource = "index"
        if len(bars) < 30:
            for c in codes:
                kc = f"sh.{c}" if c[0] == "5" else f"sz.{c}"
                bars = load_bars(conn, kc)
                if len(bars) >= 30:
                    kcode, ksource = kc, "etf"
                    break
        kdates = [b["date"] for b in bars]
        kclose = [b["c"] for b in bars]
        bar_map = {b["date"]: b for b in bars}

        dts = sorted(agg)
        if not dts:
            use_hist, agg = aggregate(codes, series, 0)
            dts = sorted(agg)

        events = detect_events(dts, agg) if len(dts) >= MIN_OBS else []
        for e in events:
            e["post20"] = post_return(kdates, kclose, e["date"], e["side"])

        if stats_only:
            summary.append((it["name"], len(dts), len(events), events))
            continue

        # ---- K 线与流量按图表起点对齐（与旧 build.py 同口径）
        if dts and len(dts) >= MIN_HIST_OBS:
            start = dts[0]
        else:
            start = kdates[0] if kdates else (dts[0] if dts else None)
        chart_dates = [d for d in kdates if start and d >= start]
        if not chart_dates and kdates:
            chart_dates = kdates

        rows = []
        for d in chart_dates:
            b = bar_map[d]
            a = agg.get(d)
            rows.append(
                (
                    d, num(b["o"], 4), num(b["h"], 4), num(b["l"], 4), num(b["c"], 4),
                    num(a["flow"], 2) if a else None,
                    a["nin"] if a else None,
                    a["nout"] if a else None,
                    a["n"] if a else None,
                    num(a["scale"], 2) if a else None,
                )
            )
        write_csv(
            DATA / "index" / f"{it['slug']}.csv",
            ["date", "o", "h", "l", "c", "flow", "n_in", "n_out", "n_etf", "scale"],
            rows,
        )

        for e in events:
            events_all.append({"slug": it["slug"], **e})

        meta_idx[it["slug"]] = {
            "kline_code": kcode,
            "kline_source": ksource,
            "first_date": chart_dates[0] if chart_dates else None,
            "last_date": chart_dates[-1] if chart_dates else None,
            "rows": len(rows),
            "flow_days": len(dts),
            "events": len(events),
            "etfs_used": use_hist,
            "etfs_short": [c for c in codes if c not in use_hist],
        }

    if stats_only:
        total = 0
        for name, nd, ne, evs in summary:
            total += ne
            tail = " ".join(
                f"{e['date']}{'B+' if e['side'] > 0 else 'S-'}{abs(e['amount']):.0f}"
                for e in evs[-6:]
            )
            print(f"{name:<16} bars={nd:<5} events={ne:<3} {tail}")
        print(f"总事件数 {total}")
        return

    # ---- events.jsonl：按日期升序，新事件追加在尾部
    events_all.sort(key=lambda e: (e["date"], e["slug"]))
    DATA.mkdir(exist_ok=True)
    with (DATA / "events.jsonl").open("w", encoding="utf-8") as f:
        for e in events_all:
            f.write(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n")

    # ---- meta.json
    cov = {s: m["etfs_short"] for s, m in meta_idx.items() if m["etfs_short"]}
    stale = {
        s: {
            "last_date": m["last_date"],
            "lag_days": sum(1 for d in all_dates if m["last_date"] and d > m["last_date"]),
        }
        for s, m in meta_idx.items()
        if m["last_date"] and m["last_date"] != latest
    }
    meta = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "latest_trade_date": latest,
        "first_trade_date": all_dates[0] if all_dates else None,
        "params": {
            "floor_yi": FLOOR_YI,
            "ratio": RATIO,
            "cap_yi": CAP_YI,
            "min_obs": MIN_OBS,
            "merge_days": MERGE_DAYS,
            "min_hist_obs": MIN_HIST_OBS,
        },
        "split_dates": {k: sorted(v) for k, v in SPLIT_DATES.items()},
        "announcements": [{"date": d, "label": t} for d, t in ANNOUNCEMENTS],
        "categories": CATS,
        "coverage": {
            "note": "这些 ETF 份额历史太短，已排除在指数历史序列之外",
            "by_index": cov,
        },
        "stale": {
            "note": "K 线比最新份额日期短的指数，上游行情还没有这几天的数据",
            "by_index": stale,
        },
        "indices": meta_idx,
        "counts": {
            "indices": len(indices),
            "etfs": len(etfs),
            "etf_rows": etf_rows,
            "events": len(events_all),
        },
    }
    (DATA / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )

    print(f"最新交易日 {latest}")
    print(f"指数 {len(meta_idx)} 个，ETF {len(etfs)} 只，事件 {len(events_all)} 个")


if __name__ == "__main__":
    run(stats_only="--stats" in sys.argv)
