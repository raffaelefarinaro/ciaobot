import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, screen, userEvent, waitFor } from 'storybook/test'
import { defineComponent, h, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import ArchiveTaskDialog from '../components/ArchiveTaskDialog.vue'
import { askArchiveTask, pendingArchiveTask } from '../lib/archiveTaskChoice'
import type { Task } from '../lib/types'
import { mountConnected } from './mountConnected'
import type { FixtureRoute } from './fixtureBoundary'

const WORKSPACE = 'storybook-alpha'
const TASK_ID = 'task-storybook'

function task(overrides: Partial<Task> = {}): Task {
  return {
    id: TASK_ID,
    title: 'Storybook task',
    status: 'in_progress',
    project_id: 'project-storybook',
    due: '',
    assignee: 'agent',
    chat_id: 'chat-storybook',
    attempt_id: '',
    linked_chats: [],
    created_at: '',
    updated_at: '',
    revision: 'a'.repeat(64),
    relative_path: 'Tasks/storybook-task.md',
    attempt_state: 'running',
    attempt_outcome: '',
    attempt_summary: '',
    attempt_detail: '',
    live_attempt_id: '',
    completed_at: null,
    has_resolution: false,
    ...overrides,
  }
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

type Submit = 'ok' | 'held' | 'error'

/** The owner's own reads and writes only: the task reload after a submit and the completion POST. */
function routes(submit: Submit): FixtureRoute[] {
  return [
    {
      method: 'GET',
      path: `/api/tasks?workspace=${WORKSPACE}`,
      respond: () => json({ tasks: [] }),
    },
    {
      method: 'POST',
      path: `/api/tasks/${TASK_ID}/complete`,
      respond: () => submit === 'held'
        ? new Promise<never>(() => {})
        : submit === 'error'
          ? json({ error: 'Synthetic refusal' }, 409)
          : json({}),
    },
  ]
}

interface ArchiveFixture {
  /** False renders the owner with no request: the production closed state. */
  open?: boolean
  awaitingReview?: boolean
  submit?: Submit
  narrow?: boolean
}

function renderArchive(options: ArchiveFixture = {}) {
  return defineComponent({
    setup() {
      const host = ref<HTMLElement>()
      const fixture = shallowRef<ReturnType<typeof mountConnected>>()
      const answer = ref(options.open === false ? 'Closed — no request' : 'Waiting for an answer')
      const failure = ref('')
      onMounted(() => {
        if (!host.value) return
        try {
          fixture.value = mountConnected(host.value, { component: ArchiveTaskDialog })
          if (options.open !== false) {
            void askArchiveTask(
              task(options.awaitingReview ? { status: 'in_review', attempt_state: 'ready_for_review' } : {}),
              WORKSPACE,
              !!options.awaitingReview,
            ).then((choice) => { answer.value = choice })
          }
        } catch (cause) {
          failure.value = String(cause)
        }
      })
      onBeforeUnmount(() => {
        pendingArchiveTask.value?.resolve('cancel')
        fixture.value?.dispose()
      })
      return () => h('div', options.narrow ? { style: 'max-width: 390px' } : {}, [
        h('p', `Answer: ${answer.value}`),
        h('div', { ref: host }),
        failure.value ? h('p', { role: 'alert' }, failure.value) : null,
      ])
    },
  })
}

const meta = {
  id: 'archive-task-dialog',
  title: 'Dialogs and menus/ArchiveTaskDialog',
  component: ArchiveTaskDialog,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof ArchiveTaskDialog>
export default meta
type Story = StoryObj<typeof meta>

const dialog = () => waitFor(() => screen.getByRole('dialog', { name: 'Archive chat' }))
const button = (name: string | RegExp) => screen.getByRole('button', { name })
const answerLine = () => screen.getByText(/Answer:/)

export const Closed: Story = {
  render: () => renderArchive({ open: false }),
  play: async () => {
    await waitFor(() => expect(screen.getByText('Answer: Closed — no request')).toBeTruthy())
    expect(screen.queryByRole('dialog')).toBeNull()
  },
}

export const Choose: Story = {
  parameters: { fixture: { routes: routes('ok') } },
  render: () => renderArchive(),
  play: async () => {
    const card = await dialog()
    expect(card.textContent).toContain('"Storybook task"')
    expect(card.textContent).toContain('is not done')
    expect(button('Cancel').textContent?.trim()).toBe('Cancel')
    expect(button('Keep task open').textContent?.trim()).toBe('Keep task open')
    expect(button(/Complete task/).textContent?.trim()).toBe('Complete task…')
  },
}

export const AwaitingReview: Story = {
  parameters: { fixture: { routes: routes('ok') } },
  render: () => renderArchive({ awaitingReview: true }),
  play: async () => {
    const card = await dialog()
    expect(card.textContent).toContain('waiting for your review')
    expect(button(/Complete task/).textContent?.trim()).toBe('Complete task…')
  },
}

export const Note: Story = {
  parameters: { fixture: { routes: routes('ok') } },
  render: () => renderArchive(),
  play: async () => {
    await dialog()
    await userEvent.click(button(/Complete task/))
    const field = await waitFor(() => screen.getByLabelText('How was this completed? (optional)'))
    expect(screen.getByRole('dialog').textContent).toContain('completes the task and archives this chat')
    expect(button('Back').textContent?.trim()).toBe('Back')
    expect(button('Mark done').textContent?.trim()).toBe('Mark done')
    await waitFor(() => expect(document.activeElement).toBe(field))
  },
}

export const NoteAwaitingReview: Story = {
  parameters: { fixture: { routes: routes('ok') } },
  render: () => renderArchive({ awaitingReview: true }),
  play: async () => {
    await dialog()
    await userEvent.click(button(/Complete task/))
    await waitFor(() => screen.getByLabelText('How was this completed? (optional)'))
    expect(screen.getByRole('dialog').textContent).toContain('Approve it and archive this chat')
    expect(button('Approve Done').textContent?.trim()).toBe('Approve Done')
  },
}

export const Busy: Story = {
  parameters: { fixture: { routes: routes('held') } },
  render: () => renderArchive(),
  play: async () => {
    await dialog()
    await userEvent.click(button(/Complete task/))
    await waitFor(() => screen.getByLabelText('How was this completed? (optional)'))
    await userEvent.click(button('Mark done'))
    const saving = await waitFor(() => button(/Saving/))
    expect((saving as HTMLButtonElement).disabled).toBe(true)
    expect((button('Back') as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByLabelText('How was this completed? (optional)') as HTMLTextAreaElement).disabled).toBe(true)
    expect(screen.getByRole('dialog')).toBeTruthy()
  },
}

export const SubmitError: Story = {
  parameters: { fixture: { routes: routes('error') } },
  render: () => renderArchive(),
  play: async () => {
    await dialog()
    await userEvent.click(button(/Complete task/))
    const field = await waitFor(() => screen.getByLabelText('How was this completed? (optional)'))
    await userEvent.click(button('Mark done'))
    const alert = await waitFor(() => screen.getByRole('alert'))
    expect(alert.textContent).toBe('This task changed since it was read. Check it and try again.')
    expect(screen.getByRole('dialog')).toBeTruthy()
    await waitFor(() => expect(document.activeElement).toBe(field))
    expect((button('Mark done') as HTMLButtonElement).disabled).toBe(false)
    expect(answerLine().textContent).toContain('Waiting for an answer')
  },
}

export const KeepTaskOpen: Story = {
  parameters: { fixture: { routes: routes('ok') } },
  render: () => renderArchive(),
  play: async () => {
    await dialog()
    await userEvent.click(button('Keep task open'))
    await waitFor(() => expect(answerLine().textContent).toContain('Answer: keep'))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  },
}

export const EscapeCancels: Story = {
  parameters: { fixture: { routes: routes('ok') } },
  render: () => renderArchive(),
  play: async () => {
    await dialog()
    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(answerLine().textContent).toContain('Answer: cancel'))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  },
}

export const Back: Story = {
  parameters: { fixture: { routes: routes('ok') } },
  render: () => renderArchive(),
  play: async () => {
    await dialog()
    await userEvent.click(button(/Complete task/))
    await waitFor(() => screen.getByLabelText('How was this completed? (optional)'))
    await userEvent.click(button('Back'))
    await waitFor(() => expect(button('Keep task open')).toBeTruthy())
    expect(screen.queryByLabelText('How was this completed? (optional)')).toBeNull()
  },
}

export const InitialFocus: Story = {
  parameters: { fixture: { routes: routes('ok') } },
  render: () => renderArchive(),
  play: async () => {
    await dialog()
    // The owner suppresses Reka's auto-focus and puts it on Cancel: the safe action.
    await waitFor(() => expect(document.activeElement).toBe(button('Cancel')))
    expect((document.activeElement as HTMLElement).matches(':focus-visible')).toBe(true)
  },
}

export const Narrow: Story = {
  parameters: { fixture: { routes: routes('ok') } },
  render: () => renderArchive({ narrow: true }),
  play: async () => {
    const card = await dialog()
    expect(card.textContent).toContain('"Storybook task"')
    expect((screen.getByText(/Answer:/).parentElement as HTMLElement).style.maxWidth).toBe('390px')
  },
}
