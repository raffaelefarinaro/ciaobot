import type { Preview } from '@storybook/vue3-vite'
import '../src/styles/app.css'

const preview: Preview = {
  parameters: { layout: 'fullscreen' },
  decorators: [() => ({
    template: '<div style="height: 100vh; overflow: auto; padding: var(--space-4); min-width: 0; box-sizing: border-box"><story /></div>',
  })],
}
export default preview
