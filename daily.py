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
GAP_LOOKBACK_DAYS = 180   # 默认只补最近这么多天的缺口，--deep 才从头扫


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
    """上交所当日全市场 ETF 份额（亿份）。

    服务端会把 pageSize 压到 2000：拿到的条数必须等于它自报的 total，
    且每条都是请求日的日期，否则是残缺/错日的快照，宁可报错也不能写进 data/。
    """
    url = ("https://query.sse.com.cn/commonQuery.do?isPagination=true&pageHelp.pageSize=10000"
           "&pageHelp.pageNo=1&pageHelp.beginPage=1&pageHelp.cacheSize=1&pageHelp.endPage=1"
           "&sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L&STAT_DATE=" + day)
    raw = get(url, {"Referer": "https://www.sse.com.cn/"}, timeout=45, insecure=True)
    payload = json.loads(raw.decode("utf-8", "replace"))
    rows = payload.get("result") or []
    total = (payload.get("pageHelp") or {}).get("total")
    if isinstance(total, int) and total != len(rows):
        raise RuntimeError(f"上交所返回 {len(rows)} 条，自报总数 {total}，快照不完整")
    out = {}
    for r in rows:
        if str(r.get("STAT_DATE", "")).strip() != day:
            raise RuntimeError(f"上交所返回了 {r.get('STAT_DATE')} 的数据，不是 {day}")
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

    dqgm 是 T-1 数据，页面会标注日期；标注不是 day 就返回 None。
    这个标注在 T 日晚间到 T+1 凌晨之间才从 T-1 翻到 T，不能凭 subname 判断。
    注意：这个接口只给「最新」一天的规模，补不了历史——所以历史深市份额只能靠导出。
    """
    url = ("http://www.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON"
           f"&CATALOGID=1945&TABKEY=tab1&PAGENO=1&txtQueryKeyAndJC={code}")
    for _attempt in range(2):
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
            # 页面有日期但对不上 = 这天的历史值拿不到，重试也没用；
            # 只有日期读不出来（页面刚翻页）才值得再试一次
            if label:
                return None
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
    # 9\d{5} 必须先判：930632 这种也是 6 位纯数字，落到下一条会被当成沪市代码，
    # 拼出 sh930632 这种不存在的符号，然后腾讯/新浪静默返回空，中证那条路永远走不到。
    if re.fullmatch(r"[Hh]\d{6}|9\d{5}", code):
        return None      # 中证自编指数，腾讯没有
    if re.fullmatch(r"\d{6}", code):
        return ("sz" if code.startswith("399") else "sh") + code
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


def official_calendar(start: str, end: str) -> set[str]:
    """深交所官方交易日历：按月取，逐日给 is_open，比用沪深300 推断靠谱。

    任何一个月拿不到就整体返回空集合，别拿半份日历去判缺口（会把缺口判成没有）。
    """
    out: set[str] = set()
    cur = date.fromisoformat(start).replace(day=1)
    stop = date.fromisoformat(end)
    while cur <= stop:
        url = ("https://www.szse.cn/api/report/exchange/onepersistenthour/monthList"
               f"?month={cur.year}-{cur.month}")
        try:
            raw = get(url, {"Referer": "https://www.szse.cn/"}, timeout=25)
            rows = json.loads(raw.decode("utf-8", "replace")).get("data") or []
            if not rows:
                raise RuntimeError("空结果")
            for r in rows:
                if str(r.get("jybz")) == "1" and r.get("jyrq"):
                    out.add(str(r["jyrq"])[:10])
        except Exception as e:  # noqa: BLE001
            log(f"  warn 官方日历 {cur.year}-{cur.month}: {type(e).__name__}: {e}")
            return set()
        cur = date(cur.year + (cur.month == 12), (cur.month % 12) + 1, 1)
    return out


def trading_ref(start: str, end: str, deep: bool = False) -> dict[str, list[float]]:
    """沪深300 日线拼成交易日历。

    日常只补近 180 天：先问新浪（一个请求、最近 320 根，够用且不怕腾讯限频），
    拿不到再按 600 天分段问腾讯；补全历史时反过来，腾讯拿不到的段回落到中证。
    """
    if not deep:
        try:
            rows = {d: v for d, v in sina_history("sh000300").items() if d >= start}
            if len(rows) > 100 and max(rows) >= (date.fromisoformat(end)
                                                 - timedelta(days=10)).isoformat():
                return rows
        except Exception as e:  # noqa: BLE001
            log(f"  warn 交易日历(新浪): {type(e).__name__}")
    out: dict[str, list[float]] = {}
    cur = date.fromisoformat(start)
    stop = date.fromisoformat(end)
    streak = 0
    while cur <= stop:
        chunk_end = min(cur + timedelta(days=598), stop)
        got: dict[str, list[float]] = {}
        try:
            got = tencent_history("sh000300", cur.isoformat(), chunk_end.isoformat())
        except Exception as e:  # noqa: BLE001
            log(f"  warn 交易日历(腾讯) {cur}~{chunk_end}: {type(e).__name__}")
        if not got:
            try:
                got = {d: v for d, v in sina_history("sh000300").items()
                       if cur.isoformat() <= d <= chunk_end.isoformat()}
            except Exception as e:  # noqa: BLE001
                log(f"  warn 交易日历(新浪) {cur}~{chunk_end}: {type(e).__name__}")
        if not got and deep:
            try:
                got = csindex_history("000300", cur.isoformat(), chunk_end.isoformat())
                time.sleep(CSINDEX_GAP)
            except Exception as e:  # noqa: BLE001
                log(f"  warn 交易日历(中证) {cur}~{chunk_end}: {type(e).__name__}")
        out.update(got)
        streak = 0 if got else streak + 1
        if streak >= 2:      # 连续两段拿不到就别耗时间了，后面用序列日期补
            log("  warn 交易日历取不到，剩下的用序列日期补")
            break
        time.sleep(TENCENT_GAP)
        cur = chunk_end + timedelta(days=1)
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
    """历史日线：带前缀/裸 6 位走腾讯→新浪→中证，自编指数直接走中证。"""
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
            rows = sina_history(sym)
            # 新浪不看日期区间、只给最近 320 根：回来的东西离 end 太远就是错标的，
            # 当成拿不到（曾经把 sh000813 返回成 2016 年的数据）
            if rows and max(rows) >= (date.fromisoformat(end) - timedelta(days=45)).isoformat():
                return rows
        except Exception as e:  # noqa: BLE001
            log(f"  warn 新浪历史 {sym}: {type(e).__name__}")
    code_cs = code.split(".")[-1]
    try:
        rows = csindex_history(code_cs, start, end)
        if rows:
            return rows
    except Exception as e:  # noqa: BLE001
        log(f"  warn 中证 {code_cs}: {type(e).__name__}")
    finally:
        time.sleep(CSINDEX_GAP)
    return {}


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


def fmt(x, n=4) -> str:
    """CSV 里的数字：跟 ingest.py 的 num()+str() 保持一样（0.0 不写成 0）。"""
    if x is None:
        return ""
    return str(round(float(x), n))


def write_series(path: Path, cols: list[str], rows: list[dict]) -> None:
    """整份重写：按日期排序、同日只留一行。补中间缺口后只能这样写。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(",".join(cols) + "\n")
        for r in rows:
            f.write(",".join(str(r.get(c, "")) for c in cols) + "\n")


def recompute_flows(rows: list[dict], targets: set[str], cal_pos: dict[str, int], splits=()) -> None:
    """只重算受影响的几行：补进去的那些日期，以及紧随其后的那一行。

    补中间缺口时，原来跟在洞后面的那一行 Δ 会被算成 0（不相邻），必须重算；
    再往后的行predecessor 没变，重算只会让末位小数白白抖动，所以不碰。
    """
    pos = {r["date"]: i for i, r in enumerate(rows)}
    for d in sorted(targets):
        i = pos.get(d)
        if i is None:
            continue
        r = rows[i]
        prev = rows[i - 1] if i else None
        dsh = flow = 0.0
        if prev is not None and r.get("share") and prev.get("share") and r.get("close"):
            if cal_pos.get(r["date"], -99) - cal_pos.get(prev["date"], -99) == 1:
                dsh = float(r["share"]) - float(prev["share"])
                flow = dsh * float(r["close"])
        if r["date"] in splits:
            dsh = flow = 0.0
        r["d_share"] = fmt(dsh)
        r["flow"] = fmt(flow)


# ------------------------------------------------------------------ 主流程

def main(argv):
    dry = "--dry-run" in argv
    deep = "--deep" in argv or "--backfill" in argv
    audit = "--audit" in argv
    backfill = deep
    want = None
    for i, a in enumerate(argv):
        if a == "--date" and i + 1 < len(argv):
            want = argv[i + 1]

    # 不带 --date 时不再「按最后一个份额日期往下走」：那只补得到尾部，
    # 而且只要某一只 ETF 比别人靠前，落在后面的序列就被永远跳过。
    # 现在改为逐序列算缺口（见下面的 missing_days），尾部与中间一起补。

    meta = json.loads((DATA / "meta.json").read_text(encoding="utf-8"))
    indices = json.loads((DATA / "indices.json").read_text(encoding="utf-8"))["indices"]
    etfs = json.loads((DATA / "etfs.json").read_text(encoding="utf-8"))["etfs"]

    today = datetime.now(CST).strftime("%Y-%m-%d")
    target = want or today
    is_today = target >= today
    log(f"目标日期 {target}（{'今天' if is_today else '补历史'}{'，backfill' if backfill else ''}{'，dry-run' if dry else ''}）")

    # ---- 已有序列
    etf_series: dict[str, list[dict]] = {
        e["code"]: read_csv(DATA / "etf" / f"{e['code']}.csv") for e in etfs
    }
    if not any(etf_series.values()):
        log("data/etf 下没有数据，先用 ingest.py 初始化")
        return EXIT_FAIL
    idx_series: dict[str, list[dict]] = {
        it["slug"]: read_csv(DATA / "index" / f"{it['slug']}.csv") for it in indices
    }

    # ---- 交易日历
    # 用沪深300 的日线当 A 股交易日基准：它是每个交易日都有的一手数据。
    # 不能拿「各序列日期的并集」当日历——港股跟 A 股的节假日不一样，
    # 并集会把 10-06 这种港股开市、A 股休市的日子算成所有 A 股序列的缺口。
    first_dates = [rows[0]["date"] for rows in etf_series.values() if rows]
    ref_start = min(first_dates) if deep and first_dates else (
        date.fromisoformat(target) - timedelta(days=200)).isoformat()
    if deep:
        # 补全历史要几百个月，官方月历太慢：退回沪深300 日线（腾讯分段→中证）
        cal_set = set(trading_ref(ref_start, target, deep=True))
    else:
        cal_set = official_calendar(ref_start, target)
        if len(cal_set) < 30:
            log("  warn 官方交易日历拿不到，退回沪深300 日线推断")
            cal_set = set(trading_ref(ref_start, target))
    # 各序列自己的日期按「出现频率」并进来：一半以上的序列都有的日子一定是交易日，
    # 只有个别港股序列才有的日子（港股开市、A 股休市）不会混进 A 股日历。
    freq: dict[str, int] = {}
    for rows in etf_series.values():
        for d in {r["date"] for r in rows}:
            freq[d] = freq.get(d, 0) + 1
    for rows in idx_series.values():
        for d in {r["date"] for r in rows}:
            freq[d] = freq.get(d, 0) + 1
    quorum = 3    # 有 3 条以上序列同时有这天，就当它是交易日（港股独有的日子只有 2 条）
    cal_set |= {d for d, c in freq.items() if c >= quorum}
    if len(cal_set) <= 30:
        log(f"  warn 交易日历只有 {len(cal_set)} 天，缺口判断可能不准")
    cal = sorted(d for d in cal_set if d <= target
                 and date.fromisoformat(d).weekday() < 5)
    # 目标日通常还没写进任何 CSV，但它是交易日；日历必须带上它，
    # 否则「上一行是不是紧邻的前一交易日」永远不成立，Δ份额会全算成 0。
    if target not in cal and date.fromisoformat(target).weekday() < 5:
        cal = sorted(cal + [target])
    cal_pos = {d: i for i, d in enumerate(cal)}

    # 默认只补最近 GAP_LOOKBACK_DAYS 天；--deep / --backfill 从每条序列的第一行扫起
    horizon = cal[0] if deep else (date.fromisoformat(target)
                                   - timedelta(days=GAP_LOOKBACK_DAYS)).isoformat()

    def missing_days(rows: list[dict]) -> list[str]:
        """这条序列在 [第一行, target] 之间缺的交易日。"""
        have = {r["date"] for r in rows}
        lo = max(rows[0]["date"] if rows else horizon, horizon)
        return [d for d in cal if lo <= d <= target and d not in have]

    if audit:
        gaps: dict[str, list[str]] = {
            f"etf/{code}": missing_days(rows) for code, rows in etf_series.items()
        }
        gaps |= {f"index/{slug}": missing_days(rows) for slug, rows in idx_series.items()}
        bad = {k: v for k, v in gaps.items() if v}
        for k, v in list(bad.items())[:40]:
            log(f"  {k}: 缺 {len(v)} 天，最近 {v[-3:]}")
        log(f"审计：{len(bad)}/{len(gaps)} 条序列有缺口"
            f"（窗口 {horizon} → {target}，加 --deep 从第一行扫起）")
        return EXIT_OK

    # ---- 份额：按日缓存，同一天只打一次上交所
    sse_cache: dict[str, dict] = {}

    def sse_of(day: str) -> dict[str, float]:
        if day not in sse_cache:
            sse_cache[day] = sse_snapshot(day)
        return sse_cache[day]

    log("取上交所快照 …")
    try:
        snap = sse_of(target)
    except Exception as e:  # noqa: BLE001
        log(f"  上交所失败：{type(e).__name__}: {e}")
        return EXIT_FAIL
    if not snap:
        log(f"  上交所 {target} 快照为空（当天数据通常 21:18 后发布）")
        if want is not None:
            return EXIT_NO_DATA
    log(f"  沪市 {target} 命中 {sum(1 for e in etfs if e['market'] == 'SH' and e['code'] in snap)} 只")
    if not snap and want is None and target in cal:
        # 目标日还没发布（常见于盘前跑）：把它从日历里摘掉，
        # 这样缺口只算到「有数据的最晚一天」，不会为每个序列白跑一次历史行情。
        cal = [d for d in cal if d != target]
        cal_pos = {d: i for i, d in enumerate(cal)}
        log(f"  {target} 还没有份额数据，本次先跳过这几天之后的部分")

    # ---- 价格
    #  目标日是今天：先用批量快照（两次请求拿全市场，几乎不限频）；
    #  补缺 / 补历史：走历史日线接口，按需要的区间拉一次。
    quotes: dict[str, dict] = {}
    if is_today and snap:
        # 只有目标日真的出了份额才值得拿快照：没份额就写不了行，快照纯属白跑
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

    def px_range(code: str, lo: str) -> dict[str, list[float]]:
        """[lo-10d, target] 的日线；目标日优先用批量快照，其余走历史接口。

        返回整段而不是只返回缺失日：调用方只写缺失的那几天，
        但「整段都没拿到」和「只有节假日没有」要能区分开。
        """
        out: dict[str, list[float]] = {}
        sym = tencent_symbol(code)
        if is_today and sym:
            q = quotes.get(sym)
            if q and q["date"] == target:
                out[target] = [q["o"], q["h"], q["l"], q["c"]]
        if lo == target and target in out:
            return out
        start_hist = (date.fromisoformat(lo) - timedelta(days=10)).isoformat()
        for d, v in history_for(code, start_hist, target).items():
            if d <= target and d not in out:
                out[d] = v
        return out

    # ---- ETF：逐条序列补缺（尾部与中间一视同仁）
    log("ETF 日线 …")
    etf_added = 0
    etf_unsolved: dict[str, list[str]] = {}
    for e in etfs:
        code, market = e["code"], e["market"]
        rows = etf_series[code]
        miss = missing_days(rows)
        if not miss:
            continue
        px = px_range(f"{market.lower()}.{code}", miss[0])
        no_share: list[str] = []
        inserted: list[str] = []
        for d in miss:
            close = (px.get(d) or [None, None, None, None])[3]
            if close is None:
                continue
            if market == "SH":
                share = sse_of(d).get(code)
            else:
                share = szse_share(code, d)
                time.sleep(0.2)
            if share is None:
                no_share.append(d)
                continue
            rows.append({"date": d, "close": fmt(close), "share": fmt(share),
                         "flow": "", "d_share": ""})
            inserted.append(d)
        added = len(inserted)
        if added:
            rows.sort(key=lambda r: r["date"])
            pos = {r["date"]: i for i, r in enumerate(rows)}
            targets = set(inserted)
            for d in inserted:      # 紧跟其后的那一行 Δ 也被打乱了，一起重算
                i = pos[d] + 1
                if i < len(rows):
                    targets.add(rows[i]["date"])
            recompute_flows(rows, targets, cal_pos, ingest.SPLIT_DATES.get(code, ()))
            etf_added += added
            if not dry:
                write_series(DATA / "etf" / f"{code}.csv", ETF_COLS, rows)
            tail = ""
            if no_share:
                tail = f"（有 {len(no_share)} 天有行情没份额）"
            log(f"  {code}: +{added} 行{tail}")
        if not px:
            log(f"  warn {code}: {miss[0]}~{target} 行情一条没拿到")
        if no_share:
            etf_unsolved[code] = no_share
    log(f"ETF 追加 {etf_added} 行")

    series = {
        code: [{"date": r["date"], "close": float(r["close"]), "share": float(r["share"]),
                "d_share": float(r["d_share"] or 0), "flow": float(r["flow"] or 0)}
               for r in rows if r.get("close") and r.get("share")]
        for code, rows in etf_series.items()
    }

    # ---- 指数
    log("指数日线 …")
    idx_added = 0
    idx_unsolved: dict[str, list[str]] = {}
    for it in indices:
        slug = it["slug"]
        rows = idx_series[slug]
        miss = missing_days(rows)
        if not miss:
            continue
        px = px_range(meta["indices"][slug]["kline_code"], miss[0])
        use_hist, agg = ingest.aggregate(it["etfs"], series, ingest.MIN_HIST_OBS)
        if not agg:
            use_hist, agg = ingest.aggregate(it["etfs"], series, 0)
        added = 0
        for d in miss:
            v = px.get(d)
            if not v:
                continue
            o, h, l, c = v
            a = agg.get(d)
            rows.append({
                "date": d, "o": fmt(o), "h": fmt(h), "l": fmt(l), "c": fmt(c),
                "flow": fmt(a["flow"]) if a else "",
                "n_in": str(a["nin"]) if a else "",
                "n_out": str(a["nout"]) if a else "",
                "n_etf": str(a["n"]) if a else "",
                "scale": fmt(a["scale"], 2) if a else "",
            })
            added += 1
        if added:
            rows.sort(key=lambda r: r["date"])
            idx_added += added
            if not dry:
                write_series(DATA / "index" / f"{slug}.csv", INDEX_COLS, rows)
            log(f"  {slug}: +{added} 根（{miss[0]} → {miss[-1]}）")
        if not px:
            idx_unsolved[slug] = miss
            log(f"  warn {slug}: {miss[0]}~{target} 行情一条没拿到")
    log(f"指数追加 {idx_added} 行")

    # ---- 收尾核对：真正的缺口（有行情、缺份额/接口失败）单独列出来
    if etf_unsolved or idx_unsolved:
        log(f"  仍有缺口：ETF {len(etf_unsolved)} 条、指数 {len(idx_unsolved)} 条（下次再试）")
        for code, days in list(etf_unsolved.items())[:8]:
            log(f"    etf/{code}: 有行情没份额 {len(days)} 天 {days[:3]}")
        for slug, days in list(idx_unsolved.items())[:8]:
            log(f"    index/{slug}: 行情没拿到 {len(days)} 天 {days[:3]}")
    else:
        log("  缺口核对：窗口内没有残余缺口")

    if dry:
        log("dry-run：不写文件")
        return EXIT_OK

    # 一行没加就直接退出：事件重算和 meta 里的 generated_at 都会变，
    # 白白给仓库添一个「数据更新」提交，线上也会重建一次。
    if etf_added == 0 and idx_added == 0:
        log("NO_NEW_DATA")
        return EXIT_NO_DATA

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
    unresolved = {f"etf/{c}": days for c, days in etf_unsolved.items()}
    unresolved |= {f"index/{s}": days for s, days in idx_unsolved.items()}
    meta.update({
        "generated_at": datetime.now(CST).isoformat(timespec="seconds"),
        "latest_trade_date": latest_trade,
        "indices": per_idx,
        "counts": {**meta.get("counts", {}), "events": len(events_all)},
        "coverage": {**meta.get("coverage", {}),
                     "by_index": {s: m["etfs_short"] for s, m in per_idx.items() if m["etfs_short"]}},
        "stale": {"note": "K 线比最新份额日期短的指数，通常是上游当天行情还没补",
                  "by_index": stale},
        "unresolved": {"note": "有行情但源头没份额/接口失败而没补齐的日期，下次运行会再试",
                       "by_series": unresolved},
    })
    (DATA / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    log(f"最新交易日 {latest_trade}｜指数 +{idx_added} 行｜ETF +{etf_added} 行｜事件 {len(events_all)}｜滞后 {len(stale)} 个")
    if idx_added == 0 and etf_added == 0:
        log("NO_NEW_DATA")
        return EXIT_NO_DATA
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
