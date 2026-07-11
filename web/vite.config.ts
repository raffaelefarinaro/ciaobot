import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'

const backendUrl = process.env.VITE_BACKEND_URL || 'http://127.0.0.1:8543'
const backendWsUrl = backendUrl.replace(/^http/, 'ws')

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': resolve(__dirname, 'src') },
  },
  build: {
    outDir: '../ciao/web/static',
    emptyOutDir: true,
  },
  server: {
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
