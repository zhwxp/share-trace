import { defineConfig } from 'astro/config';

// 产物是纯静态 HTML，路径保持旧站的 /index/<slug>.html 形式，
// 已有链接和收藏不用改。
export default defineConfig({
  build: {
    format: 'file',
  },
  devToolbar: { enabled: false },
});
