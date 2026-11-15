import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { ref, watch } from 'vue'
import WorkspaceColorPicker from '../components/settings/WorkspaceColorPicker.vue'

const meta = {
  id: 'settings-workspace-color-picker',
  title: 'Settings/Workspace color picker',
  component: WorkspaceColorPicker,
  args: { modelValue: 'amber', disabled: false, labelledby: 'workspace-colour-label' },
  render: (args) => ({
    components: { WorkspaceColorPicker },
    setup() {
      const selected = ref(args.modelValue)
      watch(() => args.modelValue, (value) => { selected.value = value })
      return { args, selected }
    },
    template: '<div><p :id="args.labelledby">Workspace colour</p><WorkspaceColorPicker v-bind="args" v-model="selected" /></div>',
  }),
} satisfies Meta<typeof WorkspaceColorPicker>
export default meta
type Story = StoryObj<typeof meta>

export const Default: Story = {}
export const Selected: Story = { args: { modelValue: 'pink' } }
export const Expanded: Story = { args: { modelValue: 'teal' } }
export const SelectedExtraCollapsed: Story = {
  args: { modelValue: 'teal' },
  play: async ({ canvasElement }) => {
    // Exercise the owner's disclosure; do not simulate its private expanded ref.
    canvasElement.querySelector<HTMLButtonElement>('button[aria-label="Show fewer colours"]')!.click()
  },
}
export const Disabled: Story = { args: { disabled: true } }
export const DisabledExpanded: Story = { args: { modelValue: 'teal', disabled: true } }
export const Narrow: Story = {
  args: { modelValue: 'teal' },
  decorators: [() => ({ template: '<div style="max-width: 390px"><story /></div>' })],
}
