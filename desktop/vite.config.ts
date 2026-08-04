import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'

const root = fileURLToPath(new URL('.', import.meta.url))

export default defineConfig({
  server: {
    // `tauri.conf.json` points devUrl at this exact port. Without pinning it
    // vite falls back to 5173 and `tauri dev` waits out its 180s timeout on
    // http://localhost:1420/startup.html. strictPort makes a clash fail loudly
    // instead of silently drifting to another port and breaking dev again.
    port: 1420,
    strictPort: true,
  },
  build: {
    target: 'safari16',
    rollupOptions: {
      input: {
        startup: resolve(root, 'startup.html'),
      },
    },
  },
  clearScreen: false,
})
