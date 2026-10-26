/* K 线 + 申赎柱 + 20 日累计 + 买卖点。详情页和分指数页共用这一份。
   用法：KLine.mount(el, payload, { small })  payload = /data/index/<slug>.json */
(function () {
  'use strict';

  // 红涨绿跌只出现在 K 线里；其余一律 ink / 灰
  var UP = '#c9473d', DOWN = '#3f9c6d', GRID = '#26262a', AXIS = '#2c2c31', MUTED = '#9a9ca2';
  var TX = '#ffffff', FLOW = '#f2f2f3', FLOW_NEG = '#6e7076', CUM = '#cf3f4e';
  var PANEL = '#1b1b1f', EDGE = '#2e2e34';

  function fmt(x, digits) {
    if (x === null || x === undefined || isNaN(x)) return '—';
    digits = digits === undefined ? 2 : digits;
    return Math.abs(x).toFixed(digits).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }
  function color(x) { return x > 0 ? UP : x < 0 ? DOWN : MUTED; }

  function derive(c) {
    var closes = c.kline.map(function (k) { return k[1]; });
    function ma(win) {
      var out = [], sum = 0;
      for (var i = 0; i < closes.length; i++) {
        sum += closes[i];
        if (i >= win) sum -= closes[i - win];
        out.push(i + 1 < win ? null : Math.round((sum / win) * 1000) / 1000);
      }
      return out;
    }
    c.ma20 = ma(20);
    c.ma60 = ma(60);
    c.cum = [];
    var queue = [], prevCum = null;
    c.flows.forEach(function (f) {
      if (f === null || f === undefined) { c.cum.push(prevCum); return; }
      queue.push(f);
      if (queue.length > 20) queue.shift();
      prevCum = Math.round(queue.reduce(function (a, b) { return a + b; }, 0) * 100) / 100;
      c.cum.push(prevCum);
    });
    return c;
  }

  function mount(el, payload, opts) {
    opts = opts || {};
    var c = derive(payload);
    var n = c.dates.length;
    var lowMap = {}, highMap = {};
    c.dates.forEach(function (d, i) { lowMap[d] = c.kline[i][2]; highMap[d] = c.kline[i][3]; });

    var axis = {
      axisLine: { lineStyle: { color: AXIS } },
      axisTick: { show: false },
      axisLabel: { color: MUTED, fontSize: 11 },
      splitLine: { lineStyle: { color: GRID } }
    };

    var maxAbs = c.events.reduce(function (m, e) { return Math.max(m, Math.abs(e.amount)); }, 1);
    var marks = c.events.map(function (e) {
      var buy = e.side > 0;
      var w = Math.abs(e.amount) / maxAbs;
      return {
        coord: [e.date, buy ? lowMap[e.date] : highMap[e.date]],
        value: e.amount,
        symbol: 'triangle',
        symbolRotate: buy ? 0 : 180,
        symbolSize: 8 + Math.round(6 * w),
        itemStyle: { color: buy ? UP : DOWN, borderColor: '#171719', borderWidth: 1, opacity: 0.65 + 0.35 * w },
        label: {
          show: w >= 0.35,
          position: buy ? 'bottom' : 'top',
          distance: 6,
          color: buy ? '#e8897b' : '#6fc99b',
          fontSize: 11,
          formatter: function (p) { return (p.value > 0 ? '+' : '') + Math.round(p.value); }
        }
      };
    });

    var annLines = (c.ann || [])
      .filter(function (a) { return n && a >= c.dates[0] && a <= c.dates[n - 1]; })
      .map(function (a) { return { xAxis: a }; });

    var zoomStart = n > 380 ? Math.max(0, 100 - (380 / n) * 100) : 0;
    var H = el.clientHeight || 520;
    var small = opts.small === undefined ? H < 420 : !!opts.small;
    var padL = small ? 34 : 56;

    var ch = echarts.getInstanceByDom(el) || echarts.init(el, null, { renderer: 'canvas' });
    ch.setOption({
      animation: false,
      backgroundColor: 'transparent',
      textStyle: { fontFamily: 'inherit' },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross', link: [{ xAxisIndex: 'all' }] },
        backgroundColor: PANEL,
        borderColor: EDGE,
        padding: [8, 10],
        textStyle: { color: TX, fontSize: 12 },
        formatter: function (ps) {
          var i = ps[0].dataIndex;
          var k = c.kline[i];
          if (!k) return '';
          var prev = i > 0 ? c.kline[i - 1][1] : k[0];
          var chg = (k[1] / prev - 1) * 100;
          var out = '<b>' + c.dates[i] + '</b>';
          out += '<br/>开 ' + fmt(k[0]) + '　收 ' + fmt(k[1]) +
            ' <span style="color:' + color(chg) + '">' + (chg > 0 ? '+' : '') + chg.toFixed(2) + '%</span>';
          out += '<br/>高 ' + fmt(k[3]) + '　低 ' + fmt(k[2]);
          if (c.flows[i] !== null && c.flows[i] !== undefined) {
            out += '<br/>净申赎 <span style="color:' + color(c.flows[i]) + '">' +
              (c.flows[i] > 0 ? '+' : '-') + fmt(c.flows[i]) + ' 亿</span>';
            out += '<br/>20 日累计 ' + fmt(c.cum[i]) + ' 亿';
          }
          var ev = c.events.filter(function (e) { return e.date === c.dates[i]; })[0];
          if (ev) {
            out += '<br/><span style="color:' + (ev.side > 0 ? UP : DOWN) + '">● ' +
              (ev.side > 0 ? '买点' : '卖点') + ' · ' + ev.days + ' 个交易日合计 ' + fmt(ev.total) +
              ' 亿 · 共振 ' + ev.reson + '/' + ev.n + '</span>';
          }
          return out;
        }
      },
      axisPointer: { link: [{ xAxisIndex: 'all' }], label: { backgroundColor: '#2c2c31', color: TX } },
      grid: [
        { left: padL, right: small ? 10 : 16, top: small ? 8 : 16, height: small ? '44%' : '52%' },
        { left: padL, right: small ? 40 : 48, top: small ? '56%' : '70%', height: small ? '16%' : '15%' }
      ],
      xAxis: [
        Object.assign({ type: 'category', data: c.dates, gridIndex: 0, boundaryGap: true, axisLabel: { show: false } }, axis),
        Object.assign({ type: 'category', data: c.dates, gridIndex: 1, boundaryGap: true }, axis,
          { axisLabel: { color: MUTED, fontSize: 11, hideOverlap: true, interval: Math.max(1, Math.ceil(n / (small ? 4 : 7))) } })
      ],
      yAxis: [
        Object.assign({ scale: true, gridIndex: 0 }, axis),
        Object.assign({ scale: true, gridIndex: 1, splitNumber: 2 }, axis),
        Object.assign({
          scale: true, gridIndex: 1, splitNumber: 2, position: 'right',
          splitLine: { show: false }, axisLabel: { color: MUTED, fontSize: 11 }
        }, axis)
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1], start: zoomStart, end: 100 },
        {
          type: 'slider', xAxisIndex: [0, 1], start: zoomStart, end: 100, bottom: small ? 2 : 8, height: small ? 12 : 16,
          borderColor: GRID, fillerColor: 'rgba(242,242,243,.06)',
          dataBackground: { lineStyle: { color: '#55555c' }, areaStyle: { color: '#2c2c31' } },
          handleStyle: { color: MUTED }, textStyle: { color: MUTED, fontSize: 11 }
        }
      ],
      series: [
        {
          name: 'K线', type: 'candlestick', data: c.kline,
          itemStyle: { color: UP, color0: DOWN, borderColor: UP, borderColor0: DOWN },
          markPoint: { data: marks, silent: true, labelLayout: { hideOverlap: true } },
          markLine: {
            silent: true, symbol: 'none',
            lineStyle: { color: '#6e7076', type: 'dashed', width: 1, opacity: 0.5 },
            label: { show: false },
            data: annLines
          }
        },
        { name: 'MA20', type: 'line', data: c.ma20, symbol: 'none', lineStyle: { width: 1, color: '#c9cbd0', opacity: 0.8 }, connectNulls: true },
        { name: 'MA60', type: 'line', data: c.ma60, symbol: 'none', lineStyle: { width: 1, color: '#6e7076', opacity: 0.9 }, connectNulls: true },
        {
          name: '日净申赎', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, barMaxWidth: 7,
          data: c.flows.map(function (v) {
            if (v === null || v === undefined) return { value: null };
            return { value: v, itemStyle: { color: v >= 0 ? FLOW : FLOW_NEG } };
          })
        },
        {
          name: '20日累计', type: 'line', xAxisIndex: 1, yAxisIndex: 2, data: c.cum,
          symbol: 'none', lineStyle: { width: 1.4, color: CUM }, connectNulls: true
        }
      ]
    }, true);

    if (!el.__klineResize) {
      el.__klineResize = true;
      window.addEventListener('resize', function () { ch.resize(); });
    }
    return c;
  }

  window.KLine = { mount: mount, fmt: fmt };
})();
