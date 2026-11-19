import { defineComponent, h, onBeforeUnmount, onMounted, provide, reactive, ref, shallowRef } from 'vue'
import { routeLocationKey, routerKey } from 'vue-router'
import type { Pinia } from 'pinia'
import { expect, userEvent, waitFor } from 'storybook/test'
import TaskWorkspaceSurface from '../components/TaskWorkspaceSurface.vue'
import { useProjectStore } from '../stores/projects'
import type { Task, TaskStatus } from '../lib/types'
import type { FixtureRoute } from './fixtureBoundary'
import { mountConnected, type ConnectedFixture } from './mountConnected'

/**
 * Synthetic task-sheet fixtures for the four "Task sheets" rows owned by
 * `TaskWorkspaceSurface.vue` (the #2930 B12 slice): the create/edit sheets,
 * the completion sheet, the delegate preview and the move sheet. No real
 * workspace names, paths, ids, tokens or user text.
 *
 * The connected mounts run on the #3267 boundary (`mountConnected`), so every
 * story declares in `parameters.fixture` exactly the reads and writes its
 * mount and play perform; anything else fails closed and the runner reports
 * it. Board-mode stories give the surface the one seam its own tests use: an
 * inert address (`routerKey`/`routeLocationKey` provided by the host), because
 * the workshop has no `/tasks/:taskId` route to hand back.
 */

/** The one synthetic workspace the task sheets run against. */
export const sheetWorkspace = 'storybook-sheet'

const sheetRevision = '4'.repeat(64)
const sheetRevisionNext = '5'.repeat(64)

/** The declared paths' workspace query, the same on every route. */
export const sheetWorkspaceQuery = `workspace=${sheetWorkspace}`

/** One synthetic task, every field at its sheet-fixture default. */
export function sheetTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 'sb-sheet-task',
    title: 'Sheet draft task',
    status: 'backlog',
    project_id: '',
    due: '',
    assignee: 'user',
    chat_id: '',
    attempt_id: '',
    linked_chats: [],
    created_at: '2026-03-01T09:00:00+00:00',
    updated_at: '2026-03-01T09:00:00+00:00',
    revision: sheetRevision,
    relative_path: 'Tasks/sb-sheet-task.md',
    attempt_state: '',
    attempt_outcome: '',
    attempt_summary: '',
    attempt_detail: '',
    live_attempt_id: '',
    // The Done lane keeps only tasks finished today, in the viewer's day.
    completed_at: overrides.status === 'done' ? new Date().toISOString() : null,
    has_resolution: false,
    ...overrides,
  }
}

export const sheetDraft = sheetTask()
export const sheetShip = sheetTask({
  id: 'sb-sheet-second',
  title: 'Sheet ship task',
  status: 'in_progress',
  revision: sheetRevisionNext,
})

/** The two-task board most stories draw: To do and In progress, one lane each. */
export const sheetBoard: readonly Task[] = [sheetDraft, sheetShip]

/** Board list rows without `sb-sheet-task` (a record that vanished on disk). */
export const sheetBoardWithoutDraft: readonly Task[] = [sheetShip]

/** Two done tasks for the drawn-Done move story. */
export const sheetDoneBoard: readonly Task[] = [
  sheetTask({ title: 'Sheet draft task', status: 'done' }),
  sheetTask({ id: 'sb-sheet-archived', title: 'Sheet archived task', status: 'done', revision: sheetRevisionNext }),
]

/** A fresh list answer payload, `order` included. */
export function sheetList(tasks: readonly Task[], extra: { order_error?: string; orderVersion?: number } = {}) {
  const sequences: Record<TaskStatus, string[]> = { backlog: [], in_progress: [], in_review: [], done: [] }
  for (const task of tasks) sequences[task.status].push(task.id)
  return {
    workspace: sheetWorkspace,
    tasks,
    order: {
      version: extra.orderVersion ?? 1,
      sequences,
    },
    ...(extra.order_error ? { order_error: extra.order_error } : {}),
  }
}

/** A `GET /api/tasks/{id}` detail payload, with the record's `body`. */
export function sheetDetail(
  task: Task,
  body = '',
  extra: { resolution?: string; updates?: unknown[]; completions?: unknown[] } = {},
) {
  return {
    workspace: sheetWorkspace,
    task: {
      ...task,
      body,
      ...(extra.resolution !== undefined ? { resolution: extra.resolution } : {}),
      ...(extra.updates !== undefined ? { updates: extra.updates } : {}),
      ...(extra.completions !== undefined ? { completions: extra.completions } : {}),
    },
  }
}

/** One attempt-history row; `attempt_id` has a synthetic default. */
export function sheetAttempt(overrides: Partial<{
  attempt_id: string
  task_id: string
  task_revision: string
  chat_id: string
  state: string
  created_at: string
  updated_at: string
  ended_at: string
  detail: string
  outcome: string
  summary: string
  released: boolean
  live: boolean
  chat_archived: boolean
}> = {}) {
  return {
    attempt_id: 'sb-sheet-attempt-live',
    task_id: 'sb-sheet-task',
    task_revision: sheetRevision,
    chat_id: 'sb-sheet-chat-live',
    state: 'running',
    created_at: '2026-03-01T09:00:00+00:00',
    updated_at: '2026-03-01T09:05:00+00:00',
    ended_at: '',
    detail: '',
    outcome: '',
    summary: '',
    released: false,
    live: false,
    chat_archived: false,
    ...overrides,
  }
}

/** An attempt-history payload the surface's editor draws. */
export function sheetAttempts(task: Task, attempts: readonly unknown[]) {
  return { workspace: sheetWorkspace, task, attempts }
}

export function sheetJson(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/** A refused write the way the real API words one, at any status. */
export function sheetFailure(message: string, status = 500): Response {
  return sheetJson({ error: { code: 'fixture_refused', message, retryable: false } }, status)
}

/** A declared route whose answer never lands: the native-loading shape. */
export function sheetHeld(): Promise<never> {
  return new Promise<never>(() => {})
}

export function sheetListRoute(respond: () => Response | Promise<never>): FixtureRoute {
  return { method: 'GET', path: `/api/tasks?${sheetWorkspaceQuery}`, respond }
}

export function sheetDetailRoute(taskId: string, respond: () => Response | Promise<never>): FixtureRoute {
  return { method: 'GET', path: `/api/tasks/${taskId}?${sheetWorkspaceQuery}`, respond }
}

export function sheetAttemptsRoute(taskId: string, respond: () => Response | Promise<never>): FixtureRoute {
  return { method: 'GET', path: `/api/tasks/${taskId}/attempts?${sheetWorkspaceQuery}`, respond }
}

export function sheetCreateRoute(respond: () => Response | Promise<never>): FixtureRoute {
  return { method: 'POST', path: '/api/tasks', respond }
}

export function sheetCompleteRoute(taskId: string, respond: () => Response | Promise<never>): FixtureRoute {
  return { method: 'POST', path: `/api/tasks/${taskId}/complete`, respond }
}

export function sheetPatchRoute(taskId: string, respond: () => Response | Promise<never>): FixtureRoute {
  return { method: 'PATCH', path: `/api/tasks/${taskId}`, respond }
}

export function sheetOrderRoute(taskId: string, respond: () => Response | Promise<never>): FixtureRoute {
  return { method: 'POST', path: `/api/tasks/${taskId}/order`, respond }
}

export function sheetDelegateRoute(taskId: string, respond: () => Response | Promise<never>): FixtureRoute {
  return { method: 'POST', path: `/api/tasks/${taskId}/delegate`, respond }
}

export function sheetAttemptActionRoute(taskId: string, attemptId: string, action: string, respond: () => Response | Promise<never>): FixtureRoute {
  return { method: 'POST', path: `/api/tasks/${taskId}/attempt/${attemptId}/${action}`, respond }
}

export function sheetChatsRoute(taskId: string, respond: () => Response | Promise<never>): FixtureRoute {
  return { method: 'POST', path: `/api/tasks/${taskId}/chats`, respond }
}

/**
 * Stateful responder: the `n`th call answers from `answers[n]`, repeating the
 * last one, so a play can script a refusal then its refresh with declared
 * paths only.
 */
export function sheetAnswers<T>(answers: readonly T[]): () => T {
  let call = 0
  return () => {
    call++
    const answer = answers[Math.min(call - 1, answers.length - 1)]!
    // A Response body is read once: a repeated answer is a fresh copy.
    return answer instanceof Response ? (answer.clone() as T) : answer
  }
}

/**
 * Store seed only, never orchestration: the synthetic workspace name and its
 * auto-managed General project, like the component tests do.
 */
export function seedSheetStore(pinia: Pinia): void {
  const store = useProjectStore(pinia)
  store.workspaces = [
    { name: sheetWorkspace, vault_root: '/tmp/storybook-sheet', default_provider: 'claude', gws_profile: '' },
  ]
  store.activeWorkspace = sheetWorkspace
  store.projects = [
    { project_id: 'sb-sheet-general', name: 'General', workspace: sheetWorkspace, context: '', created_at: '2026-03-01', order: 1, vault_folder: '', is_auto: true },
  ]
}

/** The host renders the production surface over an inert address — the seam
 * the component's own tests mount with ("a bare mount ... renders without a
 * router"): the board's address sync never fires, because the workshop has no
 * `/tasks/:taskId` route to hand back, and a play asserts the surface's own
 * state, never an address.
 */
export function sheetHost(props: { mode?: 'board' | 'overlay'; taskId?: string } = {}, options: { doneShown?: boolean } = {}) {
  return defineComponent({
    name: 'TaskSheetsStoryHost',
    setup() {
      // An inert address: no navigation, no route reads, no extra declared routes.
      // useHistoryNav registers an afterEach hook on whatever router it finds.
      provide(routerKey, { replace: () => Promise.resolve(), afterEach: () => () => {} } as never)
      provide(routeLocationKey, reactive({ fullPath: '/tasks', params: {} }) as never)
      let restore: (() => void) | null = null
      if (options.doneShown !== undefined) {
        let previous: string | null = null
        try { previous = localStorage.getItem('ciao.tasks.doneShown') } catch { /* blocked: nothing persists */ }
        try { localStorage.setItem('ciao.tasks.doneShown', options.doneShown ? '1' : '0') } catch { /* blocked */ }
        restore = () => {
          try {
            if (previous === null) localStorage.removeItem('ciao.tasks.doneShown')
            else localStorage.setItem('ciao.tasks.doneShown', previous)
          } catch { /* blocked */ }
        }
      }
      onBeforeUnmount(() => restore?.())
      return () => h(TaskWorkspaceSurface, {
        mode: props.mode ?? 'board',
        ...(props.taskId ? { taskId: props.taskId } : {}),
      })
    },
  })
}

/** Mount the surface over the active boundary; the story must declare its routes. */
export function mountSheetTask(
  host: HTMLElement,
  props: { mode?: 'board' | 'overlay'; taskId?: string } = {},
  options: { doneShown?: boolean } = {},
): ConnectedFixture {
  return mountConnected(host, {
    component: sheetHost(props, options),
    seed: seedSheetStore,
  })
}

/** Mount + the visible ledger, the shape the connected Home Today stats stories use. */
export function sheetStoryView(config: {
  label: string
  props?: { mode?: 'board' | 'overlay'; taskId?: string }
  options?: { doneShown?: boolean }
  maxWidth?: number
}) {
  return defineComponent({
    name: 'TaskSheetsStoryView',
    setup() {
      const host = ref<HTMLElement>()
      const fixture = shallowRef<ConnectedFixture>()
      const error = ref('')
      onMounted(() => {
        try { fixture.value = mountSheetTask(host.value!, config.props ?? {}, config.options) } catch (cause) { error.value = String(cause) }
      })
      onBeforeUnmount(() => fixture.value?.dispose())
      return () => h('div', { style: `max-width: ${config.maxWidth ?? 640}px` }, [
        h('p', `Synthetic ${config.label} fixture for the production TaskWorkspaceSurface sheets.`),
        h('div', { ref: host }),
        h('p', { role: 'status' }, `Fixture requests: ${fixture.value?.boundary.requests.length ?? 0}. Violations: ${fixture.value?.boundary.violations.length ?? 0}.`),
        error.value ? h('p', { role: 'alert' }, error.value) : null,
        ...(fixture.value?.boundary.violations ?? []).map((message) => h('p', { role: 'alert' }, message)),
      ])
    },
  })
}

/**
 * Pick a native `<select>` the way the element's own change event does: the
 * workshop runner has no real native dropdown to drive, and the surface's
 * `v-model` listens to `change`.
 */
export function pickSelect(select: HTMLSelectElement, value: string): void {
  select.value = value
  select.dispatchEvent(new Event('change', { bubbles: true }))
}

/**
 * Open a card's editor the way a card does (its title press), and wait for the
 * page region. Works in board mode only; the description read's shape is left
 * to the play.
 */
export async function openSheetEditor(
  canvasElement: HTMLElement,
  click: (element: Element) => Promise<void>,
  taskId = 'sb-sheet-task',
): Promise<HTMLElement> {
  const title = await waitFor(() => {
    const found = canvasElement.querySelector<HTMLElement>(`[data-task-id="${taskId}"] .task-title`)
    expect(found).toBeTruthy()
    return found!
  }, { timeout: 4000 })
  await click(title)
  return waitFor(() => {
    expect(canvasElement.querySelector<HTMLElement>('[role="region"][aria-label="Edit task"]')).toBeTruthy()
    return canvasElement.querySelector<HTMLElement>('[role="region"][aria-label="Edit task"]')!
  })
}

/**
 * Open one card's ⋯ menu and wait for its portal content; the reka-ui menu
 * renders outside the story canvas, so callers query `document.body`.
 */
export async function openCardMenu(cardId: string): Promise<HTMLElement> {
  // The board draws its cards after the list read lands: wait for the trigger.
  const trigger = await waitFor(() => {
    const found = document.body.querySelector<HTMLButtonElement>(`[data-task-id="${cardId}"] .task-more`)
    expect(found).toBeTruthy()
    return found!
  }, { timeout: 4000 })
  trigger.focus()
  await userEvent.click(trigger)
  return waitFor(() => {
    expect(document.body.querySelector('.context-menu')).toBeTruthy()
    return document.body.querySelector<HTMLElement>('.context-menu')!
  })
}

/** Press one of the opened card menu's items by its exact label. */
export function menuButton(menu: Element, label: string): HTMLButtonElement {
  const button = [...menu.querySelectorAll('button')].find((candidate) => candidate.textContent === label)
  if (!button) throw new Error(`No ${label} in the open card menu`)
  return button as HTMLButtonElement
}

/**
 * Settle a connected story: requests have reached the ledger, then one quiet
 * half second, then the ledger still says zero violations — the shared shape
 * the connected Home Today stats stories assert. Alerts inside the surface
 * are its own honest states, so only the boundary's own violation ledger is
 * asserted here.
 */
export async function assertSheetsSettled(canvasElement: HTMLElement): Promise<void> {
  await waitFor(() => {
    const ledger = canvasElement.lastElementChild?.textContent ?? ''
    expect(ledger).toMatch(/Fixture requests: [1-9]/)
    expect(ledger).toMatch(/Violations: 0\./)
  })
  await new Promise((resolve) => setTimeout(resolve, 500))
  expect(canvasElement.textContent).not.toContain('Story network violation')
  expect(canvasElement.lastElementChild?.textContent ?? '').toMatch(/Violations: 0\./)
}
