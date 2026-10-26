/* 入场：区块里的标题、数字、列表依次抬起 14px 淡入，只播一次。
   transform / opacity；prefers-reduced-motion 下直接显示（CSS 里也兜了一层）。 */
(function () {
  'use strict';
  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var items = document.querySelectorAll('.reveal');
  if (reduce || !('IntersectionObserver' in window)) {
    for (var i = 0; i < items.length; i++) items[i].classList.add('in');
    return;
  }
  var io = new IntersectionObserver(function (entries) {
    entries.forEach(function (en) {
      if (!en.isIntersecting) return;
      en.target.classList.add('in');
      io.unobserve(en.target);
    });
  }, { rootMargin: '0px 0px -8% 0px', threshold: 0.05 });

  // 同一容器里的相邻元素错开 70ms
  for (var j = 0; j < items.length; j++) {
    var el = items[j];
    var sibs = el.parentElement ? el.parentElement.querySelectorAll(':scope > .reveal') : [];
    var k = Array.prototype.indexOf.call(sibs, el);
    if (k > 0) el.style.setProperty('--d', Math.min(k, 6) * 70 + 'ms');
    io.observe(el);
  }
})();
