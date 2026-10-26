/** 数字与日期的显示规则，全站共用一处 */

function bad(x) {
  return x === null || x === undefined || (typeof x === 'number' && Number.isNaN(x));
}

export function num(x, digits = 2) {
  if (bad(x)) return '—';
  return Math.abs(x).toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function signed(x, digits = 2) {
  if (bad(x)) return '—';
  const body = num(x, digits);
  return x > 0 ? `+${body}` : x < 0 ? `-${body}` : body;
}

export function pct(x, digits = 1) {
  if (bad(x)) return '—';
  return `${x > 0 ? '+' : x < 0 ? '-' : ''}${num(x, digits)}%`;
}

export function tone(x) {
  if (bad(x)) return 'flat';
  return x > 0 ? 'up' : x < 0 ? 'down' : 'flat';
}

export function side(s) {
  return s > 0 ? '买入' : '卖出';
}

export function shortDate(d) {
  return d ? d.slice(5) : '—';
}
