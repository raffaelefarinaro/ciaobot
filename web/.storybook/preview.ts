import type { Preview } from '@storybook/vue3-vite'
import type { ViewportMap } from 'storybook/viewport'
import { computed, onUnmounted, watch } from 'vue'
import { DEFAULT_WORKSPACE_COLOR, WORKSPACE_COLOR_PRESETS } from '../src/lib/workspaceColors'
import { applyStoryAppearance } from '../src/stories/appearanceGlobals'
import '../src/styles/app.css'
import { assertNoFixtureViolations, fenceStory, type FixtureConfig } from '../src/stories/fixtureBoundary'

const wrapperTemplate = '<div style="padding: var(--space-4); min-width: 0; box-sizing: border-box; background: var(--bg); color: var(--fg)"><story /></div>'

// The production widths the layout is checked at, not stand-ins for them.
const viewports: ViewportMap = {
  narrow: { name: 'Narrow (390px)', styles: { width: '390px', height: '844px' }, type: 'mobile' },
  medium: { name: 'Medium (820px)', styles: { width: '820px', height: '1180px' }, type: 'tablet' },
  wide: { name: 'Wide (1280px)', styles: { width: '1280px', height: '800px' }, type: 'desktop' },
}

const accentItems = [
  { value: 'neutral', title: 'Neutral' },
  ...WORKSPACE_COLOR_PRESETS.map((preset) => ({ value: preset.id, title: preset.label })),
]

const preview: Preview = {
  parameters: { layout: 'fullscreen', viewport: { options: viewports } },
  initialGlobals: {
    theme: 'dark',
    contrast: 'normal',
    font: 'system',
    accent: DEFAULT_WORKSPACE_COLOR,
  },
  globalTypes: {
    theme: {
      description: 'The `theme-light` class on <html> (`lib/appearance.ts`).',
      toolbar: {
        title: 'Theme',
        icon: 'mirror',
        items: [
          { value: 'dark', title: 'Dark' },
          { value: 'light', title: 'Light' },
        ],
        dynamicTitle: true,
      },
    },
    contrast: {
      description: 'The `data-text-contrast` attribute on <html> (`lib/appearance.ts`).',
      toolbar: {
        title: 'Text contrast',
        icon: 'contrast',
        items: [
          { value: 'normal', title: 'Normal' },
          { value: 'soft', title: 'Soft' },
        ],
        dynamicTitle: true,
      },
    },
    font: {
      description: 'The `data-body-font` attribute on <html> (`lib/appearance.ts`).',
      toolbar: {
        title: 'Body font',
        icon: 'paragraph',
        items: [
          { value: 'system', title: 'System' },
          { value: 'serif', title: 'Serif' },
          { value: 'mono', title: 'Mono' },
        ],
        dynamicTitle: true,
      },
    },
    accent: {
      description: 'The `data-workspace-color` of the app shell (`App.vue`).',
      toolbar: {
        title: 'Workspace accent',
        icon: 'paintbrush',
        items: accentItems,
        dynamicTitle: true,
      },
    },
  },
  decorators: [
    (_Story, context) => ({
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
    }),
    (story, context) => ({
      components: { story },
      setup() {
        const root = document.documentElement
        const globals = context.globals
        // Applied during setup, so the first paint is already in the chosen
        // appearance; a toolbar change re-applies it through the same reset.
        let restore = applyStoryAppearance(globals, root)
        watch(
          () => [globals.theme, globals.contrast, globals.font],
          () => {
            restore()
            restore = applyStoryAppearance(globals, root)
          },
        )
        onUnmounted(() => restore())
        const accent = computed(() => (
          typeof globals.accent === 'string' ? globals.accent : DEFAULT_WORKSPACE_COLOR
        ))
        return { accent }
      },
      // Matches App.vue: the accent tokens live on the shell element, not <html>.
      template: '<div :data-workspace-color="accent" style="padding: var(--space-4); min-width: 0; box-sizing: border-box; background: var(--bg); color: var(--fg)"><story /></div>',
    }),
  ],
  // The boundary throws at the call site, but a component may swallow that
  // (`.catch(() => {})`). The recorded violations still fail the story here.
  afterEach: () => assertNoFixtureViolations(),
}
export default preview
