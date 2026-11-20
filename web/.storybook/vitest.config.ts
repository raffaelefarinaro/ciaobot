// The Storybook browser interaction runner (run with `npm run test-storybook`).
// Each story renders in headless Chromium through @vitest/browser; a play
// function or an uncaught render error fails the story. The Storybook plugin
// derives the test list from the stories glob in main.ts, so no include list
// here. The builder reads the workshop's own vite config from main.ts's
// `builder.viteConfigPath`, but the runner only reads this file, so the shared
// config is merged here too: the Vue plugin compiles the production SFCs the
// stories import.
import { defineConfig } from 'vitest/config'
import storybookTest from '@storybook/addon-vitest/vitest-plugin'
import { playwright } from '@vitest/browser-playwright'
import sbConfig from './vite.config'

export default defineConfig({
  ...sbConfig,
  plugins: [storybookTest({ configDir: '.storybook' }), ...(sbConfig.plugins ?? [])],
  test: {
    browser: {
      enabled: true,
      headless: true,
      provider: playwright(),
      instances: [{ browser: 'chromium' }],
    },
  },
})
