#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""国家队 ETF 动态站 —— 纯静态站点生成器

数据源：~/.hermes/data/astock/market.sqlite（bars / fund_shares / v_etf_daily）
产物：site/ 目录，纯 HTML+JS，可直接丢到任何静态服务器（nginx / GitHub Pages / OSS）。

用法：
    python3 build.py            # 生成站点
    python3 build.py --stats    # 只打印信号统计，不写文件
"""
from __future__ import annotations

import json
import math
import shutil
import sqlite3
import statistics
import sys
from bisect import bisect_left
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = Path.home() / ".hermes/data/astock/market.sqlite"
OUT = ROOT / "site"

# ---- 信号参数（全部写进页面图注，别只留在代码里） ----
FLOOR_YI = 2.0        # 单日净申赎绝对下限（亿元）
RATIO = 0.08          # 相对该指数 ETF 合计规模的占比（8%）
CAP_YI = 100.0        # 占比门槛的绝对上限（亿元），大指数不会因为规模大而失灵
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
    idx = json.loads((ROOT / "config/indices.json").read_text())["indices"]
    etfs = json.loads((ROOT / "config/etfs.json").read_text())["etfs"]
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
            {
                "date": r["date"],
                "o": r["open"],
                "h": r["high"],
                "l": r["low"],
                "c": r["close"],
            }
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
    """单指数信号：单日净申赎 ≥ max(2 亿, 5% × 该指数 ETF 合计规模)，且封顶 100 亿。

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


# ---------------------------------------------------------------- 页面

def fmt(x, n=2):
    if x is None:
        return "—"
    s = f"{x:,.{n}f}"
    return s


def cls_num(x):
    if x is None:
        return ""
    return "up" if x > 0 else ("down" if x < 0 else "")


def arrow(x):
    if x is None:
        return ""
    return "▲" if x > 0 else ("▼" if x < 0 else "—")


def page_shell(title, page, body, data=None, nav_active="home"):
    nav = f"""
    <nav class="nav">
      <a href="{page}index.html" class="{'active' if nav_active == 'home' else ''}">今日动态</a>
      <a href="{page}indices.html" class="{'active' if nav_active == 'idx' else ''}">分指数</a>
      <a href="{page}about.html" class="{'active' if nav_active == 'about' else ''}">口径说明</a>
    </nav>"""
    data_tag = (
        f'<script id="page-data" type="application/json">{json.dumps(data, ensure_ascii=False, separators=(",", ":"))}</script>'
        if data is not None
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{title}</title>
<meta name="description" content="ShareTrace：国家队 ETF 份额申赎、指数 K 线与买卖点">
<link rel="stylesheet" href="{page}assets/style.css">
</head>
<body>
<header class="top">
  <div class="wrap">
    <div class="brand">
      <span class="logo">ST</span>
      <div>
        <h1>ShareTrace</h1>
        <p class="sub">国家队 ETF 份额足迹 · 指数 K 线 · 买卖点</p>
      </div>
    </div>
    {nav}
  </div>
</header>
<main class="wrap">
{body}
</main>
<footer class="foot">
  <div class="wrap">
    <p>ShareTrace · 数据源：上交所 ETF 归档 / 深交所 / 同花顺 · 行情 baostock · 份额为基金总份额，非成交额</p>
    <p>本站只做数据呈现，不构成投资建议。生成时间 {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
  </div>
</footer>
{data_tag}
<script src="{page}assets/echarts.min.js"></script>
<script src="{page}assets/app.js"></script>
</body>
</html>
"""


def table(headers, rows, cls="", align=None):
    align = align or [""] * len(headers)
    th = "".join(f'<th class="{a}">{h}</th>' for h, a in zip(headers, align))
    body = []
    for r in rows:
        cells = []
        for v, a in zip(r, align):
            cells.append(f'<td class="{a}">{v}</td>')
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f'<table class="{cls}"><thead><tr>{th}</tr></thead><tbody>{"".join(body)}</tbody></table>'


def badge(side):
    return '<span class="tag buy">买入</span>' if side > 0 else '<span class="tag sell">卖出</span>'


# ---------------------------------------------------------------- 主流程

def build(stats_only=False):
    indices, etfs, by_code = load_config()
    conn = connect()
    series = load_etf_series(conn)

    latest = max(r["date"] for rows in series.values() for r in rows[-1:])

    # ---- 每只 ETF 的最新一行
    etf_latest = {}
    for e in etfs:
        rows = series.get(e["code"], [])
        if not rows:
            continue
        last = rows[-1]
        prev = rows[-2] if len(rows) > 1 else None
        chg = (
            (last["close"] / prev["close"] - 1) * 100
            if prev and prev["close"] and last["close"]
            else None
        )
        etf_latest[e["code"]] = {
            "share": last["share"],
            "d_share": last["d_share"],
            "flow": last["flow"],
            "close": last["close"],
            "chg": chg,
            "date": last["date"],
            "obs": len(rows),
            "first": rows[0]["date"],
        }

    # ---- 逐指数
    idx_pages = []
    for it in indices:
        codes = it["etfs"]
        use_hist, agg = aggregate(codes, series, MIN_HIST_OBS)
        dates = sorted(agg)
        events = detect_events(dates, agg) if len(dates) >= MIN_OBS else []

        # K 线：指数优先，缺则用代理 ETF
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

        # 流量序列：只用份额历史足够长的 ETF，避免尾部跳变
        agg_use = agg
        dts = sorted(agg_use)
        if not dts:
            # 全部 ETF 都没有足够历史（如纯深市指数），退到当日口径，至少能显示今天
            use_hist, agg_use = aggregate(codes, series, 0)
            dts = sorted(agg_use)

        if stats_only:
            idx_pages.append((it["name"], len(dts), len(events), events))
            continue

        # ---- 图表：K 线铺满，流量按日期对齐（无数据处留空）
        # 份额历史够长就从份额起点画；只有几天（纯深市指数）则退回指数全历史，
        # 否则图上只剩三根蜡烛，看不出任何走势。
        if dts and len(dts) >= MIN_HIST_OBS:
            start = dts[0]
        else:
            start = kdates[0] if kdates else (dts[0] if dts else None)
        chart_dates = [d for d in kdates if start and d >= start]
        if not chart_dates and kdates:
            chart_dates = kdates
        kline, ma20, ma60, flows, cum = [], [], [], [], []
        closes, win, prev_cum = [], [], None
        for d in chart_dates:
            b = bar_map[d]
            kline.append([b["o"], b["c"], b["l"], b["h"]])
            closes.append(b["c"])
            n = len(closes)
            ma20.append(round(sum(closes[-20:]) / 20, 3) if n >= 20 else None)
            ma60.append(round(sum(closes[-60:]) / 60, 3) if n >= 60 else None)
            if d in agg_use:
                f = agg_use[d]["flow"]
                win.append(f)
                if len(win) > 20:
                    win.pop(0)
                flows.append(round(f, 2))
                prev_cum = round(sum(win), 2)
                cum.append(prev_cum)
            else:
                flows.append(None)
                cum.append(prev_cum)

        kd_set = set(chart_dates)

        # ---- 事件后验
        for e in events:
            e["post20"] = post_return(kdates, kclose, e["date"], e["side"])
        buys = [e for e in events if e["side"] > 0 and e["post20"] is not None]
        sells = [e for e in events if e["side"] < 0 and e["post20"] is not None]

        def stat(evs):
            if not evs:
                return None
            rets = [e["post20"] for e in evs]
            return {
                "n": len(rets),
                "avg": round(sum(rets) / len(rets), 2),
                "win": round(100 * sum(1 for r in rets if r > 0) / len(rets)),
            }

        latest_row = agg_use.get(latest)
        last_dates = sorted(agg_use)
        d5 = round(sum(agg_use[d]["flow"] for d in last_dates[-5:]), 2) if last_dates else None
        d20 = round(sum(agg_use[d]["flow"] for d in last_dates[-20:]), 2) if last_dates else None

        idx_pages.append(
            {
                "slug": it["slug"],
                "name": it["name"],
                "cat": it["cat"],
                "indexCode": kcode,
                "ksource": ksource,
                "proxyName": by_code.get(it["proxy"], {}).get("name") if it["proxy"] else None,
                "chart": {
                    "dates": chart_dates,
                    "kline": kline,
                    "ma20": ma20,
                    "ma60": ma60,
                    "flows": flows,
                    "cum": cum,
                    "events": [
                        {
                            "date": e["date"],
                            "side": e["side"],
                            "amount": e["amount"],
                            "total": e["total"],
                            "days": e["days"],
                            "reson": e["reson"],
                            "n": e["n"],
                            "span": e["span"],
                        }
                        for e in events
                        if e["date"] in kd_set
                    ],
                    "ann": [a[0] for a in ANNOUNCEMENTS],
                },
                "events": events,
                "stats": {
                    "latest": latest,
                    "today": round(latest_row["flow"], 2) if latest_row else None,
                    "todayN": latest_row["n"] if latest_row else 0,
                    "todayIn": latest_row["nin"] if latest_row else 0,
                    "todayOut": latest_row["nout"] if latest_row else 0,
                    "d5": d5,
                    "d20": d20,
                    "flowObs": len(dts),
                    "flowFirst": dts[0] if dts else None,
                    "first": chart_dates[0] if chart_dates else None,
                    "last": chart_dates[-1] if chart_dates else None,
                    "buyStat": stat(buys),
                    "sellStat": stat(sells),
                },
                "etfs": [
                    {
                        "code": c,
                        "name": by_code[c]["name"],
                        "tier": by_code[c]["tier"],
                        "market": by_code[c]["market"],
                        "latest": etf_latest.get(c),
                        "hist": len(series.get(c, [])),
                    }
                    for c in codes
                ],
            }
        )

    if stats_only:
        total = 0
        for name, n, k, evs in idx_pages:
            total += k
            print(f"{name:<14} bars={n:<5} events={k:<3} " + " ".join(
                f"{e['date']}{'B' if e['side']>0 else 'S'}{e['amount']:+.0f}" for e in evs[-6:]
            ))
        print("总事件数", total)
        return

    # ---- 写站点
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "assets").mkdir(parents=True)
    (OUT / "index").mkdir()
    for f in ("echarts.min.js", "app.js", "style.css"):
        shutil.copy(ROOT / "assets" / f, OUT / "assets" / f)

    home_html, home_data = render_home(idx_pages, etfs, by_code, series, latest)
    (OUT / "index.html").write_text(page_shell("ShareTrace · 国家队 ETF 份额足迹", "", home_html, home_data, "home"))

    idx_html = render_indices(idx_pages)
    (OUT / "indices.html").write_text(page_shell("分指数 · ShareTrace", "", idx_html, None, "idx"))

    about_html = render_about()
    (OUT / "about.html").write_text(page_shell("口径说明 · ShareTrace", "", about_html, None, "about"))

    for p in idx_pages:
        body, data = render_detail(p)
        (OUT / "index" / f"{p['slug']}.html").write_text(
            page_shell(f"{p['name']} · ShareTrace", "../", body, data, "idx")
        )

    n_ev = sum(len(p["events"]) for p in idx_pages)
    print(f"生成完成：{len(idx_pages)} 个指数页 / {n_ev} 个信号事件 / 最新交易日 {latest}")
    print(f"输出目录：{OUT}")


def render_home(idx_pages, etfs, by_code, series, latest):
    # 沪市 47 只（历史连续）合计
    sh_codes = [e["code"] for e in etfs if e["market"] == "SH"]
    _, agg_sh = aggregate(sh_codes, series)
    dates = sorted(agg_sh)
    total_today = round(agg_sh[latest]["flow"], 2) if latest in agg_sh else None
    nin = agg_sh[latest]["nin"] if latest in agg_sh else 0
    nout = agg_sh[latest]["nout"] if latest in agg_sh else 0
    d5 = round(sum(agg_sh[d]["flow"] for d in dates[-5:]), 2)
    d20 = round(sum(agg_sh[d]["flow"] for d in dates[-20:]), 2)

    # 当日信号
    today_events = [e for p in idx_pages for e in p["events"] if e["date"] == latest]

    # 分指数当日排行
    rows = []
    for p in idx_pages:
        s = p["stats"]
        if s["today"] is None:
            continue
        rows.append(
            (
                f'<a href="index/{p["slug"]}.html">{p["name"]}</a>',
                p["cat"],
                f'<span class="{cls_num(s["today"])}">{arrow(s["today"])} {fmt(s["today"])}</span>',
                f'<span class="{cls_num(s["d5"])}">{fmt(s["d5"])}</span>',
                f'<span class="{cls_num(s["d20"])}">{fmt(s["d20"])}</span>',
                f'{s["todayIn"]}/{s["todayN"]}',
                "●" if any(e["date"] == latest for e in p["events"]) else "",
                abs(s["today"]),
            )
        )
    rows.sort(key=lambda r: -r[-1])
    idx_table = table(
        ["指数", "大类", f"{latest} 净申赎(亿)", "近5日(亿)", "近20日(亿)", "净申购/只数", "信号"],
        [r[:-1] for r in rows[:20]],
        cls="tbl",
        align=["", "", "num", "num", "num", "num", "c"],
    )

    # ETF 异动
    movers = []
    for e in etfs:
        rows_ = series.get(e["code"], [])
        if not rows_ or rows_[-1]["date"] != latest:
            continue
        movers.append((e, rows_[-1]))
    movers.sort(key=lambda x: -abs(x[1]["flow"]))
    mrows = [
        (
            f'<span class="code">{e["code"]}</span> {e["name"]}',
            e["tier"],
            fmt(r["share"]),
            f'<span class="{cls_num(r["flow"])}">{fmt(r["flow"])}</span>',
            f'<span class="{cls_num(r["d_share"])}">{fmt(r["d_share"])}</span>',
        )
        for e, r in movers[:12]
    ]
    mover_table = table(
        ["ETF", "档", "份额(亿份)", "当日净申赎(亿)", "Δ份额(亿份)"],
        mrows,
        cls="tbl",
        align=["", "c", "num", "num", "num"],
    )

    # 事件时间线
    all_ev = sorted(
        [(e, p) for p in idx_pages for e in p["events"]], key=lambda x: x[0]["date"], reverse=True
    )
    ev_rows = [
        (
            e["date"],
            f'<a href="index/{p["slug"]}.html">{p["name"]}</a>',
            badge(e["side"]),
            f'<span class="{cls_num(e["amount"])}">{fmt(e["amount"])}</span>',
            f'{e["days"]} 天 / {fmt(e["total"])}',
            f'{e["reson"]}/{e["n"]}',
            f'<span class="{cls_num(e["post20"])}">{fmt(e["post20"], 1)}%</span>' if e["post20"] is not None else "—",
        )
        for e, p in all_ev[:40]
    ]
    ev_table = table(
        ["日期", "指数", "方向", "当日(亿)", "连续/合计(亿)", "共振", "后20日"],
        ev_rows,
        cls="tbl",
        align=["", "", "c", "num", "num", "c", "num"],
    )

    # 日度历史（客户端分页）
    hist = []
    for i, d in enumerate(dates):
        a = agg_sh[d]
        hist.append(
            {
                "d": d,
                "f": round(a["flow"], 2),
                "nin": a["nin"],
                "nout": a["nout"],
                "ev": sum(1 for p in idx_pages for e in p["events"] if e["date"] == d),
            }
        )
    hist.reverse()

    # 首页 120 日图
    tail = dates[-120:]
    chart = {
        "dates": tail,
        "flows": [round(agg_sh[d]["flow"], 2) for d in tail],
        "cum": [],
    }
    run, win = 0.0, []
    for d in tail:
        win.append(agg_sh[d]["flow"])
        if len(win) > 20:
            win.pop(0)
        run = sum(win)
        chart["cum"].append(round(run, 2))

    today = date.today().isoformat()
    stale_note = (
        f'<span class="stale">今日 {today} 的份额 21:18 后更新</span>' if today != latest else ""
    )

    body = f"""
<section class="hero">
  <div class="hero-main">
    <p class="eyebrow">最新交易日 · {latest}{stale_note}</p>
    <div class="big {cls_num(total_today)}">{arrow(total_today)} {fmt(total_today)}<span class="unit">亿元</span></div>
    <p class="hero-note">沪市 47 只国家队 ETF 当日合计净申赎（申购为净流入，正数=净申购）</p>
  </div>
  <div class="kpis">
    <div class="kpi"><span>净申购 / 净赎回</span><b>{nin} / {nout} 只</b></div>
    <div class="kpi"><span>近 5 日累计</span><b class="{cls_num(d5)}">{fmt(d5)} 亿</b></div>
    <div class="kpi"><span>近 20 日累计</span><b class="{cls_num(d20)}">{fmt(d20)} 亿</b></div>
    <div class="kpi"><span>当日信号</span><b>{len(today_events)} 个</b></div>
  </div>
</section>

<section class="card">
  <h2>今日信号</h2>
  {"".join(f'<div class="evline">{badge(e["side"])} <a href="index/{p["slug"]}.html">{p["name"]}</a> <b class="{cls_num(e["amount"])}">{fmt(e["amount"])} 亿</b> <span class="dim">{e["days"]} 个交易日 / 合计 {fmt(e["total"])} 亿 / 共振 {e["reson"]}/{e["n"]}</span></div>' for p in idx_pages for e in p["events"] if e["date"] == latest) or '<p class="dim">当日无指数触发买卖信号（阈值见「口径说明」）。</p>'}
</section>

<section class="card">
  <h2>分指数当日净申赎 <span class="dim">按金额排序，前 20</span></h2>
  <div class="table-wrap">{idx_table}</div>
  <p class="more"><a href="indices.html">查看全部 43 个指数 →</a></p>
</section>

<section class="card">
  <h2>ETF 异动榜 <span class="dim">按 |当日净申赎| 排序</span></h2>
  <div class="table-wrap">{mover_table}</div>
</section>

<section class="card">
  <h2>沪市合计净申赎 <span class="dim">近 120 个交易日</span></h2>
  <div id="homeChart" class="chart chart-sm"></div>
  <p class="note">柱=当日净申赎（红=净申购，绿=净赎回），线=20 日滚动累计。深市 10 只 2026-10-21 起才有份额数据，未计入历史序列。</p>
</section>

<section class="card">
  <h2>历史信号</h2>
  <div class="table-wrap">{ev_table}</div>
</section>

<section class="card">
  <h2>历史记录 <span class="dim">全部交易日 · 沪市 47 只</span></h2>
  <div class="table-wrap" id="histTable"></div>
  <div class="pager" id="histPager"></div>
</section>
"""
    data = {"type": "home", "chart": chart, "hist": hist}
    return body, data


def render_indices(idx_pages):
    cards = []
    for p in idx_pages:
        s = p["stats"]
        sig = ""
        if p["events"]:
            e = p["events"][-1]
            sig = f'<span class="tag {"buy" if e["side"]>0 else "sell"}">{e["date"][5:]} {"买" if e["side"]>0 else "卖"} {e["amount"]:+.0f}</span>'
        cards.append(
            f"""<a class="icard" href="index/{p['slug']}.html" data-cat="{p['cat']}">
  <div class="ic-head"><b>{p['name']}</b><span class="dim">{p['cat']}</span></div>
  <div class="ic-num {cls_num(s['today'])}">{fmt(s['today'])}<span class="unit">亿</span></div>
  <div class="ic-meta">5日 {fmt(s['d5'])} · 20日 {fmt(s['d20'])}</div>
  <div class="ic-foot">{sig or '<span class="dim">暂无信号</span>'}</div>
</a>"""
        )
    tabs = '<button class="tab active" data-cat="all">全部</button>' + "".join(
        f'<button class="tab" data-cat="{c}">{c}</button>' for c in CATS
    )
    return f"""
<section class="card">
  <h2>分指数 <span class="dim">43 个标的指数 / 57 只 ETF · 点开看 K 线与买卖点</span></h2>
  <div class="tabs" id="catTabs">{tabs}</div>
  <div class="igrid" id="igrid">{"".join(cards)}</div>
  <div class="pager" id="idxPager"></div>
</section>
"""


def render_detail(p):
    s = p["stats"]
    rows = []
    for e in p["events"]:
        top = "、".join(f"{by_name(c)} {v:+.1f}" for c, v in e["top"])
        rows.append(
            (
                e["date"],
                badge(e["side"]),
                f'<span class="{cls_num(e["amount"])}">{fmt(e["amount"])}</span>',
                e["days"],
                f'<span class="{cls_num(e["total"])}">{fmt(e["total"])}</span>',
                f'{e["reson"]}/{e["n"]}',
                f'<span class="dim">{top}</span>',
                f'<span class="{cls_num(e["post20"])}">{fmt(e["post20"], 1)}%</span>' if e["post20"] is not None else "—",
            )
        )
    ev_table = table(
        ["日期", "方向", "当日(亿)", "连续天数", "合计(亿)", "共振", "前三标的(亿)", "后20日"],
        rows,
        cls="tbl",
        align=["", "c", "num", "c", "num", "c", "", "num"],
    )

    erows = []
    for e in p["etfs"]:
        lt = e["latest"]
        erows.append(
            (
                f'<span class="code">{e["code"]}</span>',
                e["name"],
                e["tier"],
                fmt(lt["share"]) if lt else "—",
                f'<span class="{cls_num(lt["flow"])}">{fmt(lt["flow"])}</span>' if lt else "—",
                f'<span class="{cls_num(lt["d_share"])}">{fmt(lt["d_share"])}</span>' if lt else "—",
                f'{lt["obs"]} 天' if lt else "0",
                "完整" if e["hist"] >= MIN_HIST_OBS else "仅近期",
            )
        )
    etf_table = table(
        ["代码", "名称", "档", "份额(亿份)", "当日净申赎(亿)", "Δ份额(亿份)", "份额历史", "状态"],
        erows,
        cls="tbl",
        align=["", "", "c", "num", "num", "num", "c", "c"],
    )

    bs, ss = s["buyStat"], s["sellStat"]
    stat_html = ""
    if bs or ss:
        parts = []
        if bs:
            parts.append(f"买点后 20 日：均值 {bs['avg']:+.1f}% / 胜率 {bs['win']}% / {bs['n']} 次")
        if ss:
            parts.append(f"卖点后 20 日（按跌计）：均值 {ss['avg']:+.1f}% / 胜率 {ss['win']}% / {ss['n']} 次")
        stat_html = f'<p class="note">{" · ".join(parts)}</p>'

    warn = ""
    if s.get("flowObs", 0) < MIN_HIST_OBS:
        warn = (
            '<div class="warn">该指数的 ETF 全在深市，没有免费的日度份额历史'
            f"（{s.get('flowFirst')} 起才有 {s.get('flowObs', 0)} 个交易日的数据），"
            "所以只有最近的流量柱，算不出历史买卖点。</div>"
        )
    ksrc_note = (
        f"K 线为指数 {p['indexCode']}"
        if p["ksource"] == "index"
        else f"K 线以 {p['proxyName'] or p['indexCode']} 价格替代（该指数行情不可得）"
    )

    body = f"""
<section class="detail-head">
  <p class="crumb"><a href="../indices.html">分指数</a> / {p['cat']}</p>
  <h2>{p['name']}</h2>
  <div class="kpis">
    <div class="kpi"><span>当日净申赎</span><b class="{cls_num(s['today'])}">{fmt(s['today'])} 亿</b></div>
    <div class="kpi"><span>净申购/只数</span><b>{s['todayIn']}/{s['todayN']}</b></div>
    <div class="kpi"><span>近 5 日</span><b class="{cls_num(s['d5'])}">{fmt(s['d5'])} 亿</b></div>
    <div class="kpi"><span>近 20 日</span><b class="{cls_num(s['d20'])}">{fmt(s['d20'])} 亿</b></div>
  </div>
  <p class="note">数据 {s['first']} → {s['last']} · {ksrc_note}</p>
  {warn}
</section>

<section class="card">
  <div id="detailChart" class="chart"></div>
  <div class="legend">
    <span><i class="dot up"></i>买点（净申购）</span>
    <span><i class="dot down"></i>卖点（净赎回）</span>
    <span><i class="dot ann"></i>国家队公告日</span>
    <span class="dim">图上标记为该指数口径的自适应阈值信号，见下方图注</span>
  </div>
  {stat_html}
</section>

<section class="card">
  <h2>信号明细</h2>
  <div class="table-wrap">{ev_table if p['events'] else '<p class="dim">该指数区间内没有触发信号。</p>'}</div>
</section>

<section class="card">
  <h2>成分 ETF</h2>
  <div class="table-wrap">{etf_table}</div>
</section>

<section class="card notes">
  <h2>图注</h2>
  <ol>
    <li><b>标记条件</b>：当日该指数下 ETF 合计净申赎 |F| ≥ max({FLOOR_YI:.0f} 亿, {RATIO*100:.0f}% × 该指数 ETF 合计规模)，占比门槛封顶 {CAP_YI:.0f} 亿；成分 ETF ≥2 只时同向只数需 ≥ max(2, 60%)。{MERGE_DAYS} 个交易日内同向连续触发合并为一个事件，标在 |F| 最大那天。</li>
    <li><b>为什么这么设</b>：固定金额门槛对不同规模指数不公平——沪深300 四只 ETF 合计规模近万亿，2 亿只是噪音；行业指数只有几十亿，100 亿永远触发不了。用「自身规模的 8%」当尺子，再压 2 亿地板、封顶 100 亿，跨指数才可比。</li>
    <li><b>公告是事后确认</b>：2015-07、2023-10、2024-02、2025-04 四次汇金公告都在份额异动之后，2024-02 公告当天 510300 份额仅 +0.72%。别把公告日当买入起点。</li>
    <li><b>口径限制</b>：份额是基金总份额（净申赎），不是成交额；已剔除份额折算日；深市 10 只 ETF 2026-10-21 起才有日度份额，不进历史序列；金额 = Δ份额 × 当日收盘价。</li>
  </ol>
</section>
"""
    data = {
        "type": "detail",
        "name": p["name"],
        "chart": p["chart"],
    }
    return body, data


_ETF_NAMES = {}


def by_name(code):
    return _ETF_NAMES.get(code, code)


def render_about():
    return f"""
<section class="card">
  <h2>这个站看什么</h2>
  <p class="note">ShareTrace 指「份额的足迹」——国家队买 ETF 走一级市场申购，唯一直接指标是基金总份额；把份额变化画到指数 K 线上，就是它的足迹。</p>
  <p>国家队（中央汇金投资 / 汇金资管、证金、国新、诚通）买 ETF 走一级市场申购，<b>份额增加与二级市场成交无关</b>。唯一直接指标是基金总份额，不是成交额、不是规模、不是盘口异动。本站把 57 只「国家队曾出现在前十大持有人」的 ETF 的日度份额变化，折算成金额，画到 43 个标的指数的 K 线上。</p>
  <p class="note">成交额法已被实测证伪：510300 在 2012-05~2026-10 的 3500 个交易日里，成交额 &gt;2×20 日均触发 129 天，落在国家队公告 ±2 日内的只有 5 天，精确率 4%。</p>
</section>
<section class="card">
  <h2>信号规则</h2>
  <ol>
    <li>日净申赎 F = Σ(Δ份额 × 当日收盘价)，按标的指数加总。</li>
    <li>触发：|F| ≥ max({FLOOR_YI:.0f} 亿, {RATIO*100:.0f}% × 该指数 ETF 合计规模)，封顶 {CAP_YI:.0f} 亿；成分 ETF ≥2 只时要求同向只数 ≥ max(2, 60%)。</li>
    <li>{MERGE_DAYS} 个交易日内同向连续触发合并，标在 |F| 最大那天。</li>
    <li>每个事件统计「后 20 个交易日」指数涨跌：买点看涨、卖点看跌，正数=信号有效。</li>
  </ol>
  <p class="note">为什么用「规模的 5%」而不是固定金额：沪深300 四只 ETF 合计规模近万亿，行业指数只有几十亿，同一个金额门槛在两类指数上完全不是一回事。封顶 100 亿是为了让万亿级指数也保留灵敏度。</p>
  <p class="note">信号方向对、强度不够当择时用。27 个季度样本里份额变化与下一季收益的相关性只有 +0.21，独立事件仅 3~4 个。</p>
</section>
<section class="card">
  <h2>已知口径限制</h2>
  <ul>
    <li>份额折算（2 份合 1 份）会造出假申赎，已剔除 510310 / 512100 / 560010 三个折算日。</li>
    <li>深市 10 只 ETF（159919、159845、159629、159915、159901、159922、159995、159870、159992、159949）没有免费的日度份额历史，2026-10-21 起才有；它们不进历史序列，只出现在当日数据里。</li>
    <li>沪市份额来自上交所归档接口（最早 2012-01-04），覆盖度逐年爬升：2012 年 5 只、2020 年 34 只、2024 年起 47 只。跨年比较要按当年在册只数归一。</li>
    <li>「份额大增」不等于「国家队买入」：2022-12 510300 份额 +82%，汇金持仓纹丝未动，那波是保险理财在买。</li>
    <li>「大额净赎回」也不等于清仓：2026-01 的 13 个交易日合计 -7722 亿，同期沪深300 走平，更像换仓／转托管。</li>
  </ul>
</section>
<section class="card">
  <h2>更新方式</h2>
  <p>数据由本地脚本从 <code>market.sqlite</code> 生成静态页面：<code>python3 build.py</code>，产物在 <code>site/</code>，可直接部署到任何静态服务器。</p>
</section>
"""


if __name__ == "__main__":
    stats_only = "--stats" in sys.argv
    _, etfs_cfg, _ = load_config()
    _ETF_NAMES.update({e["code"]: e["name"] for e in etfs_cfg})
    build(stats_only=stats_only)
