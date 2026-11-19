import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import { onUnmounted } from 'vue'
import SettingsAppInstall from '../components/settings/SettingsAppInstall.vue'
import { installedOnDevice } from '../lib/installPrompt'

// No transport: the owner reads its platform and window facts once at setup,
// so it mounts plain under the default empty fixture fence.
const meta = {
  id: 'settings-app-install',
  title: 'Settings general/SettingsAppInstall',
  component: SettingsAppInstall,
  render: () => ({
    components: { SettingsAppInstall },
    template: '<SettingsAppInstall />',
  }),
} satisfies Meta<typeof SettingsAppInstall>
export default meta
type Story = StoryObj<typeof meta>

// The workshop runs on a loopback origin, which is a secure context, and in a
// browser tab, so the row reads "A browser tab" and the localhost origin.
export const Default: Story = {
  play: async ({ canvas }) => {
    expect(await canvas.findByText('A browser tab')).toBeTruthy()
    expect(canvas.getByText(/Secure: the host/)).toBeTruthy()
    const summary = canvas.getByText('Steps for every platform')
    expect((summary.closest('details') as HTMLDetailsElement).open).toBe(false)
  },
}

export const StepsExpanded: Story = {
  render: () => ({
    components: { SettingsAppInstall },
    template: '<SettingsAppInstall :expand-steps="true" />',
  }),
  play: async ({ canvas }) => {
    const summary = await canvas.findByText('Steps for every platform')
    expect((summary.closest('details') as HTMLDetailsElement).open).toBe(true)
  },
}

// The runner's keyboard events do not activate a native <summary>, so this story
// asserts the keyboard focus and its ring. The open state is StepsExpanded's.
export const KeyboardFocus: Story = {
  play: async ({ canvas, userEvent }) => {
    const summary = canvas.getByText('Steps for every platform')
    // Tab from the page start (the HTTPS help link comes first) to the summary.
    for (let i = 0; i < 5 && document.activeElement !== summary; i++) await userEvent.tab()
    expect(document.activeElement).toBe(summary)
    expect(summary.matches(':focus-visible')).toBe(true)
    expect((summary.closest('details') as HTMLDetailsElement).open).toBe(false)
  },
}

// The device-level fact comes from the install list SettingsView keeps; the
// story sets the same shared flag and clears it on teardown.
export const InstalledOnDevice: Story = {
  render: () => ({
    components: { SettingsAppInstall },
    setup() {
      installedOnDevice.value = true
      onUnmounted(() => { installedOnDevice.value = false })
      return {}
    },
    template: '<SettingsAppInstall />',
  }),
  play: async ({ canvas }) => {
    expect(await canvas.findByText(/The app is installed on this device too/)).toBeTruthy()
    expect(canvas.getByText('A browser tab')).toBeTruthy()
  },
}

export const Narrow: Story = {
  decorators: [() => ({ template: '<div style="max-width: 390px"><story /></div>' })],
  play: async ({ canvas }) => {
    const row = (await canvas.findByText('This window')).closest('.install-row') as HTMLElement
    expect(row.getBoundingClientRect().width).toBeLessThanOrEqual(390)
  },
}
