import { h } from 'vue'
import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, userEvent, waitFor } from 'storybook/test'
import TaskOriginNote from '../components/TaskOriginNote.vue'
import type { ChatInfo, Task } from '../lib/types'
import { peekHost, peekView, seedTaskSignals, settlePeek } from './taskPeekFixture'
import {
  sheetCompleteRoute, sheetFailure, sheetHeld, sheetJson, sheetList, sheetListRoute, sheetTask,
} from './taskSheetsFixture'

/**
 * The chat's Origin rows (the `components-task-origin-note` inventory row,
 * #3298): the delegated task row with its state, the Approve Done and Mark
 * done closure form, and the linked-task rows. Each story mounts the
 * production component through `mountConnected`, seeds the app-wide task
 * signals it reads, and declares only the writes its play performs. Synthetic
 * fixtures only.
 */
const meta = {
  id: 'task-origin-note',
  title: 'Task notes and peek/TaskOriginNote',
  component: TaskOriginNote,
  args: { chat: { chat_id: 'sb-origin-chat' } },
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof TaskOriginNote>
export default meta
type Story = StoryObj<typeof meta>

type OriginChat = Pick<ChatInfo, 'chat_id' | 'helper'>

const originChat = 'sb-origin-chat'
const originAttempt = 'sb-origin-attempt'

/** A delegated task whose agent is still working. */
const runningTask = sheetTask({
  id: 'sb-origin-task',
  title: 'Synthetic origin task',
  status: 'in_progress',
  attempt_id: originAttempt,
  live_attempt_id: originAttempt,
  attempt_state: 'running',
  chat_id: originChat,
})

/** The same task once the agent reported done and the result waits for Approve. */
const reviewTask = sheetTask({
  ...runningTask,
  status: 'in_review',
  attempt_state: 'ready_for_review',
})

/** The chat this task was delegated to. `attemptId` may name an older attempt. */
function delegated(attemptId = originAttempt): OriginChat {
  return {
    chat_id: originChat,
    helper: { kind: 'task_delegation', task_id: 'sb-origin-task', task_revision: runningTask.revision, attempt_id: attemptId },
  }
}

/** A chat with no delegation stamp: the helper is absent, not null. */
const plainChat: OriginChat = { chat_id: originChat }

/** The production owner in a 390px wrapper, with its signals seeded. */
function origin(label: string, chat: OriginChat, tasks: readonly Task[], loaded = true) {
  return peekView({
    label,
    component: peekHost(() => h(TaskOriginNote, { chat })),
    seed: seedTaskSignals(tasks, loaded),
    maxWidth: 390,
  })
}

function buttonNamed(canvasElement: HTMLElement, text: string): HTMLButtonElement {
  const found = [...canvasElement.querySelectorAll<HTMLButtonElement>('button')].find((button) => button.textContent?.trim() === text)
  if (!found) throw new Error(`No button "${text}"`)
  return found
}

async function titleOf(canvasElement: HTMLElement): Promise<HTMLElement> {
  return waitFor(() => {
    const found = canvasElement.querySelector<HTMLElement>('.task-origin-title')
    expect(found).toBeTruthy()
    return found!
  })
}

export const NoOrigin: Story = {
  render: () => h(origin('no origin', plainChat, [])),
  play: async ({ canvasElement }) => {
    await settlePeek(canvasElement)
    // A chat with no delegation and no link draws no origin section at all.
    expect(canvasElement.querySelector('.task-origin')).toBeNull()
  },
}

export const TaskNotLoaded: Story = {
  render: () => h(origin('delegated, task not loaded', delegated(), [], false)),
  play: async ({ canvasElement }) => {
    await settlePeek(canvasElement)
    // Before the signals read lands, the row falls back to the chat's own stamp.
    const title = await titleOf(canvasElement)
    expect(title.textContent).toBe('A task on the board')
    expect(canvasElement.querySelector('.task-origin-detail')!.textContent).toBe('Delegated task')
    expect(canvasElement.querySelector('.task-origin-status')).toBeNull()
    expect(canvasElement.querySelector('.task-origin-controls')).toBeNull()
  },
}

export const Running: Story = {
  render: () => h(origin('delegated, running', delegated(), [runningTask])),
  play: async ({ canvasElement }) => {
    await settlePeek(canvasElement)
    const title = await titleOf(canvasElement)
    expect(title.textContent).toBe('Synthetic origin task')
    const status = canvasElement.querySelector<HTMLElement>('.task-origin-status')!
    expect(status.textContent).toBe('Running')
    expect(status.classList.contains('warn')).toBe(true)
    // A running task can still be marked done, and cannot be approved yet.
    const markDone = buttonNamed(canvasElement, 'Mark done…')
    expect(markDone.disabled).toBe(false)
    expect(markDone.getAttribute('aria-label')).toBe('Mark Synthetic origin task done and archive this chat')
    expect([...canvasElement.querySelectorAll('button')].some((button) => button.textContent?.trim() === 'Approve Done')).toBe(false)
  },
}

export const ReviewReady: Story = {
  render: () => h(origin('review ready', delegated(), [reviewTask])),
  play: async ({ canvasElement }) => {
    await settlePeek(canvasElement)
    const status = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('.task-origin-status')
      expect(found).toBeTruthy()
      return found!
    })
    expect(status.textContent).toBe('Review ready')
    const approve = buttonNamed(canvasElement, 'Approve Done')
    expect(approve.getAttribute('aria-label')).toBe('Approve the result of Synthetic origin task and mark it done')
    // A result waiting for review is approved, not marked done over.
    expect([...canvasElement.querySelectorAll('button')].some((button) => button.textContent?.trim() === 'Mark done…')).toBe(false)
  },
}

export const ClosureForm: Story = {
  render: () => h(origin('approve form open', delegated(), [reviewTask])),
  play: async ({ canvasElement }) => {
    await settlePeek(canvasElement)
    await userEvent.click(buttonNamed(canvasElement, 'Approve Done'))
    const field = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLTextAreaElement>('.task-origin-note')
      expect(found).toBeTruthy()
      return found!
    })
    // The note field takes the keyboard the moment the form opens.
    expect(document.activeElement).toBe(field)
    expect(canvasElement.querySelector('.task-origin-conseq')!.textContent).toBe('Marks the task done and archives this chat.')
    // Cancel closes the form and hands focus back to the button that opened it.
    await userEvent.click(buttonNamed(canvasElement, 'Cancel'))
    await waitFor(() => expect(canvasElement.querySelector('.task-origin-note')).toBeNull())
    await waitFor(() => expect(document.activeElement).toBe(buttonNamed(canvasElement, 'Approve Done')))
  },
}

export const MarkDoneForm: Story = {
  render: () => h(origin('mark done form open', delegated(), [runningTask])),
  play: async ({ canvasElement }) => {
    await settlePeek(canvasElement)
    await userEvent.click(buttonNamed(canvasElement, 'Mark done…'))
    const field = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLTextAreaElement>('.task-origin-note')
      expect(found).toBeTruthy()
      return found!
    })
    expect(document.activeElement).toBe(field)
    expect(canvasElement.querySelector('.task-origin-conseq')!.textContent).toBe('Closes the task and archives this chat. The transcript stays in your vault.')
    expect(buttonNamed(canvasElement, 'Mark done and archive')).toBeTruthy()
    await userEvent.click(buttonNamed(canvasElement, 'Cancel'))
    await waitFor(() => expect(document.activeElement).toBe(buttonNamed(canvasElement, 'Mark done…')))
  },
}

export const Saving: Story = {
  parameters: {
    fixture: { routes: [sheetCompleteRoute(reviewTask.id, () => sheetHeld())] },
  },
  render: () => h(origin('approve saving', delegated(), [reviewTask])),
  play: async ({ canvasElement }) => {
    await settlePeek(canvasElement)
    await userEvent.click(buttonNamed(canvasElement, 'Approve Done'))
    await waitFor(() => expect(canvasElement.querySelector('.task-origin-note')).toBeTruthy())
    await userEvent.click(canvasElement.querySelector<HTMLButtonElement>('.task-origin-actions .btn-primary')!)
    // The write is in flight: the submit and the field are blocked, not only dimmed.
    await waitFor(() => expect(buttonNamed(canvasElement, 'Saving…').disabled).toBe(true))
    expect(canvasElement.querySelector<HTMLTextAreaElement>('.task-origin-note')!.disabled).toBe(true)
    expect(buttonNamed(canvasElement, 'Cancel').disabled).toBe(true)
  },
}

export const Refused: Story = {
  parameters: {
    fixture: {
      routes: [
        sheetCompleteRoute(reviewTask.id, () => sheetFailure('Synthetic completion refused.', 500)),
        // The refused write re-reads the signals; the task is still waiting for review.
        sheetListRoute(() => sheetJson(sheetList([reviewTask]))),
      ],
    },
  },
  render: () => h(origin('approve refused', delegated(), [reviewTask])),
  play: async ({ canvasElement }) => {
    await settlePeek(canvasElement)
    await userEvent.click(buttonNamed(canvasElement, 'Approve Done'))
    await waitFor(() => expect(canvasElement.querySelector('.task-origin-note')).toBeTruthy())
    await userEvent.click(canvasElement.querySelector<HTMLButtonElement>('.task-origin-actions .btn-primary')!)
    const alert = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('.task-origin-error[role="alert"]')
      expect(found).toBeTruthy()
      return found!
    })
    expect(alert.textContent).toContain('Synthetic completion refused.')
    // The form stays open with its keyboard back in the note, so the user can retry.
    await waitFor(() => expect(document.activeElement).toBe(canvasElement.querySelector('.task-origin-note')))
    expect(buttonNamed(canvasElement, 'Approve Done').disabled).toBe(false)
  },
}

export const Superseded: Story = {
  render: () => h(origin('a newer attempt holds the task', delegated('sb-origin-attempt-old'), [runningTask])),
  play: async ({ canvasElement }) => {
    await settlePeek(canvasElement)
    const status = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('.task-origin-status')
      expect(found).toBeTruthy()
      return found!
    })
    expect(status.textContent).toBe('A newer attempt holds this task')
    expect(status.classList.contains('muted')).toBe(true)
    // This chat's result is history: no approve and no mark done from here.
    expect(canvasElement.querySelector('.task-origin-controls')).toBeNull()
  },
}

export const Done: Story = {
  render: () => h(origin('task done', delegated(), [sheetTask({ ...runningTask, status: 'done', attempt_state: '', live_attempt_id: '' })])),
  play: async ({ canvasElement }) => {
    await settlePeek(canvasElement)
    const status = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('.task-origin-status')
      expect(found).toBeTruthy()
      return found!
    })
    expect(status.textContent).toBe('Done')
    expect(status.classList.contains('done')).toBe(true)
    expect(canvasElement.querySelector('.task-origin-controls')).toBeNull()
  },
}

const linkedTask = sheetTask({
  id: 'sb-origin-linked',
  title: 'Synthetic linked task',
  status: 'backlog',
  linked_chats: [originChat],
})

export const LinkedOnly: Story = {
  render: () => h(origin('linked task only', plainChat, [linkedTask])),
  play: async ({ canvasElement }) => {
    await settlePeek(canvasElement)
    const rows = await waitFor(() => {
      const found = canvasElement.querySelectorAll<HTMLAnchorElement>('.task-origin-line')
      expect(found).toHaveLength(1)
      return found
    })
    // A link is read-only: a named row to the task, with no state and no closure.
    expect(rows[0]!.getAttribute('href')).toBe('/tasks/sb-origin-linked')
    expect(rows[0]!.querySelector('.task-origin-detail')!.textContent).toBe('Linked task')
    expect(canvasElement.querySelector('.task-origin-status')).toBeNull()
    expect(canvasElement.querySelector('.task-origin-controls')).toBeNull()
  },
}
