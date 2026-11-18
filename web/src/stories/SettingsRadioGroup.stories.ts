import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { ref } from 'vue'
import type { ConcreteComponent } from 'vue'
import SettingsRadioGroup from '../components/settings/SettingsRadioGroup.vue'

type RadioArgs = {
  label: string
  options: { value: string; label: string; fontFamily?: string }[]
  modelValue: string | null
}

const themeOptions = [
  { value: 'dark', label: 'Dark' },
  { value: 'light', label: 'Light' },
  { value: 'system', label: 'System' },
]
// Same option faces as the production SettingsAppearance consumer.
const fontOptions = [
  { value: 'system', label: 'System', fontFamily: 'var(--font-sans)' },
  { value: 'serif', label: 'Serif', fontFamily: 'var(--font-serif)' },
  { value: 'mono', label: 'Mono', fontFamily: 'var(--font-mono)' },
]

const meta = {
  id: 'settings-radio-group',
  title: 'Settings/Settings radio group',
  component: SettingsRadioGroup as unknown as ConcreteComponent<RadioArgs>,
  args: { label: 'Theme', options: themeOptions, modelValue: 'dark' },
  argTypes: { modelValue: { control: false, description: 'Locally controlled selection; click or use arrow keys, Home and End.' } },
  render: (args) => ({
    components: { SettingsRadioGroup },
    setup() {
      const selected = ref(args.modelValue)
      return { args, selected }
    },
    template: '<SettingsRadioGroup v-model="selected" :label="args.label" :options="args.options" />',
  }),
} satisfies Meta<RadioArgs>
export default meta
type Story = StoryObj<typeof meta>

export const Default: Story = {}
export const Selected: Story = {
  args: { modelValue: 'light' },
  globals: { theme: 'light' },
}
export const TextContrast: Story = {
  args: { label: 'Text contrast', modelValue: 'soft', options: [
    { value: 'normal', label: 'Normal' },
    { value: 'soft', label: 'Soft' },
  ] },
  globals: { contrast: 'soft' },
}
export const BodyFont: Story = {
  args: { label: 'Body font', options: fontOptions, modelValue: 'serif' },
  globals: { font: 'serif' },
}
export const Unselected: Story = { args: { modelValue: null } }
export const Narrow: Story = {
  args: { label: 'Body font', options: fontOptions, modelValue: 'serif' },
  globals: { font: 'serif' },
  decorators: [() => ({ template: '<div style="max-width: 390px"><story /></div>' })],
}
