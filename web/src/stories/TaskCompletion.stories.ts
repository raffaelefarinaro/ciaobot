import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, userEvent, waitFor } from 'storybook/test'
import TaskWorkspaceSurface from '../components/TaskWorkspaceSurface.vue'
import { type Task } from '../lib/types'
import {
  assertSheetsSettled, openSheetEditor, sheetAnswers, sheetBoard,
  sheetCompleteRoute, sheetDetail, sheetDetailRoute, sheetDraft, sheetFailure,
  sheetJson, sheetList, sheetListRoute, sheetStoryView,
} from './taskSheetsFixture'

/**
 * The resolution sheet (the `task-completion` inventory row): the one
 * completion action with its optional note, its edit mode over a done task,
 * the review-ready approval, its busy and refused writes, and its cancel
 * paths. Each story mounts the production component over the declared fixture
 * routes; the sheet is real markup and real writes.
 */
const meta = {
  id: 'task-completion',
  title: 'Task sheets/Task completion',
  component: TaskWorkspaceSurface,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof TaskWorkspaceSurface>
export default meta
type Story = StoryObj<typeof meta>

function view(label: string, props: { mode?: 'board' | 'overlay'; taskId?: string } = {}, maxWidth = 640) {
  return sheetStoryView({ label, props, maxWidth })
}

const listRoute = sheetListRoute(() => sheetJson(sheetList(sheetBoard)))

/** A done task the edit state opens, with a stored resolution. */
const doneTask: Task = {
  ...sheetDraft,
  title: 'Sheet done task',
  status: 'done',
  completed_at: new Date().toISOString(),
  has_resolution: true,
}
const doneBoard: readonly Task[] = [doneTask, sheetBoard[1]!]
const doneListRoute = sheetListRoute(() => sheetJson(sheetList(doneBoard)))
const doneDetailRoute = sheetDetailRoute(doneTask.id, () => (
  sheetJson(sheetDetail(doneTask, 'Prose the list never carried.', { resolution: 'Checked the runbook end to end.' }))
))

/** The review-ready task: the sheet titles itself an approval there. */
const reviewTask: Task = {
  ...sheetDraft,
  title: 'Sheet review task',
  status: 'in_review',
  attempt_state: 'ready_for_review',
  attempt_id: 'sb-sheet-attempt-live',
  live_attempt_id: 'sb-sheet-attempt-live',
  attempt_summary: 'The agent says it ran the storybook checks end to end.',
  chat_id: 'sb-sheet-chat-live',
}
const reviewBoard: readonly Task[] = [reviewTask, sheetBoard[1]!]
const reviewListRoute = sheetListRoute(() => sheetJson(sheetList(reviewBoard)))
const reviewCompleteRoute = sheetCompleteRoute(reviewTask.id, () => (
  sheetJson(sheetDetail({ ...reviewTask, status: 'done' }))
))

/** Open the resolution sheet from a card's Done checkbox and wait for it. */
async function openCompleteSheet(
  canvasElement: HTMLElement,
  task: Task,
  approve = false,
): Promise<HTMLElement> {
  await waitFor(() => {
    expect(canvasElement.querySelector(`[data-task-id="${task.id}"] .task-title`)).toBeTruthy()
  })
  const check = canvasElement.querySelector<HTMLButtonElement>(`[data-task-id="${task.id}"] .task-check`)!
  expect(check.getAttribute('aria-label')).toBe(approve ? 'Approve and mark done' : `Mark ${task.title} done`)
  await userEvent.click(check)
  return waitFor(() => {
    expect(canvasElement.querySelector<HTMLElement>('.task-resolution-sheet')).toBeTruthy()
    return canvasElement.querySelector<HTMLElement>('.task-resolution-sheet')!
  })
}

export const Default: Story = {
  parameters: { fixture: { routes: [listRoute] } },
  render: () => view('resolution sheet'),
  play: async ({ canvasElement }) => {
    const sheet = await openCompleteSheet(canvasElement, sheetDraft)
    // The gate is the surface's own: resolutionOpen && resolutionTask.
    expect(sheet.getAttribute('role')).toBe('dialog')
    expect(sheet.getAttribute('aria-modal')).toBe('true')
    expect(sheet.textContent).toContain('Complete with resolution')
    // The note field is where focus entered the sheet.
    expect(document.activeElement).toBe(sheet.querySelector('#task-resolution-text'))
    expect([...sheet.querySelectorAll('button')].some((button) => button.textContent === 'Mark done')).toBe(true)
    await assertSheetsSettled(canvasElement)
  },
}

export const Complete: Story = {
  parameters: {
    fixture: {
      routes: [
        listRoute,
        sheetCompleteRoute(sheetDraft.id, () => sheetJson(sheetDetail({ ...sheetDraft, status: 'done' }))),
      ],
    },
  },
  render: () => view('resolution sheet, completion written'),
  play: async ({ canvasElement }) => {
    const sheet = await openCompleteSheet(canvasElement, sheetDraft)
    // A blank draft completes without a resolution: one write, resolution ''.
    await userEvent.click(sheet.querySelector('[type="submit"]')!)
    await waitFor(() => expect(canvasElement.querySelectorAll('.task-resolution-sheet')).toHaveLength(0), { timeout: 4000 })
    await assertSheetsSettled(canvasElement)
  },
}

export const Edit: Story = {
  parameters: {
    fixture: {
      routes: [
        doneListRoute,
        doneDetailRoute,
      ],
    },
  },
  // The done card draws only with the Done lane shown.
  render: () => sheetStoryView({ label: 'resolution sheet, edit mode', options: { doneShown: true } }),
  play: async ({ canvasElement }) => {
    // A done card's editor: its Resolution block reads the stored text, and
    // Edit resolution opens the sheet with that text in the field.
    const region = await openSheetEditor(canvasElement, userEvent.click, doneTask.id)
    expect(region.textContent).toContain('Checked the runbook end to end.')
    const editButton = [...region.querySelectorAll('button')].find((button) => button.textContent === 'Edit resolution')!
    await userEvent.click(editButton)
    const sheet = await waitFor(() => {
      expect(canvasElement.querySelector<HTMLElement>('.task-resolution-sheet')).toBeTruthy()
      return canvasElement.querySelector<HTMLElement>('.task-resolution-sheet')!
    }, { timeout: 4000 })
    expect(sheet.textContent).toContain('Edit resolution')
    const field = sheet.querySelector<HTMLTextAreaElement>('#task-resolution-text')!
    // The field reads the stored text first: a sheet opened empty over prose
    // that exists would let a blank Save erase it.
    expect(field.value).toBe('Checked the runbook end to end.')
    expect([...sheet.querySelectorAll('button')].some((button) => button.textContent === 'Save resolution')).toBe(true)
    await assertSheetsSettled(canvasElement)
  },
}

export const ReviewReady: Story = {
  parameters: {
    fixture: {
      routes: [
        reviewListRoute,
        reviewCompleteRoute,
      ],
    },
  },
  render: () => view('resolution sheet, approval'),
  play: async ({ canvasElement }) => {
    const sheet = await openCompleteSheet(canvasElement, reviewTask, true)
    // An approval, not a plain Done: the title names it and the sheet carries
    // the decision-reason control.
    expect(sheet.textContent).toContain('Approve with resolution')
    expect(sheet.querySelector('#task-approve-reason')).toBeTruthy()
    expect([...sheet.querySelectorAll('button')].some((button) => button.textContent === 'Approve done')).toBe(true)
    await assertSheetsSettled(canvasElement)
  },
}

export const Pending: Story = {
  parameters: {
    fixture: {
      routes: [
        listRoute,
        sheetCompleteRoute(sheetDraft.id, () => {
          return new Promise<never>(() => {})
        }),
      ],
    },
  },
  render: () => view('resolution sheet, write in flight'),
  play: async ({ canvasElement }) => {
    const sheet = await openCompleteSheet(canvasElement, sheetDraft)
    await userEvent.click(sheet.querySelector('[type="submit"]')!)
    // Busy is the block itself, not a style: the form and its way out wait.
    await waitFor(() => expect([...sheet.querySelectorAll('button')].some((button) => button.textContent === 'Saving…')).toBe(true))
    expect(sheet.querySelector<HTMLTextAreaElement>('#task-resolution-text')).toHaveProperty('disabled', true)
    expect(sheet.querySelector('[type="submit"]')).toHaveProperty('disabled', true)
    const cancel = [...sheet.querySelectorAll('button')].find((button) => button.textContent === 'Cancel') as HTMLButtonElement
    expect(cancel.disabled).toBe(true)
    expect(sheet.querySelector('.task-sheet-head .btn-icon')).toHaveProperty('disabled', true)
    await assertSheetsSettled(canvasElement)
  },
}

export const ActionError: Story = {
  parameters: {
    fixture: {
      routes: [
        // One declared list path: the board's read and the Refresh task re-read.
        sheetListRoute(sheetAnswers([
          sheetJson(sheetList(sheetBoard)),
          sheetJson(sheetList(sheetBoard)),
        ])),
        sheetCompleteRoute(sheetDraft.id, () => sheetFailure('Synthetic completion refused: the record moved on.', 409)),
        sheetDetailRoute(sheetDraft.id, () => sheetJson(sheetDetail(sheetDraft, ''))),
      ],
    },
  },
  render: () => view('resolution sheet, refused completion'),
  play: async ({ canvasElement }) => {
    const sheet = await openCompleteSheet(canvasElement, sheetDraft)
    const field = sheet.querySelector<HTMLTextAreaElement>('#task-resolution-text')!
    field.value = 'Checked the storybook end to end.'
    field.dispatchEvent(new Event('input', { bubbles: true }))
    await userEvent.click(sheet.querySelector<HTMLButtonElement>('[type="submit"]')!)
    // The refused write keeps the sheet and the draft, says the server's
    // sentence, and offers Refresh task as the way to present the new revision.
    await waitFor(() => {
      const alert = [...canvasElement.querySelectorAll('[role="alert"]')]
        .find((node) => node.textContent?.includes('Synthetic completion refused: the record moved on.'))
      return expect(alert).toBeTruthy()
    }, { timeout: 4000 })
    // Refresh task re-reads the board and the record without touching the draft.
    await userEvent.click([...sheet.querySelectorAll('button')].find((button) => button.textContent === 'Refresh task')!)
    await waitFor(() => expect(canvasElement.querySelectorAll('[role="alert"]').length).toBe(0), { timeout: 4000 })
    expect(canvasElement.querySelector<HTMLTextAreaElement>('#task-resolution-text')!.value)
      .toBe('Checked the storybook end to end.')
    await assertSheetsSettled(canvasElement)
  },
}

export const Draft: Story = {
  parameters: {
    fixture: {
      routes: [
        listRoute,
        sheetCompleteRoute(sheetDraft.id, () => sheetJson(sheetDetail({ ...sheetDraft, status: 'done' }))),
      ],
    },
  },
  render: () => view('resolution sheet, typed note'),
  play: async ({ canvasElement }) => {
    const sheet = await openCompleteSheet(canvasElement, sheetDraft)
    const field = sheet.querySelector<HTMLTextAreaElement>('#task-resolution-text')!
    field.value = 'Checked the runbook end to end.'
    field.dispatchEvent(new Event('input', { bubbles: true }))
    expect(field.value).toBe('Checked the runbook end to end.')
    // The typed draft is sent as typed, in the same POST as its completion.
    await userEvent.click(sheet.querySelector('[type="submit"]')!)
    await waitFor(() => expect(canvasElement.querySelectorAll('.task-resolution-sheet')).toHaveLength(0), { timeout: 4000 })
    await assertSheetsSettled(canvasElement)
  },
}

export const Submit: Story = {
  parameters: {
    fixture: {
      routes: [
        reviewListRoute,
        reviewCompleteRoute,
      ],
    },
  },
  render: () => view('resolution sheet, approval written'),
  play: async ({ canvasElement }) => {
    const sheet = await openCompleteSheet(canvasElement, reviewTask, true)
    const reason = sheet.querySelector<HTMLTextAreaElement>('#task-approve-reason')!
    reason.value = 'Result matches the storybook runbook.'
    reason.dispatchEvent(new Event('input', { bubbles: true }))
    // The approval closes the release loop: the write releases the linkage and
    // completes the card together.
    await userEvent.click(sheet.querySelector('[type="submit"]')!)
    await waitFor(() => expect(canvasElement.querySelectorAll('.task-resolution-sheet')).toHaveLength(0), { timeout: 4000 })
    await assertSheetsSettled(canvasElement)
  },
}

export const Cancel: Story = {
  parameters: { fixture: { routes: [listRoute] } },
  render: () => view('resolution sheet, cancel without a write'),
  play: async ({ canvasElement }) => {
    await openCompleteSheet(canvasElement, sheetDraft)
    // Escape closes without writing: resetResolution clears the slot, so the
    // ledger keeps only the board's own list read.
    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(canvasElement.querySelectorAll('.task-resolution-sheet')).toHaveLength(0))
    await assertSheetsSettled(canvasElement)
    // Reopened, the sheet is again a plain complete-mode sheet.
    const reopened = await openCompleteSheet(canvasElement, sheetDraft)
    expect(reopened.textContent).toContain('Mark done')
  },
}
