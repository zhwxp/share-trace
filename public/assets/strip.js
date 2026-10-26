/* 份额足迹条的读数：悬停 / 触摸 / 方向键逐日看。数据在 data-strip 上，
   格式 `序号串|金额串`，都是 base36；序号是距 2012-01-01 的天数。 */
(function () {
  'use strict';
  var T0 = Date.UTC(2012, 0, 1);
  var nf = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  function decode(el) {
    var raw = el.getAttribute('data-strip');
    if (!raw) return null;
    var parts = raw.split('|');
    var ord = parts[0].split(','), fl = parts[1].split(',');
    var dates = new Array(ord.length), flows = new Array(ord.length);
    for (var i = 0; i < ord.length; i++) {
      dates[i] = new Date(T0 + parseInt(ord[i], 36) * 86400000);
      flows[i] = parseInt(fl[i], 36) / 10;
    }
    return { dates: dates, flows: flows };
  }

  var iso = function (d) {
    return d.getUTCFullYear() + '-' +
      String(d.getUTCMonth() + 1).padStart(2, '0') + '-' +
      String(d.getUTCDate()).padStart(2, '0');
  };

  function init(el) {
    var data = decode(el);
    if (!data) return;
    var svg = el.querySelector('svg');
    var cursor = el.querySelector('.cursor');
    var cap = el.querySelector('.stripcap');
    var idle = cap ? cap.getAttribute('data-idle') : '';
    var n = data.flows.length;
    var timer = null;

    function show(i, x) {
      i = Math.max(0, Math.min(n - 1, i));
      var f = data.flows[i];
      var sign = f > 0 ? '+' : f < 0 ? '\u2212' : '';
      if (cap) {
        cap.innerHTML = '<span class="cr-d">' + iso(data.dates[i]) + '</span> ' + sign +
          nf.format(Math.abs(f)) + ' 亿元' + (f > 0 ? ' 净申购' : f < 0 ? ' 净赎回' : ' 持平');
        cap.classList.add('live');
      }
      var box = svg.getBoundingClientRect();
      var left = x === undefined ? (i / (n - 1)) * box.width : x;
      left = Math.max(0, Math.min(box.width, left));
      cursor.style.transform = 'translateX(' + left + 'px)';
      el.classList.add('is-live');
      el.dataset.i = String(i);
    }

    function hide() {
      el.classList.remove('is-live');
      if (cap && cap.classList.contains('live')) {
        cap.classList.remove('live');
        cap.textContent = idle;
      }
    }

    el.addEventListener('pointermove', function (e) {
      if (e.pointerType === 'touch') return;
      var box = svg.getBoundingClientRect();
      show(Math.round(((e.clientX - box.left) / box.width) * (n - 1)), e.clientX - box.left);
    });
    el.addEventListener('pointerleave', hide);
    el.addEventListener('click', function (e) {
      var box = svg.getBoundingClientRect();
      show(Math.round(((e.clientX - box.left) / box.width) * (n - 1)), e.clientX - box.left);
      clearTimeout(timer);
      timer = setTimeout(hide, 3200);
    });
    el.addEventListener('focus', function () { show(Number(el.dataset.i || n - 1)); });
    el.addEventListener('blur', hide);
    el.addEventListener('keydown', function (e) {
      var i = Number(el.dataset.i || n - 1);
      var step = e.shiftKey ? 20 : 1;
      if (e.key === 'ArrowRight') { show(i + step); e.preventDefault(); }
      else if (e.key === 'ArrowLeft') { show(i - step); e.preventDefault(); }
      else if (e.key === 'Home') { show(0); e.preventDefault(); }
      else if (e.key === 'End') { show(n - 1); e.preventDefault(); }
    });
  }

  var list = document.querySelectorAll('.footstrip');
  for (var i = 0; i < list.length; i++) init(list[i]);
})();
