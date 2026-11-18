import type { Preview } from '@storybook/vue3-vite'
import { onUnmounted } from 'vue'
import '../src/styles/app.css'
import { installFixtureBoundary } from '../src/stories/fixtureBoundary'

const wrapperTemplate = '<div style="padding: var(--space-4); min-width: 0; box-sizing: border-box; background: var(--bg); color: var(--fg)"><story /></div>'

const preview: Preview = {
  parameters: { layout: 'fullscreen' },
  decorators: [(_Story, context) => {
    // Default network fence: every story that does not opt in through
    // `parameters.fixture` runs with an empty fixture boundary, so fetch,
    // XHR, WebSocket, EventSource and beacon attempts from a presentational
    // story are rejected, recorded as violations and fail the browser runner.
    // Connected stories install their own declared boundary through
    // mountConnected and set `parameters.fixture`. Docs pages render many
    // stories at once; the single-mount guard forbids that, so they stay
    // unfenced and the runner's per-story check keeps its guarantee.
    if (context.viewMode === 'docs' || context.parameters.fixture) return { template: wrapperTemplate }
    return {
      setup() {
        const boundary = installFixtureBoundary({ routes: [] })
        onUnmounted(() => boundary.dispose())
      },
      template: wrapperTemplate,
    }
  }],
}
export default preview
