#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用上交所归档重写某几天的沪市份额。

起因：etf_flow_monitor 在 21:18 拿不到上交所当日快照时会回退到同花顺
（fund_scale / unit_nav 反推），那个数值来自季报口径的规模，比交易所的
实际总份额偏低，而且越到后面差得越多。2026-10-21~23 三天 47 只沪市 ETF
全是 ths 口径，逐日偏低最多 10%。

交易所归档里这几天的数据是有的，只是当天 21:18 还没发布。所以这里按日期
重新拉一遍、覆盖库里的值，source 标成 sse_resync 便于以后追查。

用法：
    python3 tools/resync_shares.py --from 2026-10-21 --to 2026-10-23 [--db PATH] [--dry-run]
之后跑 `python3 ingest.py` 重新导出 data/。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import ssl
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
DEFAULT_DB = Path.home() / ".hermes/data/astock/market.sqlite"


def sse_snapshot(day: str, tries=3) -> dict[str, float]:
    url = ("https://query.sse.com.cn/commonQuery.do?isPagination=true&pageHelp.pageSize=10000"
           "&pageHelp.pageNo=1&pageHelp.beginPage=1&pageHelp.cacheSize=1&pageHelp.endPage=1"
           "&sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L&STAT_DATE=" + day)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                       "Referer": "https://www.sse.com.cn/"})
            raw = urllib.request.urlopen(req, timeout=45, context=ctx).read()
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
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 + 2 * i)
    raise last  # type: ignore[misc]


def daterange(a: str, b: str):
    d0, d1 = date.fromisoformat(a), date.fromisoformat(b)
    while d0 <= d1:
        yield d0.isoformat()
        d0 += timedelta(days=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--to", dest="end", required=True)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    etfs = json.loads((ROOT / "data/etfs.json").read_text(encoding="utf-8"))["etfs"]
    sh = [e["code"] for e in etfs if e["market"] == "SH"]
    conn = sqlite3.connect(args.db)
    total = 0
    for day in daterange(args.start, args.end):
        if date.fromisoformat(day).weekday() >= 5:
            continue
        try:
            snap = sse_snapshot(day)
        except Exception as e:  # noqa: BLE001
            print(f"{day}: 上交所取不到（{type(e).__name__}），跳过")
            continue
        if not snap:
            print(f"{day}: 上交所无数据（可能还没发布），跳过")
            continue
        changed = 0
        for code in sh:
            v = snap.get(code)
            if v is None:
                continue
            cur = conn.execute(
                "SELECT share_yi FROM fund_shares WHERE code=? AND date=?", (code, day)).fetchone()
            if cur is None:
                if not args.dry_run:
                    conn.execute(
                        "INSERT INTO fund_shares(code,date,share_yi,source,updated_at) "
                        "VALUES(?,?,?,?,datetime('now'))", (code, day, v, "sse_resync"))
                changed += 1
            elif abs(cur[0] - v) > 1e-6:
                if not args.dry_run:
                    conn.execute(
                        "UPDATE fund_shares SET share_yi=?, source='sse_resync', updated_at=datetime('now') "
                        "WHERE code=? AND date=?", (v, code, day))
                changed += 1
        total += changed
        print(f"{day}: 沪市 {len(snap)} 只，改写 {changed} 行")
        time.sleep(1.0)

    if not args.dry_run:
        conn.commit()
    conn.close()
    print(f"合计改写 {total} 行{'（dry-run 未写库）' if args.dry_run else '，接着跑 python3 ingest.py'}")


if __name__ == "__main__":
    sys.exit(main())
