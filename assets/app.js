/* 国家队 ETF 动态站 —— 前端渲染 */
(function () {
  "use strict";

  var UP = "#f0545e", DOWN = "#16b981", DIM = "#8b98ad", GRID = "#1f2a3a", AXIS = "#2a3748";
  var TX = "#c8d2e0";

  function fmt(x, n) {
    if (x === null || x === undefined || isNaN(x)) return "—";
    n = n === undefined ? 2 : n;
    var s = Math.abs(x).toFixed(n).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return (x < 0 ? "-" : "") + s;
  }
  function cls(x) { return x > 0 ? "up" : (x < 0 ? "down" : ""); }
  function color(x) { return x > 0 ? UP : (x < 0 ? DOWN : DIM); }

  function axisCommon() {
    return {
      axisLine: { lineStyle: { color: AXIS } },
      axisTick: { show: false },
      axisLabel: { color: DIM, fontSize: 10 },
      splitLine: { lineStyle: { color: GRID, type: "dashed" } }
    };
  }

  var dataEl = document.getElementById("page-data");
  var DATA = dataEl ? JSON.parse(dataEl.textContent) : null;

  /* ---------------- 首页 ---------------- */
  function initHome(d) {
    var c = d.chart;
    var el = document.getElementById("homeChart");
    if (el && window.echarts) {
      var ch = echarts.init(el, null, { renderer: "canvas" });
      ch.setOption({
        animation: false,
        grid: { left: 48, right: 52, top: 18, bottom: 28 },
        tooltip: {
          trigger: "axis",
          backgroundColor: "rgba(15,22,33,.95)",
          borderColor: GRID,
          textStyle: { color: TX, fontSize: 12 },
          formatter: function (ps) {
            var i = ps[0].dataIndex, out = "<b>" + c.dates[i] + "</b>";
            out += "<br/>净申赎 " + (c.flows[i] > 0 ? "+" : "") + fmt(c.flows[i]) + " 亿";
            out += "<br/>20 日累计 " + fmt(c.cum[i]) + " 亿";
            return out;
          }
        },
        xAxis: Object.assign({ type: "category", data: c.dates, boundaryGap: true }, axisCommon(),
          { axisLabel: { color: DIM, fontSize: 10, interval: Math.ceil(c.dates.length / 6) } }),
        yAxis: [
          Object.assign({ type: "value", name: "亿", nameTextStyle: { color: DIM, fontSize: 10 } }, axisCommon()),
          Object.assign({ type: "value", name: "累计", nameTextStyle: { color: DIM, fontSize: 10 }, splitLine: { show: false } }, axisCommon())
        ],
        series: [
          {
            name: "当日净申赎", type: "bar", barMaxWidth: 14,
            data: c.flows.map(function (v) {
              return { value: v, itemStyle: { color: v >= 0 ? UP : DOWN, opacity: 0.85 } };
            })
          },
          {
            name: "20日累计", type: "line", yAxisIndex: 1, data: c.cum,
            symbol: "none", lineStyle: { width: 1.6, color: "#4f8cff" }, connectNulls: true
          }
        ]
      });
      window.addEventListener("resize", function () { ch.resize(); });
    }

    // 历史记录表（客户端分页）
    var hist = d.hist, per = 20, page = 0;
    var host = document.getElementById("histTable");
    var pager = document.getElementById("histPager");
    if (!host) return;

    function render() {
      var start = page * per;
      var rows = hist.slice(start, start + per).map(function (r) {
        return "<tr><td>" + r.d + "</td>" +
          '<td class="num ' + cls(r.f) + '">' + fmt(r.f) + "</td>" +
          '<td class="num">' + r.nin + "</td>" +
          '<td class="num">' + r.nout + "</td>" +
          '<td class="c">' + (r.ev ? "● " + r.ev : "") + "</td></tr>";
      }).join("");
      host.innerHTML = '<table class="tbl"><thead><tr><th>日期</th><th class="num">净申赎(亿)</th>' +
        '<th class="num">净申购</th><th class="num">净赎回</th><th class="c">指数信号</th></tr></thead>' +
        "<tbody>" + rows + "</tbody></table>";

      var pages = Math.ceil(hist.length / per);
      pager.innerHTML = '<button id="pv"' + (page === 0 ? " disabled" : "") + ">上一页</button>" +
        '<span class="info">' + (page + 1) + " / " + pages + " 页 · 共 " + hist.length + " 个交易日</span>" +
        '<button id="nx"' + (page >= pages - 1 ? " disabled" : "") + ">下一页</button>";
      document.getElementById("pv").onclick = function () { page = Math.max(0, page - 1); render(); };
      document.getElementById("nx").onclick = function () { page = Math.min(pages - 1, page + 1); render(); };
    }
    render();
  }

  /* ---------------- 指数详情 ---------------- */
  function initDetail(d) {
    var el = document.getElementById("detailChart");
    if (!el || !window.echarts) return;
    var c = d.chart, n = c.dates.length;
    var lowMap = {}, highMap = {};
    c.dates.forEach(function (dt, i) { lowMap[dt] = c.kline[i][2]; highMap[dt] = c.kline[i][3]; });

    var maxAbs = c.events.reduce(function (m, e) { return Math.max(m, Math.abs(e.amount)); }, 1);
    var marks = c.events.map(function (e) {
      var buy = e.side > 0;
      var w = Math.abs(e.amount) / maxAbs;
      return {
        coord: [e.date, buy ? lowMap[e.date] : highMap[e.date]],
        value: e.amount,
        symbol: "triangle",
        symbolRotate: buy ? 0 : 180,
        symbolSize: 8 + Math.round(6 * w),
        itemStyle: { color: buy ? UP : DOWN, borderColor: "#0a0e15", borderWidth: 1, opacity: 0.55 + 0.45 * w },
        label: {
          show: w >= 0.35,
          position: buy ? "bottom" : "top",
          distance: 6,
          color: buy ? UP : DOWN,
          fontSize: 10,
          formatter: function (p) { return (p.value > 0 ? "+" : "") + Math.round(p.value); }
        }
      };
    });

    var annLines = c.ann.filter(function (a) {
      return c.dates.length && a >= c.dates[0] && a <= c.dates[n - 1];
    }).map(function (a) { return { xAxis: a }; });

    var zoomStart = n > 380 ? Math.max(0, 100 - (380 / n) * 100) : 0;

    var ch = echarts.init(el, null, { renderer: "canvas" });
    ch.setOption({
      animation: false,
      backgroundColor: "transparent",
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "cross", link: [{ xAxisIndex: "all" }] },
        backgroundColor: "rgba(15,22,33,.96)",
        borderColor: GRID,
        textStyle: { color: TX, fontSize: 12 },
        formatter: function (ps) {
          var i = ps[0].dataIndex;
          var k = c.kline[i];
          if (!k) return "";
          var prev = i > 0 ? c.kline[i - 1][1] : k[0];
          var chg = (k[1] / prev - 1) * 100;
          var out = "<b>" + c.dates[i] + "</b>";
          out += '<br/>开 ' + fmt(k[0]) + "　收 " + fmt(k[1]) + ' <span style="color:' + color(chg) + '">' +
            (chg > 0 ? "+" : "") + chg.toFixed(2) + "%</span>";
          out += "<br/>高 " + fmt(k[3]) + "　低 " + fmt(k[2]);
          if (c.flows[i] !== null && c.flows[i] !== undefined) {
            out += '<br/>净申赎 <span style="color:' + color(c.flows[i]) + '">' +
              (c.flows[i] > 0 ? "+" : "") + fmt(c.flows[i]) + " 亿</span>";
            out += "<br/>20 日累计 " + fmt(c.cum[i]) + " 亿";
          }
          var ev = c.events.filter(function (e) { return e.date === c.dates[i]; })[0];
          if (ev) {
            out += '<br/><span style="color:' + (ev.side > 0 ? UP : DOWN) + '">● ' +
              (ev.side > 0 ? "买点" : "卖点") + " · " + ev.days + " 个交易日合计 " + fmt(ev.total) + " 亿 · 共振 " +
              ev.reson + "/" + ev.n + "</span>";
          }
          return out;
        }
      },
      axisPointer: { link: [{ xAxisIndex: "all" }], label: { backgroundColor: "#243146" } },
      grid: [
        { left: 54, right: 16, top: 22, height: "50%" },
        { left: 54, right: 46, top: "66%", height: "17%" }
      ],
      xAxis: [
        Object.assign({ type: "category", data: c.dates, gridIndex: 0, boundaryGap: true, axisLabel: { show: false } }, axisCommon()),
        Object.assign({ type: "category", data: c.dates, gridIndex: 1, boundaryGap: true }, axisCommon(),
          { axisLabel: { color: DIM, fontSize: 10, interval: Math.ceil(n / 8) } })
      ],
      yAxis: [
        Object.assign({ scale: true, gridIndex: 0 }, axisCommon()),
        Object.assign({ scale: true, gridIndex: 1, splitNumber: 2, name: "亿", nameTextStyle: { color: DIM, fontSize: 10 } }, axisCommon()),
        Object.assign({ scale: true, gridIndex: 1, splitNumber: 2, position: "right", name: "累计", nameTextStyle: { color: DIM, fontSize: 10 }, splitLine: { show: false }, axisLabel: { color: "#38bdf8", fontSize: 9 } }, axisCommon())
      ],
      dataZoom: [
        { type: "inside", xAxisIndex: [0, 1], start: zoomStart, end: 100 },
        {
          type: "slider", xAxisIndex: [0, 1], start: zoomStart, end: 100, bottom: 6, height: 16,
          borderColor: GRID, fillerColor: "rgba(79,140,255,.12)",
          handleStyle: { color: "#4f8cff" }, textStyle: { color: DIM, fontSize: 10 }
        }
      ],
      series: [
        {
          name: "K线", type: "candlestick", data: c.kline,
          itemStyle: { color: UP, color0: DOWN, borderColor: UP, borderColor0: DOWN },
          markPoint: { data: marks, silent: true, labelLayout: { hideOverlap: true } },
          markLine: {
            silent: true, symbol: "none",
            lineStyle: { color: "#94a3b8", type: "dashed", width: 1, opacity: 0.45 },
            label: { show: false },
            data: annLines
          }
        },
        { name: "MA20", type: "line", data: c.ma20, symbol: "none", lineStyle: { width: 1, color: "#f0b90b", opacity: .85 }, connectNulls: true },
        { name: "MA60", type: "line", data: c.ma60, symbol: "none", lineStyle: { width: 1, color: "#7c8cff", opacity: .8 }, connectNulls: true },
        {
          name: "日净申赎", type: "bar", xAxisIndex: 1, yAxisIndex: 1, barMaxWidth: 8,
          data: c.flows.map(function (v) {
            if (v === null || v === undefined) return { value: null };
            return { value: v, itemStyle: { color: v >= 0 ? UP : DOWN, opacity: 0.9 } };
          })
        },
        {
          name: "20日累计", type: "line", xAxisIndex: 1, yAxisIndex: 2, data: c.cum,
          symbol: "none", lineStyle: { width: 1.4, color: "#38bdf8" }, connectNulls: true
        }
      ]
    });
    window.addEventListener("resize", function () { ch.resize(); });
  }

  /* ---------------- 分指数列表 ---------------- */
  function initIndices() {
    var grid = document.getElementById("igrid");
    if (!grid) return;
    var tabs = document.querySelectorAll("#catTabs .tab");
    var cards = Array.prototype.slice.call(grid.querySelectorAll(".icard"));
    var pager = document.getElementById("idxPager");
    var per = 12, page = 0, cat = "all";

    function filtered() {
      return cards.filter(function (c) { return cat === "all" || c.dataset.cat === cat; });
    }
    function render() {
      var list = filtered();
      var pages = Math.max(1, Math.ceil(list.length / per));
      page = Math.min(page, pages - 1);
      cards.forEach(function (c) { c.style.display = "none"; });
      list.slice(page * per, page * per + per).forEach(function (c) { c.style.display = ""; });
      pager.innerHTML = '<button id="pv"' + (page === 0 ? " disabled" : "") + ">上一页</button>" +
        '<span class="info">' + (page + 1) + " / " + pages + " 页 · " + list.length + " 个指数</span>" +
        '<button id="nx"' + (page >= pages - 1 ? " disabled" : "") + ">下一页</button>";
      document.getElementById("pv").onclick = function () { page = Math.max(0, page - 1); render(); };
      document.getElementById("nx").onclick = function () { page = Math.min(pages - 1, page + 1); render(); };
    }
    Array.prototype.forEach.call(tabs, function (t) {
      t.onclick = function () {
        Array.prototype.forEach.call(tabs, function (x) { x.classList.remove("active"); });
        t.classList.add("active");
        cat = t.dataset.cat;
        page = 0;
        render();
      };
    });
    render();
  }

  if (DATA && DATA.type === "home") initHome(DATA);
  if (DATA && DATA.type === "detail") initDetail(DATA);
  initIndices();
})();
