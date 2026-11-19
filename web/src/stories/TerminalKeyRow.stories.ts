import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, fn, waitFor } from 'storybook/test'
import { defineComponent, h, onBeforeUnmount, onMounted } from 'vue'
import TerminalKeyRow from '../components/TerminalKeyRow.vue'

// The production rule shows the row only under a coarse pointer, and the
// workshop runs with a fine pointer, where a hidden key cannot take focus or
// show in the accessibility tree. This host adds the coarse-pointer display
// for the story's lifetime and removes it again; the component is unchanged.
const coarsePointerDisplay = defineComponent({
  setup() {
    const style = document.createElement('style')
    style.textContent = '.tp-keys { display: flex !important; }'
    onMounted(() => document.head.append(style))
    onBeforeUnmount(() => style.remove())
    return () => null
  },
})

// The row only renders buttons and reports presses; it has no fetch and no
// store. Its keys are hidden under a fine pointer (the CSS shows them only
// under a coarse one), so the play functions assert the DOM and the emitted
// presses rather than the visibility.
const meta = {
  id: 'terminal-key-row',
  title: 'Terminal/TerminalKeyRow',
  component: TerminalKeyRow,
  tags: ['!autodocs'],
  args: { disabled: false, onPress: fn() },
  render: (args) => () => h('div', [h(coarsePointerDisplay), h(TerminalKeyRow, args)]),
} satisfies Meta<typeof TerminalKeyRow>
export default meta
type Story = StoryObj<typeof meta>

export const Default: Story = {
  play: async ({ args, canvas, userEvent }) => {
    const escape = canvas.getByRole('button', { name: 'Escape' })
    expect(escape).toBeEnabled()
    // A tap must not take the focus from the shell: pointerdown and mousedown
    // are prevented, so the focus stays where it was and the click still fires.
    const before = document.activeElement
    await userEvent.click(escape)
    expect(args.onPress).toHaveBeenCalledWith('escape', true)
    expect(document.activeElement).toBe(before)
  },
}

export const Disabled: Story = {
  args: { disabled: true },
  play: async ({ args, canvas, userEvent }) => {
    const escape = canvas.getByRole('button', { name: 'Escape' })
    expect(escape).toBeDisabled()
    expect(canvas.getByRole('button', { name: 'Control C' })).toBeDisabled()
    await userEvent.click(escape)
    expect(args.onPress).not.toHaveBeenCalled()
  },
}

// A keyboard activation (Enter on a focused key) is reported as not a pointer,
// and the focus stays on the key so the key can be pressed again.
export const KeyboardFocus: Story = {
  play: async ({ args, canvas, userEvent }) => {
    const up = canvas.getByRole('button', { name: 'Up arrow' })
    up.focus()
    await waitFor(() => expect(document.activeElement).toBe(up))
    await userEvent.keyboard('{Enter}')
    expect(args.onPress).toHaveBeenCalledWith('up', false)
    expect(document.activeElement).toBe(up)
  },
}
