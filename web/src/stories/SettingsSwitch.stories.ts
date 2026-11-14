import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { ref } from 'vue'
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
    template: '<SettingsSwitch v-bind="args" aria-label="Check vault answers" :checked="checked" @toggle="checked = !checked" />',
  }),
} satisfies Meta<typeof SettingsSwitch>
export default meta
type Story = StoryObj<typeof meta>

export const On: Story = {}
export const Off: Story = { args: { checked: false } }
export const DisabledOn: Story = { args: { disabled: true } }
export const DisabledOff: Story = { args: { checked: false, disabled: true } }
export const Busy: Story = { args: { busy: true } }
export const Narrow: Story = {
  decorators: [() => ({ template: '<div style="max-width: 390px"><story /></div>' })],
}
