/* 指数详情页：取一份序列，交给 KLine 画。 */
(function () {
  'use strict';
  var el = document.getElementById('detailChart');
  if (!el || !window.echarts || !window.KLine) return;
  var status = document.getElementById('chart-status');

  fetch(el.dataset.src)
    .then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(function (c) {
      window.KLine.mount(el, c);
      if (status) status.remove();
    })
    .catch(function (err) {
      if (status) status.textContent = '图表数据没加载成功（' + err.message + '），下面的数字不受影响。';
    });
})();
