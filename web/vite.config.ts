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
      '/api': backendUrl,
      '/ws': { target: backendWsUrl, ws: true },
    },
  },
})
