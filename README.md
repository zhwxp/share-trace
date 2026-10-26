# ShareTrace — 国家队 ETF 份额足迹

名字取自「份额的足迹」：把 57 只「国家队曾出现在前十大持有人」的 ETF 的日度份额变化折算成金额，画到 43 个标的指数的 K 线上，标出买卖点。

- 首页：最新交易日的合计净申赎、异动榜、分指数排行、历史信号、逐日历史（客户端分页）
- 分指数：43 个指数，点开是「K 线 + 买卖点 + 日净申赎柱 + 20 日累计线」+ 信号明细表
- 纯静态：HTML + 本地 ECharts，无后端、无外链、无构建依赖

## 目录

```
build.py             生成器（只依赖 Python 标准库）
config/indices.json  43 个指数 → 57 只 ETF 映射、K 线代码
config/etfs.json     57 只 ETF 元数据（名称 / 档位 / 市场 / 所属指数）
assets/              style.css、app.js、echarts.min.js
site/                生成产物，直接部署这个目录
```

## 生成

```bash
python3 build.py           # 读 market.sqlite，重建 site/
python3 build.py --stats   # 只打印每个指数的信号数量，不写文件
```

数据源是本地 `~/.hermes/data/astock/market.sqlite`（`bars` / `fund_shares` / `calendar`），
可用环境变量 `ASTOCK_DB` 覆盖。份额与行情先由 `etf_flow_monitor.py`、`astock sync` 更新进库，
再跑 `build.py` 出站。

## 信号规则

日净申赎 `F = Σ(Δ份额 × 当日收盘价)`，按标的指数加总。

1. `|F| ≥ max(2 亿, 8% × 该指数 ETF 合计规模)`，占比门槛封顶 100 亿；
2. 成分 ETF ≥ 2 只时，同向只数 ≥ `max(2, 60%)`；
3. 5 个交易日内同向连续触发合并为一个事件，标在 `|F|` 最大那天；
4. 每个事件统计「后 20 个交易日」指数涨跌：买点看涨、卖点看跌，正数=信号有效。

已剔除份额折算日（510310 2024-09-20、512100 2022-09-02、560010 2022-09-13）。
深市 10 只 ETF 没有免费的日度份额历史，2026-10-21 起才有，只进当日数据、不进历史序列。

## 部署

站点是纯静态，把 `site/` 整个目录传上去即可。

**nginx**

```nginx
server {
    listen 80;
    server_name sharetrace.example.com;
    root /var/www/sharetrace/site;
    index index.html;
    location / { try_files $uri $uri/ =404; }
}
```

**GitHub Pages / Cloudflare Pages / 对象存储**：把 `site/` 作为发布目录（Pages 需要把产物放根目录或配置 publish dir 为 `site`）。

本地预览：

```bash
cd site && python3 -m http.server 8099
```

## 远端

```bash
git remote add origin <你的仓库地址>
git push -u origin main
```

## 口径与已知限制

- 份额是基金总份额（一级市场申赎），不是成交额——成交额法实测精确率只有 4%。
- 「份额大增」不等于「国家队买入」：2022-12 510300 份额 +82% 时汇金持仓纹丝未动。
- 「大额净赎回」也不等于清仓：2026-01 的 13 个交易日 -7722 亿，同期沪深300 走平，更像换仓／转托管。
- 沪市份额来自上交所归档接口（2012-01-04 起），覆盖度逐年爬升，跨年比较要按当年在册只数归一。
- 信号方向对、强度不够当择时用。

口径细节见站内「口径说明」页，以及 `skills/custom/etf-flow-tracking/`。
