#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日增量：抓当天数据，追加到 data/，不碰 sqlite。

源（都不用 key，GitHub runner 实测可达）：
  上交所 commonQuery    —— 沪市 ETF 当日份额，一次拿全市场（21:18 后出）
  深交所 1945 报表      —— 深市 ETF 当日份额，逐只，偶发 reset，带重试
  腾讯 qt.gtimg.cn      —— 批量快照，一次几十只，给当天 K 线的开高低收
  腾讯 / 新浪 fqkline   —— 历史日线，只用于补缺口（有频率限制，慢跑）
  中证 index-perf       —— H30184 / 930726 这类自编指数，腾讯没有

设计：data/ 自己就是状态。算 Δ份额只需要上一交易日的份额，就在 CSV 最后一行。
所以一台干净的 runner 上也能跑，不需要任何数据库。

用法：
    python3 daily.py                      # 抓今天（北京时间），没新数据就安静退出（退出码 3）
    python3 daily.py --date 2026-10-23    # 补指定日期
    python3 daily.py --backfill           # 把每个序列缺的交易日都补上
    python3 daily.py --dry-run            # 只打印要追加什么
"""
from __future__ import annotations

import csv
import json
import re
import ssl
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ingest  # noqa: E402  复用同一套信号规则与常量

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CST = timezone(timedelta(hours=8))   # 北京时间（UTC+8）
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

INDEX_COLS = ["date", "o", "h", "l", "c", "flow", "n_in", "n_out", "n_etf", "scale"]
ETF_COLS = ["date", "close", "share", "flow", "d_share"]

EXIT_OK = 0
EXIT_NO_DATA = 3      # 当天数据还没发布 / 没有新行，不是错误
EXIT_FAIL = 1

QUOTE_BATCH = 40      # 一次快照查多少只
TENCENT_GAP = 0.45    # 腾讯历史日线的请求间隔（太快会 501）
CSINDEX_GAP = 1.25    # 中证官网串行间隔，并发即封 IP


def log(msg):
    print(msg, flush=True)


# ------------------------------------------------------------------ HTTP

def get(url, headers=None, timeout=40, insecure=False, tries=3, backoff=2.5):
    last = None
    ctx = None
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            if i < tries - 1:
                time.sleep(backoff * (i + 1))
    raise last  # type: ignore[misc]


# ------------------------------------------------------------------ 份额

def sse_snapshot(day: str) -> dict[str, float]:
    """上交所当日全市场 ETF 份额（亿份）。"""
    url = ("https://query.sse.com.cn/commonQuery.do?isPagination=true&pageHelp.pageSize=10000"
           "&pageHelp.pageNo=1&pageHelp.beginPage=1&pageHelp.cacheSize=1&pageHelp.endPage=1"
           "&sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L&STAT_DATE=" + day)
    raw = get(url, {"Referer": "https://www.sse.com.cn/"}, timeout=45, insecure=True)
    rows = json.loads(raw.decode("utf-8", "replace")).get("result") or []
    out = {}
    for r in rows:
        code = str(r.get("SEC_CODE", "")).strip()
        try:
            v = float(r.get("TOT_VOL", 0) or 0)
        except (TypeError, ValueError):
            continue
        if code and v > 0:
            out[code] = v / 1e4
    return out


def szse_share(code: str, day: str) -> float | None:
    """深交所 1945 报表，dqgm 单位万份 → 亿份。

    dqgm 是 T-1 数据，页面会标注日期；标注不是 day 就重试，仍不是则返回 None。
    这个标注在 T 日晚间到 T+1 凌晨之间才从 T-1 翻到 T，不能凭 subname 判断。
    """
    url = ("http://www.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON"
           f"&CATALOGID=1945&TABKEY=tab1&PAGENO=1&txtQueryKeyAndJC={code}")
    for _attempt in range(3):
        try:
            raw = get(url, {"Referer": "http://www.szse.cn/"}, timeout=40, tries=3)
            text = raw.decode("utf-8", "replace")
            rows = json.loads(text)[0].get("data") or []
        except Exception as e:  # noqa: BLE001
            log(f"  warn 深交所 {code}: {type(e).__name__}")
            return None
        m = re.search(r"当前规模.*?（(\d{4}-\d{2}-\d{2})）", text, re.S)
        label = m.group(1) if m else ""
        if label != day:
            log(f"  warn 深交所 {code}: 页面日期 {label or '?'} != {day}")
            time.sleep(1.0)
            continue
        for r in rows:
            if code in str(r.get("sys_key", "")):
                try:
                    return float(str(r.get("dqgm", "")).replace(",", "")) / 1e4
                except ValueError:
                    return None
        return None
    return None


# ------------------------------------------------------------------ 价格

def tencent_symbol(code: str) -> str | None:
    """data 里的代码 → 腾讯符号；中证自编指数（H/930/931）返回 None。"""
    if "." in code:
        return code.replace(".", "")
    if re.fullmatch(r"\d{6}", code):
        return ("sz" if code.startswith("399") else "sh") + code
    if re.fullmatch(r"[Hh]\d{6}|9\d{5}", code):
        return None      # 中证自编指数，腾讯没有
    log(f"  warn 代码 {code} 认不出市场前缀，按中证自编指数处理")
    return None


def batch_quote(symbols: list[str]) -> dict[str, dict]:
    """腾讯批量快照：一次几十只，返回当日开高低收。

    收盘后「现价」就是收盘价；时间戳不是当天的一律丢掉，
    免得节假日把上一交易日的快照当成今天写进去。
    """
    out = {}
    for i in range(0, len(symbols), QUOTE_BATCH):
        chunk = symbols[i:i + QUOTE_BATCH]
        try:
            raw = get("http://qt.gtimg.cn/q=" + ",".join(chunk),
                      {"Referer": "http://finance.qq.com"}, timeout=30)
        except Exception as e:  # noqa: BLE001
            log(f"  warn 快照失败 {chunk[0]}…: {type(e).__name__}")
            continue
        for line in raw.decode("gbk", "replace").splitlines():
            if '="' not in line:
                continue
            sym = line.split("=")[0].replace("v_", "").strip()
            f = line.split('="')[1].rstrip('";').split("~")
            if len(f) < 35:
                continue
            try:
                stamp = f[30][:8]
                out[sym] = {
                    "stamp": stamp,
                    "date": f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}",
                    "o": float(f[5]), "h": float(f[33]), "l": float(f[34]), "c": float(f[3]),
                }
            except (IndexError, ValueError):
                continue
        time.sleep(0.25)
    return out


def tencent_history(symbol: str, start: str, end: str) -> dict[str, list[float]]:
    """腾讯不复权日线 → {date: [open, high, low, close]}。

    返回数组是 开/收/高/低，这里换成 开/高/低/收。
    """
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={symbol},day,{start},{end},640,")
    raw = get(url, {"Referer": "https://gu.qq.com/"}, timeout=30)
    node = (json.loads(raw.decode("utf-8")).get("data") or {}).get(symbol) or {}
    out = {}
    for r in node.get("day") or []:
        try:
            out[r[0]] = [float(r[1]), float(r[3]), float(r[4]), float(r[2])]
        except (IndexError, TypeError, ValueError):
            continue
    return out


def sina_history(symbol: str, count: int = 320) -> dict[str, list[float]]:
    """新浪日线，腾讯被限频时的第二路。"""
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=no&datalen={count}")
    raw = get(url, {"Referer": "https://finance.sina.com.cn/"}, timeout=30)
    rows = json.loads(raw.decode("utf-8", "replace"))
    out = {}
    for r in rows or []:
        try:
            out[r["day"][:10]] = [float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])]
        except (KeyError, TypeError, ValueError):
            continue
    return out


def csindex_history(code: str, start: str, end: str) -> dict[str, list[float]]:
    """中证官网日线。日期参数必须 %Y%m%d，带横杠会返回空 data。"""
    url = ("https://www.csindex.com.cn/csindex-home/perf/index-perf"
           f"?indexCode={code}&startDate={start.replace('-', '')}&endDate={end.replace('-', '')}")
    raw = get(url, {"Referer": "https://www.csindex.com.cn/"}, timeout=30)
    rows = json.loads(raw.decode("utf-8")).get("data") or []
    out = {}
    for r in rows:
        try:
            d = str(r["tradeDate"])
            out[f"{d[:4]}-{d[4:6]}-{d[6:8]}"] = [
                float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])]
        except (KeyError, TypeError, ValueError):
            continue
    return out


_tx_fail = 0   # 腾讯连续失败次数，达到 3 就整轮改用新浪，别把时间耗在限频上


def history_for(code: str, start: str, end: str) -> dict[str, list[float]]:
    """历史日线：带前缀/裸 6 位走腾讯→新浪，中证自编指数走官网。"""
    global _tx_fail
    sym = tencent_symbol(code)
    if sym:
        if _tx_fail < 3:
            try:
                rows = tencent_history(sym, start, end)
                time.sleep(TENCENT_GAP)
                if rows:
                    _tx_fail = 0
                    return rows
            except Exception as e:  # noqa: BLE001
                # 501/403 是限频，不是偶发：直接整轮停用腾讯，别再一个个撞
                if getattr(e, "code", None) in (403, 501):
                    _tx_fail = 3
                    log(f"  warn 腾讯历史 {sym}: HTTP {e.code}（限频，本轮改用新浪）")
                else:
                    _tx_fail += 1
                    log(f"  warn 腾讯历史 {sym}: {type(e).__name__}（连续 {_tx_fail} 次）")
                    time.sleep(1.5)
        try:
            return sina_history(sym)
        except Exception as e:  # noqa: BLE001
            log(f"  warn 新浪历史 {sym}: {type(e).__name__}")
            return {}
    code_cs = code.split(".")[-1]
    try:
        rows = csindex_history(code_cs, start, end)
        return rows
    except Exception as e:  # noqa: BLE001
        log(f"  warn 中证 {code_cs}: {type(e).__name__}")
        return {}
    finally:
        time.sleep(CSINDEX_GAP)


# ------------------------------------------------------------------ CSV

def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def append_rows(path: Path, rows: list[list]) -> int:
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(",".join("" if v is None else str(v) for v in r) + "\n")
    return len(rows)


def num(x, n=2):
    return None if x is None else round(x, n)


# ------------------------------------------------------------------ 主流程

def last_share_date(etfs) -> str:
    last = ""
    for e in etfs:
        path = DATA / "etf" / f"{e['code']}.csv"
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as f:
            tail = f.readlines()[-1:]
        if tail:
            last = max(last, tail[0].split(",")[0])
    return last


def main(argv):
    dry = "--dry-run" in argv
    backfill = "--backfill" in argv
    want = None
    for i, a in enumerate(argv):
        if a == "--date" and i + 1 < len(argv):
            want = argv[i + 1]

    # 不带 --date 时补齐「上次数据之后的所有工作日」。
    # 定在某个钟点跑，可能撞上交易所还没发布当天数据；只抓当天的话那一天就永远丢了。
    # 这里逐个工作日试，没发布的跳过，第二天还会再来一次。
    if want is None and "--once" not in argv:
        etfs = json.loads((DATA / "etfs.json").read_text(encoding="utf-8"))["etfs"]
        last = last_share_date(etfs)
        if not last:
            log("data/etf 下没有数据，先用 ingest.py 初始化")
            return EXIT_FAIL
        today_s = datetime.now(CST).strftime("%Y-%m-%d")
        d0 = date.fromisoformat(last) + timedelta(days=1)
        days = []
        while d0 <= date.fromisoformat(today_s):
            if d0.weekday() < 5:
                days.append(d0.isoformat())
            d0 += timedelta(days=1)
        if not days:
            log(f"最新数据已是 {last}，没有待补的工作日")
            return EXIT_NO_DATA
        log(f"待补齐的工作日：{', '.join(days)}")
        worst = EXIT_OK
        for d in days:
            rc = main(["--date", d, "--once"] + [a for a in argv if a in ("--dry-run", "--backfill")])
            if rc == EXIT_FAIL:
                worst = EXIT_FAIL
            elif rc == EXIT_OK and worst != EXIT_FAIL:
                worst = EXIT_OK
        return worst

    meta = json.loads((DATA / "meta.json").read_text(encoding="utf-8"))
    indices = json.loads((DATA / "indices.json").read_text(encoding="utf-8"))["indices"]
    etfs = json.loads((DATA / "etfs.json").read_text(encoding="utf-8"))["etfs"]

    today = datetime.now(CST).strftime("%Y-%m-%d")
    target = want or today
    is_today = target >= today
    log(f"目标日期 {target}（{'今天' if is_today else '补历史'}{'，backfill' if backfill else ''}{'，dry-run' if dry else ''}）")

    # ---- 已有数据
    etf_series: dict[str, list[dict]] = {}
    for e in etfs:
        etf_series[e["code"]] = read_csv(DATA / "etf" / f"{e['code']}.csv")
    if not any(etf_series.values()):
        log("data/etf 下没有数据，先用 ingest.py 初始化")
        return EXIT_FAIL

    cal = set()
    for it in indices:
        for r in read_csv(DATA / "index" / f"{it['slug']}.csv"):
            cal.add(r["date"])
    # 目标日通常还没写进任何 CSV，但它是交易日；日历必须带上它，
    # 否则「上一行是不是紧邻的前一交易日」永远不成立，Δ份额会全算成 0。
    cal.add(target)
    cal = sorted(cal)
    cal_pos = {d: i for i, d in enumerate(cal)}

    # ---- 份额
    shares: dict[str, float] = {}
    log("取上交所快照 …")
    try:
        snap = sse_snapshot(target)
    except Exception as e:  # noqa: BLE001
        log(f"  上交所失败：{type(e).__name__}: {e}")
        return EXIT_FAIL
    if not snap:
        log(f"  上交所 {target} 快照为空（当天数据通常 21:18 后发布）")
        return EXIT_NO_DATA
    for e in etfs:
        if e["market"] == "SH" and e["code"] in snap:
            shares[e["code"]] = snap[e["code"]]
    log(f"  沪市命中 {len(shares)} 只")

    sz_codes = [e["code"] for e in etfs if e["market"] == "SZ"]
    log(f"取深交所 {len(sz_codes)} 只 …")
    for code in sz_codes:
        v = szse_share(code, target)
        if v is not None:
            shares[code] = v
        time.sleep(0.2)
    log(f"  份额合计 {len(shares)} 只")
    missing = [e["code"] for e in etfs if e["code"] not in shares]
    if missing:
        log(f"  warn 缺份额 {len(missing)} 只：{missing}")

    # ---- 价格
    #  今天：批量快照（2 次请求拿 100 只，不触发限频）
    #  补历史 / backfill：历史日线接口，慢跑
    quotes = {}
    if is_today and not backfill:
        syms = []
        for it in indices:
            s = tencent_symbol(meta["indices"][it["slug"]]["kline_code"])
            if s:
                syms.append(s)
        for e in etfs:
            syms.append(e["market"].lower() + e["code"])
        log(f"取腾讯快照 {len(syms)} 只 …")
        quotes = batch_quote(sorted(set(syms)))
        log(f"  {len(quotes)} 只拿到当日行情")

    def price_rows(code: str, start: str) -> dict[str, list[float]]:
        """要写的日线：当天优先用批量快照，历史接口只补快照覆盖不到的日期。

        快照走 qt.gtimg.cn（一个请求几十只，几乎不限频）；
        历史走 fqkline，那个接口有频率限制，只在补缺口时用。
        """
        out = {}
        sym = tencent_symbol(code)
        if is_today and sym and sym in quotes:
            q = quotes[sym]
            if q["date"] == target:
                out[target] = [q["o"], q["h"], q["l"], q["c"]]
            else:
                log(f"  warn {sym} 快照日期是 {q['date']}，不是 {target}")
        if target not in out or backfill:
            for d, v in history_for(code, start, target).items():
                if d <= target and d not in out:
                    out[d] = v
        return out

    start = (date.fromisoformat(target) - timedelta(days=30)).isoformat()

    # ---- ETf：先补价格，再决定要不要写份额行
    log("ETF 日线 …")
    etf_px: dict[str, dict] = {}
    need_etf = [e for e in etfs
                if not etf_series[e["code"]] or etf_series[e["code"]][-1]["date"] < target]
    for e in need_etf:
        # 传 data 风格的代码（sh.510300），tencent_symbol 才认得出市场
        etf_px[e["code"]] = price_rows(f"{e['market'].lower()}.{e['code']}", start)
    log(f"  {len(need_etf)} 只需要行情，拿到 {sum(1 for v in etf_px.values() if v)} 只")

    etf_added = 0
    new_rows: dict[str, list[dict]] = {}
    for e in etfs:
        code = e["code"]
        rows = etf_series[code]
        last = rows[-1]["date"] if rows else ""
        px = etf_px.get(code) or {}
        share_now = shares.get(code)
        if share_now is None or target <= last:
            continue
        close = (px.get(target) or [None, None, None, None])[3]
        if close is None:
            log(f"  warn {code} 没有 {target} 收盘价，跳过")
            continue
        prev = rows[-1] if rows else None
        dsh = flow = 0.0
        if prev and prev["share"] and prev["date"] in cal_pos and target in cal_pos:
            if cal_pos[target] - cal_pos[prev["date"]] == 1:
                dsh = float(share_now) - float(prev["share"])
                flow = dsh * close
        if code in ingest.SPLIT_DATES and target in ingest.SPLIT_DATES[code]:
            dsh = flow = 0.0
        row = [target, num(close, 4), num(share_now, 4), num(flow, 4), num(dsh, 4)]
        new_rows.setdefault(code, []).append(
            {"date": target, "close": close, "share": share_now, "d_share": dsh, "flow": flow})
        if not dry:
            etf_added += append_rows(DATA / "etf" / f"{code}.csv", [row])
        else:
            etf_added += 1
    log(f"ETF 追加 {etf_added} 行")

    for code, extra in new_rows.items():
        etf_series[code].extend(extra)
    series = {
        code: [{"date": r["date"], "close": float(r["close"]), "share": float(r["share"]),
                "d_share": float(r["d_share"]), "flow": float(r["flow"])}
               for r in rows if r.get("close") and r.get("share")]
        for code, rows in etf_series.items()
    }

    # ---- 指数
    log("指数日线 …")
    idx_added = 0
    for it in indices:
        slug = it["slug"]
        path = DATA / "index" / f"{slug}.csv"
        rows = read_csv(path)
        last = rows[-1]["date"] if rows else ""
        if last >= target and not backfill:
            continue
        use_hist, agg = ingest.aggregate(it["etfs"], series, ingest.MIN_HIST_OBS)
        px = price_rows(meta["indices"][slug]["kline_code"], start)
        batch = []
        for d in sorted(px):
            if d <= last or d > target:
                continue
            o, h, l, c = px[d]
            a = agg.get(d)
            batch.append([d, num(o, 4), num(h, 4), num(l, 4), num(c, 4),
                          num(a["flow"], 4) if a else None,
                          a["nin"] if a else None, a["nout"] if a else None,
                          a["n"] if a else None, num(a["scale"], 2) if a else None])
        if batch and not dry:
            idx_added += append_rows(path, batch)
        elif batch:
            idx_added += len(batch)
        if batch:
            log(f"  {slug}: +{len(batch)} 根（{batch[0][0]} → {batch[-1][0]}）")
    log(f"指数追加 {idx_added} 行")

    if dry:
        log("dry-run：不写文件")
        return EXIT_OK

    # ---- 信号全量重算（规则一改，历史也跟着对齐）
    events_all = []
    flow_days = {}
    for it in indices:
        slug = it["slug"]
        rows = read_csv(DATA / "index" / f"{slug}.csv")
        dts = [r["date"] for r in rows if r["flow"] != ""]
        flow_days[slug] = len(dts)
        # 每只成分 ETF 的当日申赎，detect_events 靠它算「前 3 大贡献」
        per_out = {}
        for c in it["etfs"]:
            p = DATA / "etf" / f"{c}.csv"
            if p.exists():
                per_out[c] = {r["date"]: float(r["flow"]) for r in read_csv(p) if r["flow"]}
        agg = {
            r["date"]: {
                "flow": float(r["flow"]),
                "nin": int(r["n_in"]),
                "nout": int(r["n_out"]),
                "n": int(r["n_etf"]),
                "scale": float(r["scale"]),
                "per": {c: v[r["date"]] for c, v in per_out.items() if r["date"] in v},
            }
            for r in rows
            if r["flow"] != ""
        }
        if len(dts) < ingest.MIN_OBS:
            continue
        evs = ingest.detect_events(dts, agg)
        ingest.annotate_events(evs)
        for e in evs:
            events_all.append({"slug": slug, **e})

    events_all.sort(key=lambda e: (e["date"], e["slug"]))
    with (DATA / "events.jsonl").open("w", encoding="utf-8") as f:
        for e in events_all:
            f.write(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n")
    log(f"信号重算：{len(events_all)} 个事件")

    # ---- meta
    per_idx = {}
    share_dates = set()
    for e in etfs:
        for r in read_csv(DATA / "etf" / f"{e['code']}.csv"):
            share_dates.add(r["date"])
    latest_trade = max(share_dates) if share_dates else target

    for it in indices:
        slug = it["slug"]
        rows = read_csv(DATA / "index" / f"{slug}.csv")
        if not rows:
            continue
        old = meta["indices"].get(slug, {})
        last = rows[-1]["date"]
        per_idx[slug] = {
            "kline_code": old.get("kline_code"),
            "kline_source": old.get("kline_source"),
            "first_date": rows[0]["date"],
            "last_date": last,
            "rows": len(rows),
            "flow_days": flow_days.get(slug, old.get("flow_days", 0)),
            "events": sum(1 for e in events_all if e["slug"] == slug),
            "etfs_used": old.get("etfs_used", []),
            "etfs_short": old.get("etfs_short", []),
        }

    stale = {slug: {"last_date": m["last_date"],
                    "lag_days": sum(1 for d in share_dates if d > m["last_date"])}
             for slug, m in per_idx.items() if m["last_date"] != latest_trade}
    meta.update({
        "generated_at": datetime.now(CST).isoformat(timespec="seconds"),
        "latest_trade_date": latest_trade,
        "indices": per_idx,
        "counts": {**meta.get("counts", {}), "events": len(events_all)},
        "coverage": {**meta.get("coverage", {}),
                     "by_index": {s: m["etfs_short"] for s, m in per_idx.items() if m["etfs_short"]}},
        "stale": {"note": "K 线比最新份额日期短的指数，通常是上游当天行情还没补",
                  "by_index": stale},
    })
    (DATA / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    log(f"最新交易日 {latest_trade}｜指数 +{idx_added} 行｜ETF +{etf_added} 行｜事件 {len(events_all)}｜滞后 {len(stale)} 个")
    if idx_added == 0 and etf_added == 0:
        log("NO_NEW_DATA")
        return EXIT_NO_DATA
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
