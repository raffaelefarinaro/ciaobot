import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import { ref } from 'vue'
import SlashCommandPicker from '../components/SlashCommandPicker.vue'
import type { SlashCommand } from '../lib/types'

const commands: SlashCommand[] = [
  { name: 'review', description: 'Review the current changes', argument_hint: '[path]', source: 'skill', path: '/skills/review' },
  { name: 'clear', description: '', argument_hint: '', source: 'builtin', path: '' },
]

const meta = {
  id: 'slash-command-picker',
  title: 'Chat/SlashCommandPicker',
  component: SlashCommandPicker,
  args: { commands, highlight: 0 },
  render: (args) => ({
    components: { SlashCommandPicker },
    setup() {
      const picked = ref('none')
      const highlighted = ref(-1)
      return { args, picked, highlighted }
    },
    template: `<SlashCommandPicker v-bind="args" @pick="picked = $event.name" @highlight="highlighted = $event" />
      <p data-testid="events">{{ picked }} / {{ highlighted }}</p>`,
  }),
} satisfies Meta<typeof SlashCommandPicker>
export default meta
type Story = StoryObj<typeof meta>

export const Default: Story = {
  play: async ({ canvas, userEvent }) => {
    const options = canvas.getAllByRole('option')
    expect(options[0].getAttribute('aria-selected')).toBe('true')
    expect(options[1].getAttribute('aria-selected')).toBe('false')
    expect(options[0].textContent).toContain('skill')
    expect(options[0].textContent).toContain('[path]')
    expect(options[0].textContent).toContain('Review the current changes')
    await userEvent.hover(options[1])
    expect(canvas.getByTestId('events').textContent).toContain('none / 1')
    await userEvent.click(options[1])
    expect(canvas.getByTestId('events').textContent).toContain('clear / 1')
  },
}

export const Empty: Story = {
  args: { commands: [], highlight: 0 },
  play: async ({ canvas }) => {
    expect(canvas.getByRole('listbox', { name: 'Slash commands' })).toBeTruthy()
    expect(canvas.queryAllByRole('option')).toHaveLength(0)
  },
}
