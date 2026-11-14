import type { StorybookConfig } from '@storybook/vue3-vite'
import { fileURLToPath } from 'node:url'

const config: StorybookConfig = {
  framework: {
    name: '@storybook/vue3-vite',
    options: {
      builder: { viteConfigPath: fileURLToPath(new URL('./vite.config.ts', import.meta.url)) },
      docgen: { plugin: 'vue-component-meta', tsconfig: 'tsconfig.json' },
    },
  },
  stories: ['../src/stories/**/*.stories.ts'],
  addons: ['@storybook/addon-docs'],
  core: { disableTelemetry: true },
}
export default config
