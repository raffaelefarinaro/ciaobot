import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import { ref } from 'vue'
import NeedsYouRow from '../components/NeedsYouRow.vue'
import type { NeedsYouMenuItem } from '../components/NeedsYouRow.vue'

// Home's "Needs you" row: one title, one line, one primary control and, only
// when there is more to offer, a ⋯ menu. Everything is synthetic text.
const items: NeedsYouMenuItem[] = [
  { id: 'later', label: 'Later' },
  { id: 'settings', label: 'Open Settings', href: '/settings' },
  { id: 'unavailable', label: 'Not available yet', disabled: true },
]

function renderRow(args: Record<string, unknown>) {
  return {
    components: { NeedsYouRow },
    setup() {
      const selected = ref('')
      return { args, selected }
    },
    template: `<div>
      <ul style="margin: 0; padding: 0">
        <NeedsYouRow v-bind="args" @select="selected = $event">
          <template #primary><button type="button" class="btn-small">Run check</button></template>
        </NeedsYouRow>
      </ul>
      <p data-testid="story-selection">Selected: {{ selected || 'none' }}</p>
    </div>`,
  }
}

const meta = {
  id: 'needs-you-row',
  title: 'Notices and notifications/NeedsYouRow',
  component: NeedsYouRow,
  args: {
    title: 'Synthetic maintenance check',
    line: 'The workspace backup has not run yet.',
  },
  render: (args) => renderRow(args as unknown as Record<string, unknown>),
} satisfies Meta<typeof NeedsYouRow>
export default meta
type Story = StoryObj<typeof meta>

const rowOf = (canvasElement: HTMLElement) =>
  canvasElement.querySelector('.needs-you-row') as HTMLElement
const selection = (canvasElement: HTMLElement) =>
  (canvasElement.querySelector('[data-testid="story-selection"]') as HTMLElement).textContent

export const Default: Story = {
  play: async ({ canvas, canvasElement }) => {
    const row = rowOf(canvasElement)
    expect(row).not.toBeNull()
    expect(canvas.getByText('Synthetic maintenance check')).toBeTruthy()
    expect(canvas.getByText('The workspace backup has not run yet.')).toBeTruthy()
    expect(canvas.getByRole('button', { name: 'Run check' })).toBeTruthy()
    // A row with nothing else to offer has no ⋯ trigger.
    expect(canvasElement.querySelector('.needs-you-more')).toBeNull()
    // The row is the inbox's focus target, not a tab stop.
    expect(row.getAttribute('tabindex')).toBe('-1')
    expect(row.hasAttribute('data-home-notice-window')).toBe(true)
    expect(row.classList.contains('needs-you-row--error')).toBe(false)
  },
}

export const Alert: Story = {
  args: { alert: true, line: 'Synthetic permission was refused. Check the vault and try again.' },
  play: async ({ canvasElement }) => {
    const line = canvasElement.querySelector('.needs-you-line') as HTMLElement
    expect(line.getAttribute('role')).toBe('alert')
    expect(line.classList.contains('needs-you-line--alert')).toBe(true)
    expect(line.textContent).toContain('Synthetic permission was refused')
  },
}

export const ErrorTone: Story = {
  args: { tone: 'error', line: 'Synthetic failure the operator has to read.' },
  play: async ({ canvas, canvasElement }) => {
    const row = rowOf(canvasElement)
    expect(row.classList.contains('needs-you-row--error')).toBe(true)
    expect(canvasElement.querySelector('.needs-you-marker')).not.toBeNull()
    // Tone is a word for assistive tech, never colour alone.
    expect(canvas.getByText('Error:')).toBeTruthy()
    expect(canvas.queryByText('Blocking:')).toBeNull()
  },
}

export const BlockingTone: Story = {
  args: { tone: 'blocking', line: 'Synthetic step is blocking the next one.' },
  play: async ({ canvas, canvasElement }) => {
    const row = rowOf(canvasElement)
    expect(row.classList.contains('needs-you-row--blocking')).toBe(true)
    expect(row.classList.contains('needs-you-row--error')).toBe(false)
    expect(canvas.getByText('Blocking:')).toBeTruthy()
    expect(canvas.queryByText('Error:')).toBeNull()
  },
}

export const Menu: Story = {
  args: { items },
  play: async ({ canvas, canvasElement }) => {
    const trigger = canvas.getByRole('button', { name: 'More actions for Synthetic maintenance check' })
    expect(trigger.getAttribute('aria-haspopup')).toBe('menu')
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
    expect(trigger.hasAttribute('aria-controls')).toBe(false)
    expect(canvasElement.querySelector('[role="menu"]')).toBeNull()
  },
}

export const MenuOpen: Story = {
  args: { items },
  play: async ({ canvas, canvasElement, userEvent }) => {
    const trigger = canvas.getByRole('button', { name: 'More actions for Synthetic maintenance check' })
    await userEvent.click(trigger)
    await waitFor(() => expect(canvasElement.querySelector('[role="menu"]')).not.toBeNull())
    const menu = canvasElement.querySelector('[role="menu"]') as HTMLElement
    expect(trigger.getAttribute('aria-expanded')).toBe('true')
    expect(trigger.getAttribute('aria-controls')).toBe(menu.id)
    expect(menu.getAttribute('aria-label')).toBe('Actions for Synthetic maintenance check')
    expect(menu.querySelectorAll('[role="menuitem"]')).toHaveLength(3)
    // The external item is a real link that opens away from the app.
    const link = menu.querySelector('a[role="menuitem"]') as HTMLAnchorElement
    expect(link.getAttribute('target')).toBe('_blank')
    expect(link.getAttribute('rel')).toBe('noopener noreferrer')
    expect(rowOf(canvasElement).classList.contains('needs-you-row--open')).toBe(true)
    // Opening moves focus to the first enabled item.
    await waitFor(() => expect(document.activeElement).toBe(menu.querySelector('[role="menuitem"]')))
  },
}

export const MenuKeyboard: Story = {
  args: { items },
  play: async ({ canvas, canvasElement, userEvent }) => {
    const trigger = canvas.getByRole('button', { name: 'More actions for Synthetic maintenance check' })
    trigger.focus()
    await userEvent.keyboard('{Enter}')
    await waitFor(() => expect(canvasElement.querySelector('[role="menu"]')).not.toBeNull())
    const entries = [...(canvasElement.querySelectorAll<HTMLElement>('[role="menuitem"]:not([aria-disabled="true"])'))]
    await waitFor(() => expect(document.activeElement).toBe(entries[0]))
    await userEvent.keyboard('{ArrowDown}')
    expect(document.activeElement).toBe(entries[1])
    await userEvent.keyboard('{Home}')
    expect(document.activeElement).toBe(entries[0])
    await userEvent.keyboard('{End}')
    expect(document.activeElement).toBe(entries[entries.length - 1])
    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(canvasElement.querySelector('[role="menu"]')).toBeNull())
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
    expect(document.activeElement).toBe(trigger)
  },
}

export const MenuDisabledItem: Story = {
  args: { items },
  play: async ({ canvas, canvasElement, userEvent }) => {
    const trigger = canvas.getByRole('button', { name: 'More actions for Synthetic maintenance check' })
    await userEvent.click(trigger)
    await waitFor(() => expect(canvasElement.querySelector('[role="menu"]')).not.toBeNull())
    const disabledItem = canvas.getByRole('menuitem', { name: 'Not available yet' })
    expect(disabledItem.getAttribute('aria-disabled')).toBe('true')
    await userEvent.click(disabledItem)
    expect(selection(canvasElement)).toBe('Selected: none')
    expect(canvasElement.querySelector('[role="menu"]')).not.toBeNull()
    await userEvent.click(canvas.getByRole('menuitem', { name: 'Later' }))
    await waitFor(() => expect(selection(canvasElement)).toBe('Selected: later'))
    await waitFor(() => expect(canvasElement.querySelector('[role="menu"]')).toBeNull())
  },
}

export const NoticeTarget: Story = {
  args: { noticeKey: 'action:storybook-alpha:synthetic-check-1' },
  play: async ({ canvasElement }) => {
    const row = rowOf(canvasElement)
    expect(row.getAttribute('data-notice-key')).toBe('action:storybook-alpha:synthetic-check-1')
    row.focus()
    expect(document.activeElement).toBe(row)
    expect(row.getAttribute('aria-hidden')).toBeNull()
  },
}

export const Narrow: Story = {
  decorators: [() => ({ template: '<div style="max-width: 390px; container-type: inline-size; container-name: needs-you"><story /></div>' })],
  play: async ({ canvas, canvasElement }) => {
    const row = rowOf(canvasElement)
    const button = canvas.getByRole('button', { name: 'Run check' }) as HTMLButtonElement
    expect(row.getBoundingClientRect().width).toBeLessThanOrEqual(390)
    // The container query stacks the controls under the text and stretches the
    // primary to the row's width.
    expect(button.getBoundingClientRect().width).toBeGreaterThan(row.getBoundingClientRect().width * 0.8)
    expect(button.getBoundingClientRect().height).toBeGreaterThanOrEqual(32)
  },
}

// Workspace care's tone: the warning dot and a spoken word, for a row that waits
// on the owner without stopping anything.
export const AttentionTone: Story = {
  args: { tone: 'attention', title: 'Synthetic care row', line: '2 things to look at.' },
  play: async ({ canvas, canvasElement }) => {
    const row = rowOf(canvasElement)
    expect(row.classList.contains('needs-you-row--attention')).toBe(true)
    expect(canvasElement.querySelector('.needs-you-marker')).not.toBeNull()
    expect(canvas.getByText('Needs attention:')).toBeTruthy()
  },
}
