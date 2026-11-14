import type { Preview } from '@storybook/vue3-vite'
import '../src/styles/app.css'

const preview: Preview = {
  parameters: { layout: 'fullscreen' },
  decorators: [() => ({
    template: '<div style="padding: var(--space-4); min-width: 0; box-sizing: border-box; background: var(--bg); color: var(--fg)"><story /></div>',
  })],
}
export default preview
