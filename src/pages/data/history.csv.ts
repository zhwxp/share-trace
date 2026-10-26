import type { APIRoute } from 'astro';
import { marketToday } from '../../lib/data.mjs';

export const prerender = true;

/** 逐日合计明细，history 页给下载链接 */
export const GET: APIRoute = () => {
  const t = marketToday();
  const lines = ['date,flow_yi,n_in,n_out,n_etf'];
  for (const d of t.history) {
    lines.push([d.date, d.flow, d.nIn, d.nOut, d.n].join(','));
  }
  return new Response(lines.join('\n') + '\n', {
    headers: { 'Content-Type': 'text/csv; charset=utf-8' },
  });
};
