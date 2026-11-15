import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { ref } from 'vue'
import DecisionReason from '../components/DecisionReason.vue'

const meta = {
  id: 'decision-reason',
  title: 'Forms/Decision reason',
  component: DecisionReason,
  args: { modelValue: '', idPrefix: 'story-reason', disabled: false },
  render: (args) => ({
    components: { DecisionReason },
    setup() {
      const value = ref(args.modelValue)
      return { args, value }
    },
    template: '<DecisionReason v-bind="args" v-model="value" />',
  }),
} satisfies Meta<typeof DecisionReason>
export default meta
type Story = StoryObj<typeof meta>

export const Empty: Story = {}
export const QuickReason: Story = { args: { modelValue: 'already done' } }
export const Custom: Story = { args: { modelValue: 'The owner confirmed this belongs in the next workshop.' } }
export const Disabled: Story = { args: { modelValue: 'wrong person', disabled: true } }
export const LongContent: Story = {
  args: { modelValue: 'The team reviewed the proposal and agreed to keep the current approach. '.repeat(20).slice(0, 1000) },
}
export const Narrow: Story = {
  args: { ...Custom.args },
  decorators: [() => ({ template: '<div style="max-width: 390px"><story /></div>' })],
}
