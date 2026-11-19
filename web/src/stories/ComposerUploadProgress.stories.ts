import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import ComposerUploadProgress from '../components/ComposerUploadProgress.vue'
import type { ComposerUpload } from '../composables/useChatComposer'

/**
 * ComposerUploadProgress (the `components-composer-upload-progress`
 * inventory row, #3312): the composer's in-flight upload rows. Props only —
 * the one `uploads` prop the composer owns — so these stories render the
 * production component standalone with synthetic uploads. `total <= 0`
 * (unknown size) and `loaded >= total` (finished bytes, still listed) both
 * take the component's Processing branch (`isProcessing`).
 */
const upload = (overrides: Partial<ComposerUpload> = {}, id = 1): ComposerUpload => ({
  id,
  label: 'storybook-alpha.pdf',
  loaded: 0,
  total: 0,
  ...overrides,
})

// Long enough that it cannot fit a 390px composer at the xs size.
const NARROW_LABEL =
  'storybook-gamma-activity-log-capture-2026-11-19-with-a-long-synthetic-name.bin'

const meta = {
  id: 'composer-upload-progress',
  title: 'Chat leaves that survive/ComposerUploadProgress',
  component: ComposerUploadProgress,
  tags: ['!autodocs'],
  args: { uploads: [upload({ loaded: 42, total: 105 })] },
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
  render: (args) => ({
    components: { ComposerUploadProgress },
    setup() {
      return { args }
    },
    template: '<ComposerUploadProgress v-bind="args" />',
  }),
} satisfies Meta<typeof ComposerUploadProgress>
export default meta
type Story = StoryObj<typeof meta>

// One upload mid-flight: percent 40 comes from the production rounding
// (42 of 105), and the progress element carries the phase label as its name.
export const Default: Story = {
  play: async ({ canvasElement }) => {
    const status = canvasElement.querySelector('[role="status"]')
    expect(status?.textContent).toBe('Loading storybook-alpha.pdf…')
    const bar = canvasElement.querySelector('progress') as HTMLProgressElement
    expect(bar.value).toBe(40)
    expect(bar.getAttribute('aria-label')).toBe('Loading storybook-alpha.pdf…')
    expect(canvasElement.querySelector('.composer-upload-pct')?.textContent).toBe('40%')
  },
}

// Unknown size (total <= 0) takes the Processing branch with percent() at 100.
export const Processing: Story = {
  args: { uploads: [upload({ label: 'storybook-beta.bin', loaded: 0, total: 0 }, 2)] },
  play: async ({ canvasElement }) => {
    const status = canvasElement.querySelector('[role="status"]')
    expect(status?.textContent).toBe('Processing storybook-beta.bin…')
    const bar = canvasElement.querySelector('progress') as HTMLProgressElement
    expect(bar.value).toBe(100)
    expect(canvasElement.querySelector('.composer-upload-pct')?.textContent).toBe('100%')
  },
}

// The v-for renders one row per in-flight upload across the phases: Loading,
// unknown-size Processing, and a completed-but-still-listed upload (loaded >=
// total) that shows Processing as well.
export const Multiple: Story = {
  args: {
    uploads: [
      upload({ loaded: 42, total: 105 }),
      upload({ label: 'storybook-beta.bin', loaded: 0, total: 0 }, 2),
      upload({ label: 'storybook-gamma.log', loaded: 105, total: 105 }, 3),
    ],
  },
  play: async ({ canvasElement }) => {
    expect(canvasElement.querySelectorAll('.composer-upload').length).toBe(3)
    expect([...canvasElement.querySelectorAll('[role="status"]')].map((el) => el.textContent ?? '')).toEqual([
      'Loading storybook-alpha.pdf…',
      'Processing storybook-beta.bin…',
      'Processing storybook-gamma.log…',
    ])
    const bars = [...canvasElement.querySelectorAll('progress')] as HTMLProgressElement[]
    expect(bars.map((bar) => bar.value)).toEqual([40, 100, 100])
  },
}

// The root v-if hides the whole list while nothing is in flight.
export const Empty: Story = {
  args: { uploads: [] },
  play: async ({ canvasElement }) => {
    expect(canvasElement.querySelector('.composer-uploads')).toBeNull()
    expect(canvasElement.querySelector('.composer-upload')).toBeNull()
  },
}

// At phone width the composer keeps its inset and the long label ellipsises
// instead of wrapping or pushing the row past its container.
export const Narrow: Story = {
  decorators: [() => ({ template: '<div style="max-width: 390px"><story /></div>' })],
  args: { uploads: [upload({ label: NARROW_LABEL, loaded: 0, total: 0 }, 9)] },
  play: async ({ canvasElement }) => {
    const root = canvasElement.querySelector('.composer-uploads') as HTMLElement
    expect(root.getBoundingClientRect().width).toBeLessThanOrEqual(390)
    const label = canvasElement.querySelector('.composer-upload-label') as HTMLElement
    expect(getComputedStyle(label).textOverflow).toBe('ellipsis')
    expect(label.scrollWidth).toBeGreaterThan(label.clientWidth)
    const row = canvasElement.querySelector('.composer-upload-row') as HTMLElement
    expect(row.scrollWidth).toBeLessThanOrEqual(row.clientWidth)
  },
}
