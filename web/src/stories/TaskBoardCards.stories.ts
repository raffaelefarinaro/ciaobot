import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, userEvent, waitFor } from 'storybook/test'
import TaskWorkspaceSurface from '../components/TaskWorkspaceSurface.vue'
import {
  assertSheetsSettled, sheetAttemptActionRoute, sheetBoard, sheetDoneBoard, sheetHeld, sheetJson,
  sheetList, sheetListRoute, sheetStoryView, sheetTask,
} from './taskSheetsFixture'

/**
 * The card region of the production board (the `task-board-cards` inline
 * row, #3296): the lanes and their heads, the card anatomy, the filter-drawn
 * single group, the Done tab, the first-load placeholder cards and a card with
 * a write in flight. The cards render inside the production
 * TaskWorkspaceSurface through the #3267 boundary; synthetic fixtures only.
 */
const meta = {
  id: 'task-board-cards',
  title: 'Tasks board/Task board cards',
  component: TaskWorkspaceSurface,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof TaskWorkspaceSurface>
export default meta
type Story = StoryObj<typeof meta>

function view(label: string, options: { doneShown?: boolean } = {}, maxWidth = 640) {
  return sheetStoryView({ label, options, maxWidth })
}

const listRoute = sheetListRoute(() => sheetJson(sheetList(sheetBoard)))

/** A card that is overdue and whose agent is running: the meta line and the band. */
const runningCard = sheetTask({
  id: 'sb-card-running',
  title: 'Card running task',
  status: 'in_progress',
  due: '2020-01-02',
  attempt_id: 'sb-card-attempt-running',
  attempt_state: 'running',
  attempt_summary: 'Synthetic agent note.',
  chat_id: 'sb-card-chat',
  revision: '7'.repeat(64),
})

/** A card whose agent needs the user: the band names it and offers the chat. */
const needsCard = sheetTask({
  id: 'sb-card-needs',
  title: 'Card needs task',
  status: 'in_review',
  attempt_id: 'sb-card-attempt-needs',
  attempt_state: 'needs_you',
  chat_id: 'sb-card-chat',
  revision: '8'.repeat(64),
})

/** A card whose last run failed: its band offers Resume, which the busy story holds. */
const failedCard = sheetTask({
  id: 'sb-busy-task',
  title: 'Busy task',
  status: 'in_progress',
  attempt_id: 'sb-busy-attempt',
  attempt_state: 'failed',
  attempt_detail: 'Synthetic run stopped early.',
  chat_id: 'sb-busy-chat',
  revision: '9'.repeat(64),
})

function laneOf(canvasElement: HTMLElement, status: string): HTMLElement {
  return canvasElement.querySelector<HTMLElement>(`.task-lane[data-lane="${status}"]`)!
}

export const Default: Story = {
  parameters: { fixture: { routes: [listRoute] } },
  render: () => view('board cards'),
  play: async ({ canvasElement }) => {
    const title = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('[data-task-id="sb-sheet-task"] .task-title')
      expect(found).toBeTruthy()
      return found!
    })
    expect(title.textContent).toBe('Sheet draft task')
    // Each card's check is its own Done gesture, labelled with the card's title.
    const check = canvasElement.querySelector<HTMLButtonElement>('[data-task-id="sb-sheet-task"] .task-check')!
    expect(check.getAttribute('aria-label')).toBe('Mark Sheet draft task done')
    expect(check.disabled).toBe(false)
    expect(canvasElement.querySelector('.task-lede')?.textContent).toContain('2 open of 2 in storybook-sheet.')
    await assertSheetsSettled(canvasElement)
  },
}

export const Lanes: Story = {
  parameters: { fixture: { routes: [listRoute] } },
  render: () => view('lanes side by side'),
  play: async ({ canvasElement }) => {
    await waitFor(() => expect(canvasElement.querySelector('[data-task-id="sb-sheet-task"]')).toBeTruthy())
    // The unfiltered board is its lanes side by side, Done behind its tab.
    const board = canvasElement.querySelector<HTMLElement>('.task-lanes')!
    expect(board.classList.contains('task-lanes--columns')).toBe(true)
    expect(canvasElement.querySelectorAll('.task-lane')).toHaveLength(3)
    expect(laneOf(canvasElement, 'backlog').querySelector('.task-lane-label')?.textContent).toBe('To do')
    expect(laneOf(canvasElement, 'in_progress').querySelector('.task-lane-count .badge')?.textContent).toBe('1')
    // An empty lane says so in its own words.
    expect(laneOf(canvasElement, 'in_review').querySelector('.task-lane-empty')?.textContent).toBe('Nothing here.')
    const doneTab = canvasElement.querySelector<HTMLButtonElement>('.task-done-toggle')!
    expect(doneTab.getAttribute('aria-expanded')).toBe('false')
    await assertSheetsSettled(canvasElement)
  },
}

export const Cards: Story = {
  parameters: { fixture: { routes: [sheetListRoute(() => sheetJson(sheetList([runningCard, needsCard])))] } },
  render: () => view('card meta, agent band and note'),
  play: async ({ canvasElement }) => {
    await waitFor(() => expect(canvasElement.querySelector(`[data-task-id="${runningCard.id}"]`)).toBeTruthy())
    // An overdue date is red and says so in words for a screen reader.
    const due = canvasElement.querySelector<HTMLElement>(`[data-task-id="${runningCard.id}"] .task-due`)!
    expect(due.classList.contains('task-due--over')).toBe(true)
    expect(due.querySelector('.visually-hidden')?.textContent).toBe('Overdue: ')
    // The agent's words on where it got to, and its state in words with one action.
    expect(canvasElement.querySelector(`[data-task-id="${runningCard.id}"] .task-agent-note`)?.textContent).toBe('Synthetic agent note.')
    expect(canvasElement.querySelector(`[data-task-id="${runningCard.id}"] .task-agent-status`)?.textContent).toContain('Agent working')
    const needs = canvasElement.querySelector<HTMLElement>(`[data-task-id="${needsCard.id}"] .task-agent-band`)!
    expect(needs.classList.contains('task-agent-band--needs')).toBe(true)
    expect(needs.querySelector('.task-agent-status')?.textContent).toContain('Needs you')
    expect(needs.querySelector('.task-agent-action')?.textContent).toContain('Open chat')
    await assertSheetsSettled(canvasElement)
  },
}

export const Filters: Story = {
  parameters: { fixture: { routes: [listRoute] } },
  render: () => view('status filter drawn as one group'),
  play: async ({ canvasElement }) => {
    await waitFor(() => expect(canvasElement.querySelector('[data-task-id="sb-sheet-task"]')).toBeTruthy())
    const chip = [...canvasElement.querySelectorAll<HTMLButtonElement>('.task-chips button')]
      .find((button) => button.textContent?.trim().startsWith('In progress'))!
    await userEvent.click(chip)
    await waitFor(() => expect(chip.getAttribute('aria-pressed')).toBe('true'))
    // One status is one group at full width, and the way back is offered.
    expect(canvasElement.querySelector('.task-lanes')?.classList.contains('task-lanes--list')).toBe(true)
    expect(canvasElement.querySelectorAll('.task-lane')).toHaveLength(1)
    expect(canvasElement.querySelector('[data-task-id="sb-sheet-task"]')).toBeNull()
    expect(canvasElement.querySelector('[data-task-id="sb-sheet-second"]')).toBeTruthy()
    const clear = [...canvasElement.querySelectorAll<HTMLButtonElement>('.task-selects button')]
      .find((button) => button.textContent === 'Clear filters')
    expect(clear).toBeTruthy()
    await assertSheetsSettled(canvasElement)
  },
}

export const Done: Story = {
  parameters: { fixture: { routes: [sheetListRoute(() => sheetJson(sheetList(sheetDoneBoard)))] } },
  render: () => view('Done lane drawn', { doneShown: true }),
  play: async ({ canvasElement }) => {
    await waitFor(() => expect(laneOf(canvasElement, 'done')).toBeTruthy())
    const done = laneOf(canvasElement, 'done')
    expect(done.querySelectorAll('.task-card--done')).toHaveLength(2)
    expect(canvasElement.querySelector('.task-done-toggle')).toBeNull()
    // Hide returns the lane to its tab, and the tab says it is closed.
    const hide = done.querySelector<HTMLButtonElement>('button[aria-label="Hide done tasks"]')!
    await userEvent.click(hide)
    const tab = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLButtonElement>('.task-done-toggle')
      expect(found).toBeTruthy()
      return found!
    })
    expect(tab.getAttribute('aria-expanded')).toBe('false')
    expect(laneOf(canvasElement, 'done')).toBeFalsy()
    await assertSheetsSettled(canvasElement)
  },
}

export const Loading: Story = {
  parameters: { fixture: { routes: [sheetListRoute(() => sheetHeld())] } },
  render: () => view('placeholder cards while first load is in flight', {}, 390),
  play: async ({ canvasElement }) => {
    const loading = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('.task-loading')
      expect(found).toBeTruthy()
      return found!
    })
    // Two loading bars per placeholder card, a count bar per lane head, no real title.
    expect(loading.querySelectorAll('.task-card')).toHaveLength(6)
    expect(loading.querySelectorAll('.task-card .task-loading-bar')).toHaveLength(12)
    expect(loading.querySelectorAll('.task-lane-head .task-loading-bar--count')).toHaveLength(3)
    expect(loading.querySelector('.task-title')).toBeNull()
    await assertSheetsSettled(canvasElement)
  },
}

export const Busy: Story = {
  parameters: {
    fixture: {
      routes: [
        sheetListRoute(() => sheetJson(sheetList([failedCard]))),
        // The resume is in flight and never answers: the card is busy.
        sheetAttemptActionRoute(failedCard.id, 'sb-busy-attempt', 'resume', () => sheetHeld()),
      ],
    },
  },
  render: () => view('card with a write in flight'),
  play: async ({ canvasElement }) => {
    const action = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLButtonElement>(`[data-task-id="${failedCard.id}"] .task-agent-action`)
      expect(found).toBeTruthy()
      return found!
    })
    expect(action.textContent).toContain('Resume')
    const check = canvasElement.querySelector<HTMLButtonElement>(`[data-task-id="${failedCard.id}"] .task-check`)!
    expect(check.disabled).toBe(false)
    await userEvent.click(action)
    // Busy is the block itself: the card's check and the band's action are disabled.
    await waitFor(() => expect(check.disabled).toBe(true))
    expect(action.disabled).toBe(true)
    await assertSheetsSettled(canvasElement)
  },
}
