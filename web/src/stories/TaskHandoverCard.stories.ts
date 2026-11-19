import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, userEvent } from 'storybook/test'
import TaskHandoverCard from '../components/TaskHandoverCard.vue'
import { parseTaskHandover } from '../lib/taskHandover'
import type { TaskHandover } from '../lib/taskHandover'

/**
 * TaskHandoverCard (the `components-task-handover-card` inventory row,
 * #3312): the first message of a delegated chat drawn as what was handed
 * over. Props only — `handover`, `prompt` and `knownPaths` — so these
 * stories render the production component standalone with a synthetic fenced
 * prompt, parsed with the production `parseTaskHandover` exactly as ChatPanel
 * does before it hands the card its props.
 */
const PROMPT = [
  'Work on the storybook alpha task record.',
  '',
  'Title: Storybook alpha delegated task',
  '',
  '<task-board-task>',
  'Inspect the storybook alpha checklist and update the sample note.',
  '</task-board-task>',
  '',
  'Follow the workspace rules.',
].join('\n')

const HANDOVER = parseTaskHandover(PROMPT)!
const handover = (overrides: Partial<TaskHandover> = {}): TaskHandover => ({ ...HANDOVER, ...overrides })

const meta = {
  id: 'task-handover-card',
  title: 'Chat leaves that survive/TaskHandoverCard',
  component: TaskHandoverCard,
  tags: ['!autodocs'],
  args: { handover: HANDOVER, prompt: PROMPT },
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
  render: (args) => ({
    components: { TaskHandoverCard },
    setup() {
      return { args }
    },
    template: '<TaskHandoverCard v-bind="args" />',
  }),
} satisfies Meta<typeof TaskHandoverCard>
export default meta
type Story = StoryObj<typeof meta>

// Full read surface: label, h3 title, sanitised description and the closed
// native disclosure for the raw prompt.
export const Default: Story = {
  play: async ({ canvasElement }) => {
    const section = canvasElement.querySelector('section[aria-label="Task handed over"]')
    expect(section).toBeTruthy()
    expect(canvasElement.querySelector('.task-handover-label')?.textContent).toBe('Task handed over')
    expect(canvasElement.querySelector('.task-handover-title')?.textContent).toBe('Storybook alpha delegated task')
    expect(canvasElement.querySelector('.task-handover-description')?.textContent)
      .toContain('Inspect the storybook alpha checklist and update the sample note.')
    expect(canvasElement.querySelector('.task-handover-empty')).toBeNull()
    const details = canvasElement.querySelector('details.task-handover-raw') as HTMLDetailsElement
    expect(details.open).toBe(false)
    expect(canvasElement.querySelector('.task-handover-raw summary')?.textContent).toBe('Show what the agent received')
  },
}

// The disclosure is the card's one interaction: opening it shows the prompt
// prop verbatim, and the pre becomes visible geometry.
export const OpenRaw: Story = {
  play: async ({ canvasElement }) => {
    const details = canvasElement.querySelector('details.task-handover-raw') as HTMLDetailsElement
    const summary = canvasElement.querySelector('.task-handover-raw summary') as HTMLElement
    expect(details.open).toBe(false)
    await userEvent.click(summary)
    expect(details.open).toBe(true)
    const pre = canvasElement.querySelector('.task-handover-raw pre') as HTMLElement
    expect(pre.textContent).toBe(PROMPT)
    expect(pre.getBoundingClientRect().height).toBeGreaterThan(0)
  },
}

// The v-if handover.title branch hides the h3; label, description and the
// disclosure remain.
export const NoTitle: Story = {
  args: { handover: handover({ title: '' }) },
  play: async ({ canvasElement }) => {
    expect(canvasElement.querySelector('.task-handover-title')).toBeNull()
    expect(canvasElement.querySelector('.task-handover-label')?.textContent).toBe('Task handed over')
    expect(canvasElement.querySelector('.task-handover-description')?.textContent)
      .toContain('Inspect the storybook alpha checklist and update the sample note.')
  },
}

// The v-else of descriptionHtml renders the "No description." line instead.
export const NoDescription: Story = {
  args: { handover: handover({ description: '' }) },
  play: async ({ canvasElement }) => {
    expect(canvasElement.querySelector('.task-handover-description')).toBeNull()
    expect(canvasElement.querySelector('.task-handover-empty')?.textContent).toBe('No description.')
    expect(canvasElement.querySelector('.task-handover-title')?.textContent).toBe('Storybook alpha delegated task')
  },
}

// The knownPaths prop is what linksifies: the path-like mention and the
// touched bare basename become file links, while an untouched bare name
// stays plain text.
export const FileLinks: Story = {
  args: {
    handover: handover({
      description: 'Read storybook/alpha/notes.md first, then beta-notes.md; leave scratch-pad.txt for later.',
    }),
    knownPaths: ['storybook/alpha/notes.md', 'beta-notes.md'],
  },
  play: async ({ canvasElement }) => {
    const description = canvasElement.querySelector('.task-handover-description') as HTMLElement
    const links = [...description.querySelectorAll('a.file-link')]
    expect(links.map((link) => link.getAttribute('data-file-path'))).toEqual([
      'storybook/alpha/notes.md',
      'beta-notes.md',
    ])
    expect(description.textContent).toContain('scratch-pad.txt')
    expect([...description.querySelectorAll('a')].map((link) => link.getAttribute('data-file-path')))
      .not.toContain('scratch-pad.txt')
  },
}

// Keyboard path: Tab lands the focus on the summary with a visible focus
// ring. (The runner's keyboard events reach the summary but do not run its
// native Enter activation in headless Chromium, so the toggle itself is
// proven by OpenRaw via click; a real browser toggles the disclosure when
// the focused summary receives Enter.)
export const KeyboardFocus: Story = {
  play: async ({ canvasElement }) => {
    const summary = canvasElement.querySelector('.task-handover-raw summary') as HTMLElement
    await userEvent.tab()
    expect(document.activeElement).toBe(summary)
    expect(summary.matches(':focus-visible')).toBe(true)
  },
}

// At phone width the card spans the column, the long synthetic token wraps
// through overflow-wrap and nothing overflows the container.
export const Narrow: Story = {
  decorators: [() => ({ template: '<div style="max-width: 390px"><story /></div>' })],
  args: {
    handover: handover({
      title: 'Storybook gamma brief holding one long storybook-gamma-token-for-narrow-reflow',
    }),
  },
  play: async ({ canvasElement }) => {
    const section = canvasElement.querySelector('section[aria-label="Task handed over"]') as HTMLElement
    expect(section.getBoundingClientRect().width).toBeLessThanOrEqual(390)
    expect(canvasElement.scrollWidth).toBeLessThanOrEqual(canvasElement.clientWidth)
    const title = canvasElement.querySelector('.task-handover-title') as HTMLElement
    expect(getComputedStyle(title).overflowWrap).toBe('anywhere')
  },
}
