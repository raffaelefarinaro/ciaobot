import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, userEvent, waitFor } from 'storybook/test'
import TaskWorkspaceSurface from '../components/TaskWorkspaceSurface.vue'
import {
  assertSheetsSettled, menuButton, openCardMenu, pickSelect, sheetAnswers, sheetBoard,
  sheetFailure, sheetHeld, sheetJson, sheetList, sheetListRoute, sheetOrderRoute,
  sheetStoryView, sheetTask, sheetDetail,
} from './taskSheetsFixture'

/**
 * The board states of the production surface (the `components-task-workspace-surface`
 * inventory row, #3296): the first-load placeholder, a failed first read, a
 * failed refresh over retained rows, a filter hiding every task, the genuinely
 * empty board, and a card's Move down. Each story mounts the production
 * component through the #3267 boundary with the routes it declares; synthetic
 * fixtures only.
 */
const meta = {
  id: 'task-workspace-surface',
  title: 'Tasks board/TaskWorkspaceSurface',
  component: TaskWorkspaceSurface,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof TaskWorkspaceSurface>
export default meta
type Story = StoryObj<typeof meta>

function view(label: string, maxWidth = 640) {
  return sheetStoryView({ label, maxWidth })
}

const listRoute = sheetListRoute(() => sheetJson(sheetList(sheetBoard)))

/** Two To do cards in one lane, so one of them can move within it. */
const sheetThird = sheetTask({ id: 'sb-sheet-third', title: 'Sheet third task', revision: '6'.repeat(64) })
const sameLane = [sheetBoard[0]!, sheetThird]

export const FirstLoad: Story = {
  parameters: { fixture: { routes: [sheetListRoute(() => sheetHeld())] } },
  render: () => view('first load placeholder', 390),
  play: async ({ canvasElement }) => {
    const loading = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('.task-loading[role="status"]')
      expect(found).toBeTruthy()
      return found!
    })
    // The placeholder draws the loaded lane geometry: one count bar per head,
    // two loading bars per card, and no real card or title.
    expect(loading.querySelectorAll('.task-lane')).toHaveLength(3)
    expect(loading.querySelectorAll('.task-card')).toHaveLength(6)
    expect(loading.querySelector('.task-title')).toBeNull()
    expect(loading.querySelector('.task-done-toggle')).toBeTruthy()
    const widths = [...loading.querySelectorAll<HTMLElement>('.task-lane')].map((lane) => lane.getBoundingClientRect().width)
    expect(widths[0]).toBeGreaterThan(0)
    for (const width of widths) expect(width).toBeCloseTo(widths[0]!, 0)
    await assertSheetsSettled(canvasElement)
  },
}

export const LoadFailed: Story = {
  parameters: {
    fixture: { routes: [sheetListRoute(() => sheetFailure('Synthetic board read refused.', 500))] },
  },
  render: () => view('first read failed'),
  play: async ({ canvasElement }) => {
    const failed = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('.task-failed[role="alert"]')
      expect(found).toBeTruthy()
      return found!
    })
    expect(failed.textContent).toContain('Synthetic board read refused.')
    // A failed first read is never an empty board.
    expect(canvasElement.querySelector('.task-empty')).toBeNull()
    expect(canvasElement.querySelector('.task-lane')).toBeNull()
    expect([...failed.querySelectorAll('button')].some((button) => button.textContent === 'Retry')).toBe(true)
    await assertSheetsSettled(canvasElement)
  },
}

export const LoadStale: Story = {
  parameters: {
    fixture: {
      routes: [
        // The mount read answers; the refresh that a refused order write
        // triggers fails, so the rows stay on screen marked stale.
        sheetListRoute(sheetAnswers([
          sheetJson(sheetList(sameLane)),
          sheetFailure('Synthetic refresh refused.', 500),
        ])),
        sheetOrderRoute(sheetBoard[0]!.id, () => sheetFailure('Synthetic order write refused.', 500)),
      ],
    },
  },
  render: () => view('refresh failed over retained rows'),
  play: async ({ canvasElement }) => {
    await waitFor(() => expect(canvasElement.querySelector(`.task-card[data-task-id="${sheetBoard[0]!.id}"]`)).toBeTruthy())
    const menu = await openCardMenu(sheetBoard[0]!.id)
    await userEvent.click(menuButton(menu, 'Move down'))
    const stale = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('.task-stale[role="status"]')
      expect(found).toBeTruthy()
      return found!
    }, { timeout: 4000 })
    expect(stale.textContent).toContain('These are the tasks as they were last read.')
    expect([...stale.querySelectorAll('button')].some((button) => button.textContent === 'Retry')).toBe(true)
    // The retained rows are still on screen, not replaced by a failure or an empty board.
    expect(canvasElement.querySelector(`.task-card[data-task-id="${sheetBoard[0]!.id}"]`)).toBeTruthy()
    expect(canvasElement.querySelector('.task-empty')).toBeNull()
    await assertSheetsSettled(canvasElement)
  },
}

export const FilteredEmpty: Story = {
  parameters: { fixture: { routes: [listRoute] } },
  render: () => view('filter hides every task'),
  play: async ({ canvasElement }) => {
    await waitFor(() => expect(canvasElement.querySelector('#task-filter-due')).toBeTruthy())
    pickSelect(canvasElement.querySelector<HTMLSelectElement>('#task-filter-due')!, 'overdue')
    const hint = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('.task-filtered-empty')
      expect(found).toBeTruthy()
      return found!
    })
    expect(hint.textContent).toContain('No task here matches these filters. 2 tasks in storybook-sheet.')
    // Genuinely empty is not what this is: the board holds tasks the filter hides.
    expect(canvasElement.querySelector('.task-empty')).toBeNull()
    // The empty state offers the way back, and the chip row's duplicate steps aside.
    const clear = [...hint.querySelectorAll('button')].find((button) => button.textContent === 'Clear filters')!
    expect(clear).toBeTruthy()
    expect(canvasElement.querySelector('.task-filters .task-selects > button')).toBeNull()
    await userEvent.click(clear)
    await waitFor(() => expect(canvasElement.querySelector('.task-filtered-empty')).toBeNull())
    expect(canvasElement.querySelector(`.task-card[data-task-id="${sheetBoard[0]!.id}"]`)).toBeTruthy()
    await assertSheetsSettled(canvasElement)
  },
}

export const BoardEmpty: Story = {
  parameters: { fixture: { routes: [sheetListRoute(() => sheetJson(sheetList([])))] } },
  render: () => view('genuinely empty board'),
  play: async ({ canvasElement }) => {
    const hint = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('.task-empty')
      expect(found).toBeTruthy()
      return found!
    })
    expect(hint.textContent).toContain('No tasks in storybook-sheet yet.')
    expect(hint.textContent).toContain('ciao task')
    expect(canvasElement.querySelector('.task-filtered-empty')).toBeNull()
    expect(canvasElement.querySelector('.task-lanes')).toBeNull()
    await assertSheetsSettled(canvasElement)
  },
}

export const Ordering: Story = {
  parameters: {
    fixture: {
      routes: [
        sheetListRoute(() => sheetJson(sheetList(sameLane))),
        sheetOrderRoute(sheetBoard[0]!.id, () => sheetJson({
          ...sheetDetail(sheetBoard[0]!),
          order: {
            version: 2,
            sequences: { backlog: [sheetThird.id, sheetBoard[0]!.id], in_progress: [], in_review: [], done: [] },
          },
        })),
      ],
    },
  },
  render: () => view('card Move down, same lane'),
  play: async ({ canvasElement }) => {
    await waitFor(() => expect(canvasElement.querySelector(`.task-card[data-task-id="${sheetBoard[0]!.id}"]`)).toBeTruthy())
    const laneIds = () => [...canvasElement.querySelectorAll<HTMLElement>('[data-lane="backlog"] .task-card[data-task-id]')]
      .map((card) => card.dataset.taskId)
    expect(laneIds()).toEqual([sheetBoard[0]!.id, sheetThird.id])
    const menu = await openCardMenu(sheetBoard[0]!.id)
    await userEvent.click(menuButton(menu, 'Move down'))
    // The answer carries the order: the moved card now sits below its neighbour.
    await waitFor(() => expect(laneIds()).toEqual([sheetThird.id, sheetBoard[0]!.id]), { timeout: 4000 })
    await assertSheetsSettled(canvasElement)
  },
}
