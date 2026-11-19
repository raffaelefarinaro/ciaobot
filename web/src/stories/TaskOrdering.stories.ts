import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, userEvent, waitFor } from 'storybook/test'
import TaskWorkspaceSurface from '../components/TaskWorkspaceSurface.vue'
import { type Task } from '../lib/types'
import {
  assertSheetsSettled, menuButton, openCardMenu, pickSelect, sheetBoard,
  sheetDetail, sheetDoneBoard, sheetJson, sheetList, sheetListRoute,
  sheetOrderRoute, sheetPatchRoute, sheetStoryView,
} from './taskSheetsFixture'

/**
 * The move sheet (the `task-ordering` inventory row): the column-and-position
 * placement the card menu's Move… opens, its cross-lane write, the Done
 * branches (append when the Done lane is not drawn, position when it is), and
 * the order-file error the drawn menu blocks with. Production component;
 * declared reads and writes; the reka-ui menu's portal is queried from
 * `document.body`.
 */
const meta = {
  id: 'task-ordering',
  title: 'Task sheets/Task ordering',
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

/** The declared writes of the Done branches. */
const doneCompleteRoute = {
  method: 'POST' as const,
  path: '/api/tasks/sb-sheet-task/complete',
  respond: () => sheetJson(sheetDetail({ ...sheetBoard[0]!, status: 'done' })),
}

/** Open the move sheet from a card's menu. */
async function openMoveSheet(canvasElement: HTMLElement, cardId = 'sb-sheet-task'): Promise<{
  sheet: HTMLElement
  laneField: HTMLSelectElement
  positionField: HTMLSelectElement
}> {
  const menu = await openCardMenu(cardId)
  await userEvent.click(menuButton(menu, 'Move…'))
  const sheet = await waitFor(() => {
    expect(canvasElement.querySelector('.task-move-sheet')).toBeTruthy()
    return canvasElement.querySelector<HTMLElement>('.task-move-sheet')!
  })
  const laneField = sheet.querySelector<HTMLSelectElement>('#task-move-lane')!
  const positionField = sheet.querySelector<HTMLSelectElement>('#task-move-position')!
  await waitFor(() => expect(positionField.options.length).toBeGreaterThan(0))
  return { sheet, laneField, positionField }
}

export const Default: Story = {
  parameters: { fixture: { routes: [listRoute] } },
  render: () => view('move sheet'),
  play: async ({ canvasElement }) => {
    const { sheet, laneField, positionField } = await openMoveSheet(canvasElement)
    // The sheet is the menu's own dialog: the task's column preselected, the
    // landing rows its lane offers, and Escape's write-free way out.
    const heading = sheet.querySelector('#task-move-title')!
    expect(heading.textContent).toBe('Move Sheet draft task')
    expect(laneField.value).toBe('backlog')
    expect([...positionField.options].map((option) => option.textContent)).toEqual(['Top'])
    expect([...sheet.querySelectorAll('button')].some((button) => button.textContent === 'Move')).toBe(true)
    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(canvasElement.querySelectorAll('.task-move-sheet')).toHaveLength(0))
    await assertSheetsSettled(canvasElement)
  },
}

export const Move: Story = {
  parameters: {
    fixture: {
      routes: [
        listRoute,
        sheetPatchRoute('sb-sheet-task', () => sheetJson(sheetDetail({
          ...sheetBoard[0]!,
          status: 'in_progress',
        }))),
      ],
    },
  },
  render: () => view('move sheet, cross-lane write'),
  play: async ({ canvasElement }) => {
    const { sheet, laneField, positionField } = await openMoveSheet(canvasElement)
    // Another column is another set of landing rows: its top, then after each.
    pickSelect(laneField, 'in_progress')
    await waitFor(() => {
      const labels = [...positionField.options].map((option) => option.textContent)
      expect(labels).toEqual(['Top', 'After Sheet ship task'])
    })
    expect(positionField.selectedIndex).toBe(0)
    // One write carries the status and the placement together.
    await userEvent.click([...sheet.querySelectorAll('button')].find((button) => button.textContent === 'Move')!)
    await waitFor(() => expect(canvasElement.querySelectorAll('.task-move-sheet')).toHaveLength(0), { timeout: 4000 })
    await waitFor(() => {
      const node = canvasElement.querySelector('[data-lane="in_progress"] [data-task-id="sb-sheet-task"]')
      expect(node).toBeTruthy()
    }, { timeout: 4000 })
    await assertSheetsSettled(canvasElement)
  },
}

export const DoneAppend: Story = {
  parameters: {
    fixture: {
      routes: [listRoute, doneCompleteRoute],
    },
  },
  render: () => view('move sheet, Done not drawn'),
  play: async ({ canvasElement }) => {
    const { sheet, laneField, positionField } = await openMoveSheet(canvasElement)
    // Done is not drawn (no done rows this board holds): the completion
    // appends when confirmed, and the note says so.
    pickSelect(laneField, 'done')
    await waitFor(() => expect(sheet.textContent).toContain('This opens the completion sheet; the completed card appends to Done.'))
    expect([...positionField.options].map((option) => option.textContent)).toEqual(['Appends when completed'])
    // Confirm opens the completion sheet and writes nothing yet.
    await userEvent.click([...sheet.querySelectorAll('button')].find((button) => button.textContent === 'Move')!)
    await waitFor(() => expect(canvasElement.querySelector('.task-resolution-sheet')).toBeTruthy(), { timeout: 4000 })
    await assertSheetsSettled(canvasElement)
  },
}

export const DonePosition: Story = {
  parameters: {
    fixture: {
      routes: [
        sheetListRoute(() => sheetJson(sheetList(sheetDoneBoard))),
        sheetPatchRoute('sb-sheet-archived', () => sheetJson(sheetDetail(sheetDoneBoard[1] as Task))),
      ],
    },
  },
  render: () => view('move sheet, Done drawn', { doneShown: true }),
  play: async ({ canvasElement }) => {
    // With the Done lane drawn the move sheet keeps the chosen position, and
    // the note says the placement is saved with the confirmation.
    const { sheet, laneField, positionField } = await openMoveSheet(canvasElement, 'sb-sheet-archived')
    expect(laneField.value).toBe('done')
    await waitFor(() => expect(sheet.textContent).toContain('This opens the completion sheet; the chosen position is saved with the confirmation.'))
    const labels = [...positionField.options].map((option) => option.textContent)
    expect(labels).toContain('Top')
    expect(labels).toContain('After Sheet draft task')
    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(canvasElement.querySelectorAll('.task-move-sheet')).toHaveLength(0))
    await assertSheetsSettled(canvasElement)
  },
}

export const OrderError: Story = {
  parameters: {
    fixture: {
      routes: [
        sheetListRoute(() => sheetJson(sheetList(sheetBoard, { order_error: 'Synthetic order failure: the order note could not be read.' }))),
        sheetOrderRoute('sb-sheet-task', () => sheetJson(sheetDetail(sheetBoard[0] as Task))),
      ],
    },
  },
  render: () => view('move sheet, order file unreadable'),
  play: async ({ canvasElement }) => {
    // The order file could not be read: cards keep their status moves but
    // cannot be placed, and the menu's Move actions say why.
    await waitFor(() => {
      const notice = canvasElement.querySelector('.task-order-notice')
      expect(notice).toBeTruthy()
      return expect(notice!.textContent).toContain('Synthetic order failure')
    })
    const menu = await openCardMenu('sb-sheet-task')
    const up = menuButton(menu, 'Move up')
    expect(up.getAttribute('title')).toContain('Synthetic order failure')
    expect(up.disabled).toBe(true)
    expect(menuButton(menu, 'Move down').disabled).toBe(true)
    expect(menuButton(menu, 'Move…').disabled).toBe(true)
    await assertSheetsSettled(canvasElement)
  },
}
