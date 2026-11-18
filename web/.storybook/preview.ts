import type { Preview } from '@storybook/vue3-vite'
import { onUnmounted } from 'vue'
import '../src/styles/app.css'
import { assertNoFixtureViolations, fenceStory, type FixtureConfig } from '../src/stories/fixtureBoundary'

const wrapperTemplate = '<div style="padding: var(--space-4); min-width: 0; box-sizing: border-box; background: var(--bg); color: var(--fg)"><story /></div>'

const preview: Preview = {
  parameters: { layout: 'fullscreen' },
  decorators: [(_Story, context) => ({
    // Every story and docs page runs inside the fixture boundary, built from
    // `parameters.fixture` ({ routes, sockets }; empty when absent). There is
    // no opt-out flag: a story can only declare what it is allowed to reach.
    // fetch, XHR, WebSocket, EventSource and beacon attempts outside the
    // declaration are rejected and recorded as violations.
    setup() {
      const boundary = fenceStory(context.parameters.fixture as FixtureConfig | undefined, context.viewMode === 'docs')
      onUnmounted(() => boundary.dispose())
    },
    template: wrapperTemplate,
  })],
  // The boundary throws at the call site, but a component may swallow that
  // (`.catch(() => {})`). The recorded violations still fail the story here.
  afterEach: () => assertNoFixtureViolations(),
}
export default preview
