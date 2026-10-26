/* 分指数页：左边选指数，右边换图。数据走 /data/index/<slug>.json，
   默认那个指数由构建期写进 HTML，所以没有脚本也能看到一张图和它的信号。 */
(function () {
  'use strict';
  var chart = document.getElementById('pChart');
  if (!chart || !window.echarts || !window.KLine) return;

  var list = document.querySelectorAll('.pitem');
  var sel = document.getElementById('pSelect');
  var nameEl = document.getElementById('pName');
  var statsEl = document.getElementById('pStats');
  var sigEl = document.getElementById('pSignals');
  var cache = {};
  var seq = 0;

  var nf = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  function signed(v) {
    if (v === null || v === undefined || isNaN(v)) return '—';
    var s = Math.abs(v).toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    return v > 0 ? '+' + s : v < 0 ? '-' + s : s;
  }
  function pct(v) {
    if (v === null || v === undefined || isNaN(v)) return '—';
    var s = Math.abs(v).toFixed(1);
    return (v > 0 ? '+' : v < 0 ? '-' : '') + s + '%';
  }
  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; });
  }

  function stats(c) {
    var flows = c.flows.filter(function (f) { return f !== null && f !== undefined; });
    var last = flows.length ? flows[flows.length - 1] : null;
    var tail = function (k) { return flows.slice(-k).reduce(function (a, b) { return a + b; }, 0); };
    return { today: last, d5: tail(5), d20: tail(20) };
  }

  function renderSignals(c) {
    var evs = (c.events || []).slice().reverse().slice(0, 8);
    if (!evs.length) {
      sigEl.innerHTML = '<p class="empty">这个指数还没有触发过信号。</p>';
      return;
    }
    sigEl.innerHTML = evs.map(function (e) {
      return '<div class="psrow">' +
        '<span class="psd">' + esc(e.date) + '</span>' +
        '<span class="pill ' + (e.side > 0 ? 'buy' : 'sell') + '">' + (e.side > 0 ? '买入' : '卖出') + '</span>' +
        '<span class="psv">' + signed(e.amount) + '</span>' +
        '<span class="psp">' + pct(e.post20) + '</span>' +
        '</div>';
    }).join('');
  }

  function setActive(slug, name) {
    for (var i = 0; i < list.length; i++) {
      if (list[i].getAttribute('data-slug') === slug) list[i].setAttribute('aria-current', 'true');
      else list[i].removeAttribute('aria-current');
    }
    if (sel && sel.value !== slug) sel.value = slug;
    if (nameEl && name) nameEl.textContent = name;
  }

  function show(slug, name, push) {
    if (!slug) return;
    if (!name) {
      for (var k = 0; k < list.length; k++) {
        if (list[k].getAttribute('data-slug') === slug) { name = list[k].getAttribute('data-name'); break; }
      }
    }
    setActive(slug, name);
    if (push && location.hash !== '#' + slug) {
      history.replaceState(null, '', '#' + slug);
    }
    var my = ++seq;
    var done = function (c) {
      if (my !== seq) return;            // 快速连点，只画最后一次
      var s = stats(c);
      statsEl.innerHTML =
        '<span>当日 <b class="' + (s.today >= 0 ? 'up' : '') + '">' + signed(s.today) + '</b></span>' +
        '<span>近 5 日 <b>' + signed(s.d5) + '</b></span>' +
        '<span>近 20 日 <b>' + signed(s.d20) + '</b></span>';
      window.KLine.mount(chart, c);
      renderSignals(c);
      chart.setAttribute('aria-label', (name || slug) + '的 K 线、均线与净申赎，标出 ' + (c.events || []).length + ' 个买卖点');
      document.title = (name || slug) + ' · 分指数 · ShareTrace';
    };
    if (cache[slug]) { done(cache[slug]); return; }
    fetch('/data/index/' + slug + '.json')
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (c) { cache[slug] = c; done(c); })
      .catch(function (err) {
        if (my !== seq) return;
        sigEl.innerHTML = '<p class="empty">这个指数的序列没加载成功（' + esc(err.message) + '）。</p>';
      });
  }

  for (var i = 0; i < list.length; i++) {
    list[i].addEventListener('click', function (e) {
      // 允许在新标签打开；普通点击只换图，不跳页
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1) return;
      e.preventDefault();
      show(this.getAttribute('data-slug'), this.getAttribute('data-name'), true);
      if (window.innerWidth < 900) {
        var top = document.querySelector('.pmain').getBoundingClientRect().top + window.scrollY - 60;
        window.scrollTo({ top: top, behavior: 'smooth' });
      }
    });
  }
  if (sel) sel.addEventListener('change', function () {
    var opt = sel.options[sel.selectedIndex];
    show(sel.value, opt ? opt.textContent : '', true);
  });

  window.addEventListener('hashchange', function () {
    var slug = location.hash.slice(1);
    if (slug) show(slug, null, false);
  });

  // 首屏也要画：没有 hash 就画构建期给的那个指数
  var defSlug = chart.dataset.src.split('/').pop().replace('.json', '');
  show(location.hash.slice(1) || defSlug, null, false);
})();
