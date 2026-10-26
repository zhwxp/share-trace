import type { APIRoute } from 'astro';
import { load, chartPayload } from '../../../lib/data.mjs';

export const prerender = true;

export function getStaticPaths() {
  const { indices } = load();
  return indices.map((it) => ({ params: { slug: it.slug } }));
}

/** 详情页图表数据：独立 JSON，HTML 骨架不再内联几千个点 */
export const GET: APIRoute = ({ params }) => {
  const slug = params.slug as string;
  return new Response(JSON.stringify(chartPayload(slug)), {
    headers: { 'Content-Type': 'application/json; charset=utf-8' },
  });
};
