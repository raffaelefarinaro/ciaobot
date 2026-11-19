import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, userEvent, waitFor } from 'storybook/test'
import TaskWorkspaceSurface from '../components/TaskWorkspaceSurface.vue'
import { type Task, type TaskAttempt } from '../lib/types'
import {
  assertSheetsSettled, openSheetEditor, sheetAttempts, sheetBoard,
  sheetAttemptsRoute, sheetDelegateRoute, sheetDetail, sheetDetailRoute,
  sheetDraft, sheetFailure, sheetHeld, sheetJson, sheetList, sheetListRoute,
  sheetStoryView,
} from './taskSheetsFixture'

/**
 * The Agent block and the delegate preview (the `task-delegation` inventory
 * row): what the editor's Agent section says from the row's attempt, the
 * delegate preview it opens over the editor, the preview's description read,
 * its confirm modes and its busy write. Production component, declared
 * fixture routes, synthetic attempts only.
 */
const meta = {
  id: 'task-delegation',
  title: 'Task sheets/Task delegation',
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

/** A live attempt the board knows: running, in its chat. */
const liveAttempt: TaskAttempt = {
  attempt_id: 'sb-sheet-attempt-live',
  task_id: 'sb-sheet-task',
  task_revision: sheetDraft.revision,
  chat_id: 'sb-sheet-chat-live',
  state: 'running',
  created_at: '2026-03-01T09:00:00+00:00',
  updated_at: '2026-03-01T09:05:00+00:00',
  ended_at: '',
  detail: '',
  outcome: '',
  summary: 'Started the storybook runbook sweep.',
  released: false,
  live: true,
}
const liveTask: Task = {
  ...sheetDraft,
  title: 'Sheet live task',
  status: 'in_progress',
  attempt_state: 'running',
  attempt_id: liveAttempt.attempt_id,
  live_attempt_id: liveAttempt.attempt_id,
  attempt_summary: 'Started the storybook runbook sweep.',
  chat_id: liveAttempt.chat_id,
}
const liveListRoute = sheetListRoute(() => sheetJson(sheetList([liveTask, sheetBoard[1]!])))
const liveDetailRoute = sheetDetailRoute(liveTask.id, () => sheetJson(sheetDetail(liveTask, 'Prose the list never carried.')))
const liveAttemptsRoute = sheetAttemptsRoute(liveTask.id, () => (
  sheetJson({ workspace: 'storybook-sheet', task: liveTask, attempts: [liveAttempt] })
))

/** A settled attempt still linked: the resume case; archived chat: the retry case. */
const settledTask: Task = {
  ...sheetDraft,
  title: 'Sheet settled task',
  status: 'in_progress',
  attempt_state: 'failed',
  attempt_id: liveAttempt.attempt_id,
  attempt_summary: '',
  attempt_detail: 'The provider hung up on the storybook request.',
  chat_id: liveAttempt.chat_id,
}
/** The settled attempt the history lists for the settled and archived rows. */
const settledAttempt: TaskAttempt = { ...liveAttempt, state: 'failed', live: false, released: true }
const settledListRoute = sheetListRoute(() => sheetJson(sheetList([settledTask, sheetBoard[1]!])))
const settledDetailRoute = sheetDetailRoute(settledTask.id, () => sheetJson(sheetDetail(settledTask, 'Prose the list never carried.')))
const archivedSettledTask: Task = { ...settledTask, title: 'Sheet archived task', chat_archived: true }
const archivedSettledListRoute = sheetListRoute(() => sheetJson(sheetList([archivedSettledTask, sheetBoard[1]!])))
const archivedSettledDetailRoute = sheetDetailRoute(archivedSettledTask.id, () => sheetJson(sheetDetail(archivedSettledTask, 'Prose the list never carried.')))

/** A result waiting for review, the way the sheet's approval variant is keyed. */
const reviewTask: Task = {
  ...sheetDraft,
  title: 'Sheet review task',
  status: 'in_review',
  attempt_state: 'ready_for_review',
  attempt_id: liveAttempt.attempt_id,
  live_attempt_id: liveAttempt.attempt_id,
  attempt_summary: 'The agent says it ran the storybook checks end to end.',
  chat_id: liveAttempt.chat_id,
}
const reviewListRoute = sheetListRoute(() => sheetJson(sheetList([reviewTask, sheetBoard[1]!])))
const reviewDetailRoute = sheetDetailRoute(reviewTask.id, () => sheetJson(sheetDetail(reviewTask, 'Prose the list never carried.')))
const reviewAttemptsRoute = sheetAttemptsRoute(reviewTask.id, () => (
  sheetJson({
    workspace: 'storybook-sheet',
    task: reviewTask,
    attempts: [{ ...liveAttempt, state: 'ready_for_review' }],
  })
))

/** The linked-chats variant of the plain task. */
const linkedTask: Task = { ...sheetDraft, title: 'Sheet linked task', linked_chats: ['sb-sheet-chat-link'] }
const linkedListRoute = sheetListRoute(() => sheetJson(sheetList([linkedTask, sheetBoard[1]!])))
const linkedDetailRoute = sheetDetailRoute(linkedTask.id, () => sheetJson(sheetDetail(linkedTask, '')))
const linkedChatsRoute = {
  method: 'POST' as const,
  path: `/api/tasks/${linkedTask.id}/chats`,
  respond: () => sheetJson(sheetDetail({ ...linkedTask, linked_chats: [] }, '')),
}

async function openAgent(canvasElement: HTMLElement, taskId: string) {
  const region = await openSheetEditor(canvasElement, userEvent.click, taskId)
  return waitFor(() => {
    const agent = region.querySelector<HTMLElement>('.task-delegate')
    expect(agent).toBeTruthy()
    return agent!
  })
}

async function openDelegatePreview(canvasElement: HTMLElement, taskId: string) {
  const region = await openSheetEditor(canvasElement, userEvent.click, taskId)
  await waitFor(() => expect(region.querySelector('.task-delegate')).toBeTruthy())
  await userEvent.click(region.querySelector('.task-delegate-go')!)
  return waitFor(() => {
    expect(canvasElement.querySelector('.task-delegate-sheet')).toBeTruthy()
    return canvasElement.querySelector<HTMLElement>('.task-delegate-sheet')!
  })
}

export const Default: Story = {
  parameters: {
    fixture: {
      routes: [
        listRoute,
        sheetDetailRoute(sheetDraft.id, () => sheetJson(sheetDetail(sheetDraft, 'Prose the list never carried.'))),
      ],
    },
  },
  render: () => view('agent block, never delegated'),
  play: async ({ canvasElement }) => {
    const agent = await openAgent(canvasElement, sheetDraft.id)
    // A task nobody delegated says in one line what delegating does, and its
    // control opens the preview in that mode.
    expect(agent.textContent).toContain('Delegating starts a new chat in')
    expect(agent.textContent).toContain('the agent asks in that chat')
    const go = agent.querySelector('.task-delegate-go')!
    expect(go.textContent).toContain('Delegate to agent')
    expect(go).toHaveProperty('disabled', false)
    await assertSheetsSettled(canvasElement)
  },
}

export const Delegation: Story = {
  parameters: {
    fixture: {
      routes: [
        liveListRoute,
        liveDetailRoute,
        liveAttemptsRoute,
      ],
    },
  },
  render: () => view('agent block, live attempt'),
  play: async ({ canvasElement }) => {
    const agent = await openAgent(canvasElement, liveTask.id)
    // The badge is the record's own state, and the live gestures are Stop and
    // Detach — never a resume offered for a state the server refuses.
    expect(agent.textContent).toContain('Running')
    const actions = agent.querySelector('.task-delegate-actions')!
    const labels = [...actions.querySelectorAll('button')].map((button) => button.textContent)
    expect(labels).toContain('Stop')
    expect(labels).toContain('Detach')
    // The history re-reads on every open: one live attempt at this task.
    await waitFor(() => expect(agent.querySelectorAll('.task-history-row').length).toBeGreaterThan(0))
    expect(agent.querySelector('.task-history-row')!.textContent).toContain('Running')
    await assertSheetsSettled(canvasElement)
  },
}

export const ReviewReady: Story = {
  parameters: {
    fixture: {
      routes: [
        reviewListRoute,
        reviewDetailRoute,
        reviewAttemptsRoute,
      ],
    },
  },
  render: () => view('agent block, result ready for review'),
  play: async ({ canvasElement }) => {
    const agent = await openAgent(canvasElement, reviewTask.id)
    // A reviewed result reads to the chat first; Approve Done is the one
    // gesture that completes it, and the run gestures stay in the card menu.
    expect(agent.querySelector('.task-review')!.textContent).toContain('The agent says this is done.')
    const labels = [...agent.querySelectorAll('.task-delegate-actions button')].map((button) => button.textContent)
    expect(labels).toContain('Approve Done')
    expect(labels).toContain('Reject…')
    expect(agent.querySelector('.task-delegate-go')!.textContent).toContain('Open chat')
    await assertSheetsSettled(canvasElement)
  },
}

export const Handover: Story = {
  parameters: {
    fixture: {
      routes: [
        listRoute,
        sheetDetailRoute(sheetDraft.id, () => sheetJson(sheetDetail(sheetDraft, 'Prose the list never carried.'))),
      ],
    },
  },
  render: () => view('delegate preview, first hand-over'),
  play: async ({ canvasElement }) => {
    const sheet = await openDelegatePreview(canvasElement, sheetDraft.id)
    // The decision, stated before it is made, and the description the read
    // brought back — the reason a list row alone is never enough.
    expect(sheet.textContent).toContain('Hand this task to the agent')
    expect(sheet.textContent).toContain('Attendance')
    expect(sheet.textContent).toContain('Attended')
    expect(sheet.textContent).toContain('The description below is quoted into the chat as the task.')
    expect(sheet.querySelector('.task-preview')).toBeTruthy()
    expect(sheet.querySelector('#task-delegate-instructions')).toBeTruthy()
    expect([...sheet.querySelectorAll('button')].some((button) => button.textContent === 'Delegate')).toBe(true)
    await assertSheetsSettled(canvasElement)
  },
}

export const PreviewResume: Story = {
  parameters: {
    fixture: {
      routes: [
        settledListRoute,
        settledDetailRoute,
        sheetAttemptsRoute(settledTask.id, () => sheetJson(sheetAttempts(settledTask, [settledAttempt]))),
      ],
    },
  },
  render: () => view('delegate preview, settled attempt'),
  play: async ({ canvasElement }) => {
    const sheet = await openDelegatePreview(canvasElement, settledTask.id)
    // Resume is its own gesture, and the sheet names both ways out of a
    // settled attempt.
    expect(sheet.textContent).toContain('Resuming continues that attempt in its')
    const labels = [...sheet.querySelectorAll('.form-actions button')].map((button) => button.textContent)
    expect(labels).toContain('Resume')
    expect(labels).toContain('Start a new attempt')
    await assertSheetsSettled(canvasElement)
  },
}

export const PreviewRetry: Story = {
  parameters: {
    fixture: {
      routes: [
        archivedSettledListRoute,
        archivedSettledDetailRoute,
        sheetAttemptsRoute(archivedSettledTask.id, () => sheetJson(sheetAttempts(archivedSettledTask, [settledAttempt]))),
      ],
    },
  },
  render: () => view('delegate preview, archived chat'),
  play: async ({ canvasElement }) => {
    const sheet = await openDelegatePreview(canvasElement, archivedSettledTask.id)
    // An archived chat cannot be resumed: the way on is a new attempt.
    expect(sheet.textContent).toContain('This starts a new attempt in a new chat.')
    const go = [...sheet.querySelectorAll('.form-actions button')].find((button) => button.textContent === 'Start a new attempt')
    expect(go).toBeTruthy()
    expect([...sheet.querySelectorAll('.form-actions button')].map((button) => button.textContent)).not.toContain('Resume')
    await assertSheetsSettled(canvasElement)
  },
}

export const PreviewBodyLoading: Story = {
  parameters: {
    fixture: {
      routes: [
        listRoute,
        sheetDetailRoute(sheetDraft.id, () => sheetHeld()),
      ],
    },
  },
  render: () => view('delegate preview, description read in flight'),
  play: async ({ canvasElement }) => {
    const sheet = await openDelegatePreview(canvasElement, sheetDraft.id)
    // While the read is in flight the sheet says so: a confirmation against a
    // description nobody was shown is what this preview exists to prevent.
    expect(sheet.textContent).toContain('Loading description…')
    expect(sheet.querySelector('.task-preview')).toBeNull()
    await assertSheetsSettled(canvasElement)
  },
}

/** While true, every description read is refused; the play flips it before Retry. */
let refuseDescription = true

export const PreviewBodyError: Story = {
  parameters: {
    fixture: {
      routes: [
        listRoute,
        sheetDetailRoute(sheetDraft.id, () => (
          refuseDescription
            ? sheetFailure('Synthetic description read refused.', 500)
            : sheetJson(sheetDetail(sheetDraft, 'Prose the list never carried.'))
        )),
      ],
    },
  },
  render: () => view('delegate preview, description read refused'),
  play: async ({ canvasElement }) => {
    refuseDescription = true
    const sheet = await openDelegatePreview(canvasElement, sheetDraft.id)
    expect(sheet.textContent).toContain("This board could not read this task's description")
    // Re-read the description without closing the preview.
    refuseDescription = false
    await userEvent.click([...sheet.querySelectorAll('button')].find((button) => button.textContent?.trim() === 'Retry')!)
    await waitFor(() => expect(sheet.querySelector('.task-preview')).toBeTruthy(), { timeout: 4000 })
    await assertSheetsSettled(canvasElement)
  },
}

export const HandoverBusy: Story = {
  parameters: {
    fixture: {
      routes: [
        listRoute,
        sheetDetailRoute(sheetDraft.id, () => sheetJson(sheetDetail(sheetDraft, 'Prose the list never carried.'))),
        sheetDelegateRoute(sheetDraft.id, () => sheetHeld()),
      ],
    },
  },
  render: () => view('delegate preview, hand-over in flight'),
  play: async ({ canvasElement }) => {
    const sheet = await openDelegatePreview(canvasElement, sheetDraft.id)
    await userEvent.click([...sheet.querySelectorAll('.form-actions button')].find((button) => button.textContent === 'Delegate')!)
    // Busy is the block itself: Working…, and the fields stop taking input.
    await waitFor(() => expect([...sheet.querySelectorAll('button')].some((button) => button.textContent === 'Working…')).toBe(true))
    expect(sheet.querySelector<HTMLInputElement>('#task-delegate-instructions')).toHaveProperty('disabled', true)
    await assertSheetsSettled(canvasElement)
  },
}

export const AttemptsLoading: Story = {
  parameters: {
    fixture: {
      routes: [
        liveListRoute,
        liveDetailRoute,
        sheetAttemptsRoute(liveTask.id, () => sheetHeld()),
      ],
    },
  },
  render: () => view('agent block, history read in flight'),
  play: async ({ canvasElement }) => {
    const agent = await openAgent(canvasElement, liveTask.id)
    // The lazy history: Loading attempts… while its GET answers.
    await waitFor(() => expect(agent.textContent).toContain('Loading attempts…'))
    expect(agent.querySelector('.task-history')).toBeNull()
    await assertSheetsSettled(canvasElement)
  },
}

/** While true, every history read is refused; the play flips it before Retry. */
let refuseHistory = true

export const AttemptsError: Story = {
  parameters: {
    fixture: {
      routes: [
        liveListRoute,
        liveDetailRoute,
        sheetAttemptsRoute(liveTask.id, () => (
          refuseHistory
            ? sheetFailure('Synthetic history read refused.', 500)
            : sheetJson({ workspace: 'storybook-sheet', task: liveTask, attempts: [liveAttempt] })
        )),
      ],
    },
  },
  render: () => view('agent block, history read refused'),
  play: async ({ canvasElement }) => {
    refuseHistory = true
    const agent = await openAgent(canvasElement, liveTask.id)
    await waitFor(() => expect(agent.textContent).toContain('Synthetic history read refused.'))
    // The retry re-reads the history without closing the editor.
    refuseHistory = false
    await userEvent.click([...agent.querySelectorAll('button')].find((button) => button.textContent === 'Retry')!)
    await waitFor(() => expect(agent.querySelector('.task-history')).toBeTruthy(), { timeout: 4000 })
    await assertSheetsSettled(canvasElement)
  },
}

export const LinkedChats: Story = {
  parameters: {
    fixture: {
      routes: [
        linkedListRoute,
        linkedDetailRoute,
        linkedChatsRoute,
      ],
    },
  },
  render: () => view('agent block, linked chats'),
  play: async ({ canvasElement }) => {
    const region = await openSheetEditor(canvasElement, userEvent.click, linkedTask.id)
    const chats = await waitFor(() => {
      const section = region.querySelector('.task-linked-chats')
      expect(section).toBeTruthy()
      return section!
    })
    expect(chats.textContent).toContain('Linked chats')
    // Unlinking is an ordinary write; the row adopts the answer.
    const unlink = chats.querySelector<HTMLButtonElement>('button[aria-label^="Unlink chat"]')!
    await userEvent.click(unlink)
    await waitFor(() => expect(region.querySelector('.task-linked-chats')).toBeNull(), { timeout: 4000 })
    await assertSheetsSettled(canvasElement)
  },
}

/** The reconcile row: an agent-assigned card that is In progress over a settled attempt. */
const reconcileTask: Task = { ...settledTask, title: 'Sheet reconcile task', assignee: 'agent' }
const reconcileListRoute = sheetListRoute(() => sheetJson(sheetList([reconcileTask, sheetBoard[1]!])))
const reconcileDetailRoute = sheetDetailRoute(reconcileTask.id, () => sheetJson(sheetDetail(reconcileTask, 'Prose the list never carried.')))
const reconcileAttemptsRoute = sheetAttemptsRoute(reconcileTask.id, () => sheetJson(sheetAttempts(reconcileTask, [settledAttempt])))

export const Reconcile: Story = {
  parameters: {
    fixture: {
      routes: [
        reconcileListRoute,
        reconcileDetailRoute,
        reconcileAttemptsRoute,
      ],
    },
  },
  render: () => view('agent block, row flagged, not repaired'),
  play: async ({ canvasElement }) => {
    const region = await openSheetEditor(canvasElement, userEvent.click, reconcileTask.id)
    // A row that disagrees with itself is flagged with what disagrees and the
    // controls that resolve it — said, never rewritten.
    await waitFor(() => {
      const note = region.querySelector('.task-reconcile-note [data-code="settled_attempt_holds_task"]')
      expect(note).toBeTruthy()
      return expect(note!.textContent).toContain('Out of step')
    })
    await assertSheetsSettled(canvasElement)
  },
}
