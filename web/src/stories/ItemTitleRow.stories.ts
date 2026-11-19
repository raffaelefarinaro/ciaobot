import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import { ref } from 'vue'
import ItemTitleRow from '../components/ItemTitleRow.vue'

// The shared fixture: one close control and a slotted plain title, with the
// host's close count in a screen-reader-only line (the emitted `close` has no
// DOM of its own, so the play reads the host's reaction instead).
const meta = {
  id: 'item-title-row',
  title: 'Rows and titles/ItemTitleRow',
  component: ItemTitleRow,
  args: { closeLabel: 'Close item' },
  render: (args) => ({
    components: { ItemTitleRow },
    setup() {
      const closed = ref(0)
      return { args, closed }
    },
    template: `
      <div style="display: flex; align-items: center; gap: 6px; min-height: 61px;">
        <ItemTitleRow v-bind="args" @close="closed += 1">
          <span class="pane-title">Storybook item title</span>
        </ItemTitleRow>
      </div>
      <p class="sr-only" data-testid="close-count">{{ closed }}</p>
    `,
  }),
} satisfies Meta<typeof ItemTitleRow>
export default meta
type Story = StoryObj<typeof meta>

// The close control with its shared .btn-icon box (the host styles it) plus
// the plain slotted title; the close tooltip falls back to the accessible
// name when no closeTitle is given, and a click emits close.
export const Default: Story = {
  play: async ({ canvas, userEvent }) => {
    const close = canvas.getByRole('button', { name: 'Close item' })
    expect((close as HTMLButtonElement).type).toBe('button')
    expect(close.getAttribute('title')).toBe('Close item')
    expect(close.querySelector('svg')!.getAttribute('aria-hidden')).toBe('true')
    expect(canvas.getByText('Storybook item title')).toBeTruthy()
    expect(canvas.getByTestId('close-count').textContent).toBe('0')
    await userEvent.click(close)
    expect(canvas.getByTestId('close-count').textContent).toBe('1')
  },
}

// closeTitle overrides the tooltip while aria-label keeps the accessible name
// (SchedulePanel: name "Close automation", tooltip "Close").
export const CloseTitle: Story = {
  args: { closeTitle: 'Close storybook item' },
  play: async ({ canvas }) => {
    const close = canvas.getByRole('button', { name: 'Close item' })
    expect(close.getAttribute('aria-label')).toBe('Close item')
    expect(close.getAttribute('title')).toBe('Close storybook item')
  },
}

// rail: true adds item-title-row--rail, so the row takes the header's width
// and the rail can centre the title on the reading column.
export const Rail: Story = {
  args: { rail: true },
  play: async ({ canvas, canvasElement }) => {
    const row = canvasElement.querySelector('.item-title-row') as HTMLElement
    expect(row.className).toContain('item-title-row--rail')
    expect(canvas.getByRole('button', { name: 'Close item' })).toBeTruthy()
  },
}

// Tab reaches the close button, :focus-visible draws the 2px accent outline,
// and Enter emits close.
export const KeyboardFocus: Story = {
  play: async ({ canvas, userEvent }) => {
    await userEvent.tab()
    const close = canvas.getByRole('button', { name: 'Close item' })
    expect(document.activeElement).toBe(close)
    expect(close.matches(':focus-visible')).toBe(true)
    expect(getComputedStyle(close).outlineWidth).toBe('2px')
    await userEvent.keyboard('{Enter}')
    expect(canvas.getByTestId('close-count').textContent).toBe('1')
  },
}

// The @media (max-width: 767px) rule follows the viewport, not the story
// wrapper, so this state pins the workshop's narrow 390px viewport (the
// browser runner applies it to the rendering page): close hides — it drops
// out of the accessibility tree entirely — and the title keeps the room.
export const PhoneNarrow: Story = {
  globals: { viewport: { value: 'narrow' } },
  play: async ({ canvasElement, canvas }) => {
    const close = canvasElement.querySelector('.item-title-row .close-btn') as HTMLElement
    expect(close).not.toBeNull()
    expect(getComputedStyle(close).display).toBe('none')
    // Hidden from the accessibility tree too: no button role is queryable.
    expect(canvas.queryByRole('button', { name: 'Close item' })).toBeNull()
    expect(canvas.getByText('Storybook item title')).toBeTruthy()
  },
}
