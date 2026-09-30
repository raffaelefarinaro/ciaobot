/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'

const backendUrl = process.env.VITE_BACKEND_URL || 'http://127.0.0.1:8543'
const backendWsUrl = backendUrl.replace(/^http/, 'ws')

export default defineConfig({
  plugins: [
    vue({
      // Leave root-absolute template URLs (`src="/face.png"`, all in `public/`)
      // as plain URLs instead of compiling them into imports. The build served
      // them from `public/` either way; under vitest on Windows the import
      // resolved to `file:///face.png`, which is not a valid path there, and
      // every component that renders one failed to load.
      template: { transformAssetUrls: { includeAbsolute: false } },
    }),
  ],
  resolve: {
    alias: { '@': resolve(__dirname, 'src') },
  },
  build: {
    outDir: '../ciao/web/static',
    emptyOutDir: true,
  },
  test: {
    // `e2e/` holds Playwright specs. They share vitest's default `*.spec.ts`
    // naming but import `@playwright/test`, which throws the moment vitest
    // loads it — and a suite that reports "4 files failed, 1168 tests passed"
    // is the kind of green-with-red that people learn to scroll past.
    exclude: ['**/node_modules/**', '**/dist/**', 'e2e/**'],
  },
  server: {
    allowedHosts: true,
    proxy: {
      // Object form keeps changeOrigin off so the browser's Host header
      // reaches the backend; its same-origin check compares Origin against
      // Host, and the string shorthand rewrites Host to the target, turning
      // every dev-server write request into a 403.
      '/api': { target: backendUrl },
      '/ws': { target: backendWsUrl, ws: true },
    },
  },
})
