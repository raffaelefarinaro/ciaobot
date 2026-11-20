import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { onUnmounted, ref } from 'vue'
import { expect } from 'storybook/test'
import SettingsSwitch from '../components/settings/SettingsSwitch.vue'

const meta = {
  id: 'settings-switch',
  title: 'Selection remainder/SettingsSwitch',
  component: SettingsSwitch,
  args: { checked: true, disabled: false, busy: false },
  render: (args) => ({
    components: { SettingsSwitch },
    setup() {
      const checked = ref(args.checked)
      return { args, checked }
    },
    template: '<SettingsSwitch v-bind="args" aria-label="Resume after restart" :checked="checked" @toggle="checked = !checked" />',
  }),
} satisfies Meta<typeof SettingsSwitch>
export default meta
type Story = StoryObj<typeof meta>

const playTrackMotion = (expectNone: boolean): NonNullable<Story['play']> => async ({ canvas, userEvent }) => {
  const button = canvas.getByRole('switch')
  const track = button.querySelector('.switch-track') as HTMLElement
  const duration = getComputedStyle(track).transitionDuration.split(',')[0]!.trim()
  if (expectNone) expect(duration).toBe('0s')
  else expect(duration).not.toBe('0s')
  await userEvent.click(button)
  expect(button.getAttribute('aria-checked')).toBe('false')
}

// The story's own `@toggle` handler flips a local ref in response to the
// production component's click event; this proves the click emits and
// aria-checked follows the controlled prop, not a parent's persistence flow.
// Focus stays on the switch.
export const On: Story = {
  play: async ({ canvas, userEvent }) => {
    const button = canvas.getByRole('switch')
    expect(button.getAttribute('aria-checked')).toBe('true')
    await userEvent.click(button)
    expect(button.getAttribute('aria-checked')).toBe('false')
    expect(document.activeElement).toBe(button)
  },
}
export const Off: Story = {
  args: { checked: false },
  play: async ({ canvas, userEvent }) => {
    const button = canvas.getByRole('switch')
    expect(button.getAttribute('aria-checked')).toBe('false')
    await userEvent.click(button)
    expect(button.getAttribute('aria-checked')).toBe('true')
  },
}
export const DisabledOn: Story = {
  args: { disabled: true },
  play: async ({ canvas, userEvent }) => {
    const button = canvas.getByRole('switch')
    expect(button).toBeDisabled()
    expect(button.getAttribute('aria-checked')).toBe('true')
    await userEvent.click(button)
    expect(button.getAttribute('aria-checked')).toBe('true')
  },
}
export const DisabledOff: Story = {
  args: { checked: false, disabled: true },
  play: async ({ canvas, userEvent }) => {
    const button = canvas.getByRole('switch')
    expect(button).toBeDisabled()
    expect(button.getAttribute('aria-checked')).toBe('false')
    await userEvent.click(button)
    expect(button.getAttribute('aria-checked')).toBe('false')
  },
}
export const Busy: Story = {
  args: { busy: true },
  play: async ({ canvas, userEvent }) => {
    const button = canvas.getByRole('switch')
    expect(button.getAttribute('aria-busy')).toBe('true')
    expect(button).toBeEnabled()
    expect(button.textContent).toContain('…')
    await userEvent.click(button)
    expect(button.getAttribute('aria-checked')).toBe('false')
  },
}
export const Narrow: Story = {
  decorators: [() => ({ template: '<div style="max-width: 390px"><story /></div>' })],
  play: async ({ canvas }) => {
    const button = canvas.getByRole('switch')
    expect(button.getAttribute('aria-checked')).toBe('true')
    expect(button.getBoundingClientRect().width).toBeLessThanOrEqual(390)
  },
}
export const KeyboardFocus: Story = {
  play: async ({ canvas, userEvent }) => {
    await userEvent.tab()
    const button = canvas.getByRole('switch')
    expect(document.activeElement).toBe(button)
    expect(button.matches(':focus-visible')).toBe(true)
    await userEvent.keyboard('{Enter}')
    expect(button.getAttribute('aria-checked')).toBe('false')
  },
}
// Workshop wrapper mirrors the production `@media (prefers-reduced-motion: reduce)`
// rule on the scoped track so the rendered state is visible without OS media.
const reducedMotionDecorator = () => ({
  setup() {
    const style = document.createElement('style')
    style.textContent = `
      .storybook-reduced-motion .settings-switch .switch-track,
      .storybook-reduced-motion .settings-switch .switch-track::after { transition: none; }
    `
    document.head.appendChild(style)
    onUnmounted(() => style.remove())
    return {}
  },
  template: '<div class="storybook-reduced-motion"><story /></div>',
})
export const ReducedMotion: Story = {
  decorators: [reducedMotionDecorator],
  play: playTrackMotion(true),
}