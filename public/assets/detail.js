/* 指数详情页：K 线 + 申赎柱 + 20 日累计 + 买卖点。
   序列走 /data/index/<slug>.json，HTML 里只留骨架；页面共用这一份逻辑。 */
(function () {
  'use strict';

  // 红涨绿跌只出现在 K 线里；其余一律 ink / 灰，资金流那条线用 --chart-1
  var UP = '#c4242a', DOWN = '#2e7d32', GRID = '#ececec', AXIS = '#e6e6e6', MUTED = '#8a8f97';
  var TX = '#0a0a0a', FLOW = '#1d5fbf';

  var el = document.getElementById('detailChart');
  if (!el || !window.echarts) return;
  var status = document.getElementById('chart-status');

  fetch(el.dataset.src)
    .then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(function (c) {
      mount(c);
      if (status) status.remove();
    })
    .catch(function (err) {
      if (status) status.textContent = '图表数据没加载成功（' + err.message + '），下面的表格数字不受影响。';
    });

  function mount(c) {
    var n = c.dates.length;

    // 派生序列在客户端算：均线 20/60，20 日滚动累计
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

    var lowMap = {}, highMap = {};
    c.dates.forEach(function (d, i) { lowMap[d] = c.kline[i][2]; highMap[d] = c.kline[i][3]; });

    function fmt(x, digits) {
      if (x === null || x === undefined || isNaN(x)) return '—';
      digits = digits === undefined ? 2 : digits;
      return Math.abs(x).toFixed(digits).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    }
    function color(x) { return x > 0 ? UP : x < 0 ? DOWN : MUTED; }

    var axis = {
      axisLine: { lineStyle: { color: AXIS } },
      axisTick: { show: false },
      axisLabel: { color: MUTED, fontSize: 10 },
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
        itemStyle: { color: buy ? FLOW : '#8a8f97', borderColor: '#fff', borderWidth: 1, opacity: 0.6 + 0.4 * w },
        label: {
          show: w >= 0.35,
          position: buy ? 'bottom' : 'top',
          distance: 6,
          color: buy ? FLOW : MUTED,
          fontSize: 10,
          formatter: function (p) { return (p.value > 0 ? '+' : '') + Math.round(p.value); }
        }
      };
    });

    var annLines = c.ann
      .filter(function (a) { return n && a >= c.dates[0] && a <= c.dates[n - 1]; })
      .map(function (a) { return { xAxis: a }; });

    var zoomStart = n > 380 ? Math.max(0, 100 - (380 / n) * 100) : 0;

    var ch = echarts.init(el, null, { renderer: 'canvas' });
    ch.setOption({
      animation: false,
      backgroundColor: 'transparent',
      textStyle: { fontFamily: 'inherit' },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross', link: [{ xAxisIndex: 'all' }] },
        backgroundColor: '#ffffff',
        borderColor: GRID,
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
      axisPointer: { link: [{ xAxisIndex: 'all' }], label: { backgroundColor: '#f2f1ec', color: TX } },
      grid: [
        { left: 56, right: 16, top: 16, height: '52%' },
        { left: 56, right: 48, top: '70%', height: '15%' }
      ],
      xAxis: [
        Object.assign({ type: 'category', data: c.dates, gridIndex: 0, boundaryGap: true, axisLabel: { show: false } }, axis),
        Object.assign({ type: 'category', data: c.dates, gridIndex: 1, boundaryGap: true }, axis,
          { axisLabel: { color: MUTED, fontSize: 10, interval: Math.ceil(n / 7) } })
      ],
      yAxis: [
        Object.assign({ scale: true, gridIndex: 0 }, axis),
        Object.assign({ scale: true, gridIndex: 1, splitNumber: 2 }, axis),
        Object.assign({
          scale: true, gridIndex: 1, splitNumber: 2, position: 'right',
          splitLine: { show: false }, axisLabel: { color: MUTED, fontSize: 9 }
        }, axis)
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1], start: zoomStart, end: 100 },
        {
          type: 'slider', xAxisIndex: [0, 1], start: zoomStart, end: 100, bottom: 8, height: 16,
          borderColor: GRID, fillerColor: 'rgba(29,95,191,.08)',
          handleStyle: { color: FLOW }, textStyle: { color: MUTED, fontSize: 10 }
        }
      ],
      series: [
        {
          name: 'K线', type: 'candlestick', data: c.kline,
          itemStyle: { color: UP, color0: DOWN, borderColor: UP, borderColor0: DOWN },
          markPoint: { data: marks, silent: true, labelLayout: { hideOverlap: true } },
          markLine: {
            silent: true, symbol: 'none',
            lineStyle: { color: MUTED, type: 'dashed', width: 1, opacity: 0.4 },
            label: { show: false },
            data: annLines
          }
        },
        { name: 'MA20', type: 'line', data: c.ma20, symbol: 'none', lineStyle: { width: 1, color: '#6b6b6b', opacity: 0.9 }, connectNulls: true },
        { name: 'MA60', type: 'line', data: c.ma60, symbol: 'none', lineStyle: { width: 1, color: '#b9bcc2', opacity: 0.95 }, connectNulls: true },
        {
          name: '日净申赎', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, barMaxWidth: 7,
          data: c.flows.map(function (v) {
            if (v === null || v === undefined) return { value: null };
            return { value: v, itemStyle: { color: v >= 0 ? FLOW : '#cfd3d8' } };
          })
        },
        {
          name: '20日累计', type: 'line', xAxisIndex: 1, yAxisIndex: 2, data: c.cum,
          symbol: 'none', lineStyle: { width: 1.4, color: FLOW }, connectNulls: true
        }
      ]
    });
    window.addEventListener('resize', function () { ch.resize(); });
  }
})();
