#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探测各数据源能否从 GitHub Actions runner 访问。

只读、不写库、不改仓库。跑在 Actions 上是为了回答一个问题：
把每日抓取搬到 GitHub 之后，这些源（尤其交易所接口）还能不能到。

用法：python3 tools/probe_sources.py
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
RESULTS = []


def record(name, ok, detail):
    RESULTS.append({"source": name, "ok": ok, "detail": detail})
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}: {detail}", flush=True)


def get(url, headers=None, timeout=30, insecure=False):
    ctx = None
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read(), r.status


def probe_egress():
    try:
        raw, _ = get("https://ipinfo.io/json", timeout=15)
        d = json.loads(raw)
        record("egress", True, f"{d.get('ip')} {d.get('city')}/{d.get('country')} {d.get('org')}")
    except Exception as e:
        record("egress", False, f"{type(e).__name__}: {e}")


def probe_sse():
    """上交所 ETF 份额快照：一次拿全市场，是沪市 47 只的主源。

    当日数据要等收盘后汇总（cron 定在 21:18），非交易日为空，
    所以往前找最近一个有数据的交易日。
    """
    last_err = None
    for delta in range(0, 8):
        d = (datetime.now(CST) - timedelta(days=delta)).strftime("%Y-%m-%d")
        url = (
            "https://query.sse.com.cn/commonQuery.do?isPagination=true&pageHelp.pageSize=10000"
            "&pageHelp.pageNo=1&pageHelp.beginPage=1&pageHelp.cacheSize=1&pageHelp.endPage=1"
            "&sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L&STAT_DATE=" + d
        )
        try:
            raw, status = get(url, {"Referer": "https://www.sse.com.cn/"}, timeout=45, insecure=True)
            rows = json.loads(raw.decode("utf-8", "replace")).get("result") or []
            if rows:
                sample = [r.get("SEC_CODE") for r in rows[:3]]
                note = "" if delta == 0 else f"（今日未出，回退 {delta} 天）"
                record("sse:snapshot", True, f"{d} HTTP {status} rows={len(rows)} sample={sample}{note}")
                return
            last_err = f"{d} HTTP {status} result 为空"
        except Exception as e:
            last_err = f"{d} {type(e).__name__}: {e}"
        time.sleep(0.5)
    record("sse:snapshot", False, f"最近 8 天都没取到：{last_err}")


def probe_szse():
    """深交所 1945 报表：逐只查，深市 10 只用它。

    连试 3 次，用来区分「偶发 reset」和「稳定被拒」。
    """
    code = "159919"
    url = (
        "http://www.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON"
        f"&CATALOGID=1945&TABKEY=tab1&PAGENO=1&txtQueryKeyAndJC={code}"
    )
    errs = []
    for attempt in range(1, 4):
        try:
            raw, status = get(url, {"Referer": "http://www.szse.cn/"}, timeout=40)
            d = json.loads(raw.decode("utf-8"))
            rows = d[0].get("data") or []
            got = [r.get("dqgm") for r in rows if code in str(r.get("sys_key", ""))]
            record("szse:1945", bool(got), f"第 {attempt} 次 HTTP {status} rows={len(rows)} dqgm={got[:1]}")
            return
        except Exception as e:
            errs.append(f"{type(e).__name__}")
            time.sleep(2)
    record("szse:1945", False, f"3 次都失败：{', '.join(errs)}")


def probe_tencent():
    try:
        raw, status = get("http://qt.gtimg.cn/q=sh000300", {"Referer": "http://finance.qq.com"}, timeout=20)
        txt = raw.decode("gbk", "replace")
        record("tencent:quote", "~" in txt, f"HTTP {status} {txt[:60]}")
    except Exception as e:
        record("tencent:quote", False, f"{type(e).__name__}: {e}")


def probe_csindex():
    """中证 index-perf：H/9 自编指数的唯一源，也用于部分指数 K 线。"""
    # 中证官网要 %Y%m%d，带横杠会返回空 data
    end = datetime.now(CST).strftime("%Y%m%d")
    start = (datetime.now(CST) - timedelta(days=15)).strftime("%Y%m%d")
    url = f"https://www.csindex.com.cn/csindex-home/perf/index-perf?indexCode=H30269&startDate={start}&endDate={end}"
    try:
        raw, status = get(url, {"Referer": "https://www.csindex.com.cn/"}, timeout=30)
        d = json.loads(raw.decode("utf-8"))
        rows = d.get("data") or []
        last = rows[-1].get("tradeDate") if rows else None
        record("csindex:perf", bool(rows), f"HTTP {status} rows={len(rows)} last={last}")
    except Exception as e:
        record("csindex:perf", False, f"{type(e).__name__}: {e}")


def probe_tencent_kline():
    """腾讯日线：baostock 的备胎（指数/ETF 前复权日线）。"""
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           "?param=sh000300,day,2026-09-01,2026-10-26,320,qfq")
    try:
        raw, status = get(url, {"Referer": "https://gu.qq.com/"}, timeout=25)
        d = json.loads(raw.decode("utf-8"))
        node = (d.get("data") or {}).get("sh000300") or {}
        rows = node.get("qfqday") or node.get("day") or []
        record("tencent:kline", bool(rows), f"HTTP {status} rows={len(rows)} last={rows[-1][0] if rows else None}")
    except Exception as e:
        record("tencent:kline", False, f"{type(e).__name__}: {e}")


def probe_sina_kline():
    """新浪日线：腾讯之外的第二个备胎。"""
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData?symbol=sh000300&scale=240&ma=no&datalen=20")
    try:
        raw, status = get(url, {"Referer": "https://finance.sina.com.cn/"}, timeout=25)
        rows = json.loads(raw.decode("utf-8", "replace"))
        last = rows[-1].get("day") if isinstance(rows, list) and rows else None
        record("sina:kline", bool(rows), f"HTTP {status} rows={len(rows) if isinstance(rows, list) else 0} last={last}")
    except Exception as e:
        record("sina:kline", False, f"{type(e).__name__}: {e}")


def probe_baostock():
    """baostock：43 个指数 + 57 只 ETF 的日线来源。"""
    try:
        import baostock as bs  # noqa: PLC0415
    except ImportError:
        record("baostock", False, "未安装（pip install baostock）")
        return
    import contextlib
    import io

    buf = io.StringIO()
    try:
        lg = None
        for attempt in range(2):
            with contextlib.redirect_stdout(buf):
                lg = bs.login()
            if lg.error_code == "0":
                break
            time.sleep(3)
        if lg.error_code != "0":
            record("baostock", False, f"login {lg.error_code} {lg.error_msg}")
            return
        with contextlib.redirect_stdout(buf):
            rs = bs.query_history_k_data_plus(
                "sh.000300", "date,open,high,low,close",
                start_date=(datetime.now(CST) - timedelta(days=15)).strftime("%Y-%m-%d"),
                end_date=datetime.now(CST).strftime("%Y-%m-%d"),
                frequency="d", adjustflag="3",
            )
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
            bs.logout()
        record("baostock", bool(rows), f"rows={len(rows)} last={rows[-1][0] if rows else None}")
    except Exception as e:
        record("baostock", False, f"{type(e).__name__}: {e}")


def probe_ths():
    """同花顺 fuyao：需要 key。没有 secret 就跳过，不算失败。"""
    key = os.environ.get("HITHINK_API_KEY")
    if not key:
        record("hithink:fuyao", True, "跳过：未配置 HITHINK_API_KEY")
        return
    try:
        raw, status = get(
            "https://fuyao.aicubes.cn/api/fund/profile/detail?thscode=510300.SH",
            {"X-api-key": key}, timeout=25,
        )
        d = json.loads(raw.decode("utf-8"))
        ok = bool((d.get("item") or [None])[0])
        record("hithink:fuyao", ok, f"HTTP {status} item={'有' if ok else '空'}")
    except Exception as e:
        record("hithink:fuyao", False, f"{type(e).__name__}: {e}")


def main():
    print(f"runner={os.environ.get('RUNNER_OS', '?')} "
          f"at={datetime.now(CST).strftime('%Y-%m-%d %H:%M')} CST\n", flush=True)
    probe_egress()
    probe_sse()
    probe_szse()
    probe_tencent()
    probe_csindex()
    probe_tencent_kline()
    probe_sina_kline()
    probe_ths()
    probe_baostock()

    bad = [r for r in RESULTS if not r["ok"]]
    print("\n===== 汇总 =====")
    for r in RESULTS:
        print(f"{'OK  ' if r['ok'] else 'FAIL'}  {r['source']:<16} {r['detail']}")
    print(f"\n失败 {len(bad)} / 共 {len(RESULTS)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
