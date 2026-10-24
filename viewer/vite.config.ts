/// <reference types="vitest/config" />
import { defineConfig, loadEnv, type ProxyOptions } from 'vite';
import react from '@vitejs/plugin-react';
import { msw } from 'msw/vite';

// 実環境向け: VIEWER_PROXY_TARGET に CloudFront の URL(例 https://dxxxx.cloudfront.net)を入れると、
// dev サーバーが /api と /media をそこへプロキシする。本番ビルドは同一オリジンから配信される前提で相対パスのまま。
// モック: `npm run dev:mock`(--mode mock)で MSW を有効にし、AWS なしで動かす。
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const target = env.VIEWER_PROXY_TARGET;
  const isMock = mode === 'mock';

  const proxy: Record<string, ProxyOptions> | undefined =
    target && !isMock
      ? {
          '/api': { target, changeOrigin: true, secure: true },
          '/media': { target, changeOrigin: true, secure: true },
        }
      : undefined;

  return {
    plugins: [react(), ...(isMock ? [msw({ mode: 'worker-only' })] : [])],
    server: { proxy },
    // hls.js と Mantine で 1 MB 強になるが、検証用の 1 画面アプリなので分割しない
    build: { outDir: 'dist', sourcemap: false, chunkSizeWarningLimit: 1600 },
    test: {
      environment: 'node',
      include: ['src/**/*.test.ts'],
    },
  };
});
