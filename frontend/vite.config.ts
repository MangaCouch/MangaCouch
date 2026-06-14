import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { VitePWA } from 'vite-plugin-pwa';

// https://vite.dev/config/
// The production build outputs to `dist/`, which the FastAPI backend serves as
// static assets at the site root. API calls go to `/api/*` on the same origin.
export default defineConfig({
  base: '/',
  plugins: [
    react(),
    VitePWA({
      registerType: 'autoUpdate',
      includeAssets: ['favicon.svg', 'robots.txt'],
      // Don't precache API responses; the SPA shell is precached for offline use.
      workbox: {
        navigateFallback: '/index.html',
        // Never let the SW intercept API or page-image traffic.
        navigateFallbackDenylist: [/^\/api\//],
        globPatterns: ['**/*.{js,css,html,svg,png,ico,woff2}'],
        runtimeCaching: [
          {
            // Cover/page thumbnails are content-addressed and immutable.
            urlPattern: /\/api\/archives\/.*\/thumbnail.*/,
            handler: 'CacheFirst',
            options: {
              cacheName: 'mc-thumbnails',
              expiration: { maxEntries: 2000, maxAgeSeconds: 60 * 60 * 24 * 30 },
              cacheableResponse: { statuses: [0, 200] },
            },
          },
        ],
      },
      manifest: {
        name: 'MangaCouch',
        short_name: 'MangaCouch',
        description: 'Self-hosted manga library and reader.',
        theme_color: '#0f1115',
        background_color: '#0f1115',
        display: 'standalone',
        orientation: 'any',
        start_url: '/',
        scope: '/',
        icons: [
          {
            src: 'pwa-192.png',
            sizes: '192x192',
            type: 'image/png',
            purpose: 'any maskable',
          },
          {
            src: 'pwa-512.png',
            sizes: '512x512',
            type: 'image/png',
            purpose: 'any maskable',
          },
        ],
      },
    }),
  ],
  build: {
    outDir: 'dist',
    sourcemap: false,
    target: 'es2021',
  },
  server: {
    port: 5173,
    // During `npm run dev`, proxy the API to the local FastAPI backend so the
    // SPA talks to a real server with no CORS configuration.
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
});
