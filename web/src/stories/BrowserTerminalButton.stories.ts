import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import { defineComponent, h, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import BrowserTerminalButton from '../components/BrowserTerminalButton.vue'
import type { FixtureConfig } from './fixtureBoundary'
import { mountConnected, type ConnectedFixture } from './mountConnected'
import { statusRoute } from './terminalFixtures'

// The button only opens the terminal store. Opening reads the engine's status
// once, so the connected fixture declares that read and nothing else: the
// status answers "not available" and no pane is mounted in this story.
const fixture: FixtureConfig = { routes: [statusRoute({ available: false, reason: 'disabled', unlock_expires_in: 0 })] }

function renderButton(variant: 'link' | 'row') {
  return defineComponent({
    setup() {
      const host = ref<HTMLElement>()
      const connected = shallowRef<ConnectedFixture>()
      onMounted(() => {
        const button = defineComponent({
          render: () => h(BrowserTerminalButton, { chatId: 'storybook-alpha', variant }),
        })
        connected.value = mountConnected(host.value!, { component: button })
      })
      onBeforeUnmount(() => connected.value?.dispose())
      return () => h('div', { style: 'max-width: 390px' }, [
        h('p', `Synthetic ${variant} fixture. Fixture requests: ${connected.value?.boundary.requests.length ?? 0}. Violations: ${connected.value?.boundary.violations.length ?? 0}.`),
        h('div', { ref: host }),
        ...(connected.value?.boundary.violations ?? []).map(message => h('p', { role: 'alert' }, message)),
      ])
    },
  })
}

const meta = {
  id: 'browser-terminal-button',
  title: 'Terminal/BrowserTerminalButton',
  component: BrowserTerminalButton,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof BrowserTerminalButton>
export default meta
type Story = StoryObj<typeof meta>

// The click is the only interaction: it reads the status once (one declared
// request) and the button stays where it was.
export const Default: Story = {
  parameters: { fixture },
  render: () => renderButton('link'),
  play: async ({ canvas, userEvent }) => {
    const button = await canvas.findByRole('button', { name: 'Open in Terminal' })
    await userEvent.tab()
    expect(document.activeElement).toBe(button)
    await userEvent.click(button)
    await waitFor(() => expect(canvas.getByText(/Fixture requests: 1\./)).toBeTruthy())
    expect(canvas.getByText(/Violations: 0\./)).toBeTruthy()
    expect(canvas.getByRole('button', { name: 'Open in Terminal' })).toBeEnabled()
  },
}

export const Row: Story = {
  parameters: { fixture },
  render: () => renderButton('row'),
  play: async ({ canvas, userEvent }) => {
    const row = await canvas.findByRole('button', { name: 'Open terminal' })
    await userEvent.click(row)
    await waitFor(() => expect(canvas.getByText(/Fixture requests: 1\./)).toBeTruthy())
    expect(canvas.getByText(/Violations: 0\./)).toBeTruthy()
    expect(row).toHaveTextContent('Terminal')
  },
}
