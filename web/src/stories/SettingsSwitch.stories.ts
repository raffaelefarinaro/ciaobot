import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { ref } from 'vue'
import { expect } from 'storybook/test'
import SettingsSwitch from '../components/settings/SettingsSwitch.vue'

const meta = {
  id: 'settings-switch',
  title: 'Settings/Settings switch',
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
export const Off: Story = { args: { checked: false } }
export const DisabledOn: Story = { args: { disabled: true } }
export const DisabledOff: Story = { args: { checked: false, disabled: true } }
export const Busy: Story = { args: { busy: true } }
export const Narrow: Story = {
  decorators: [() => ({ template: '<div style="max-width: 390px"><story /></div>' })],
}
