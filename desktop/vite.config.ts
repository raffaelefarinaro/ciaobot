import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'

const root = fileURLToPath(new URL('.', import.meta.url))

export default defineConfig({
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
