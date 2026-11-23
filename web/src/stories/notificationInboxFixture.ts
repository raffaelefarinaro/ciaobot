import type { Component } from 'vue'
import { defineComponent, h, nextTick, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import type { Pinia } from 'pinia'
import type { RouteRecordRaw } from 'vue-router'
import { expect, waitFor } from 'storybook/test'
import { mountConnected } from './mountConnected'
import type { FixtureRoute } from './fixtureBoundary'
import { useHousekeepingStore } from '../stores/housekeeping'
import { useProjectStore } from '../stores/projects'
import { useTaskSignalsStore } from '../stores/taskSignals'
import { useTaskStore } from '../stores/tasks'
import { checkingHealth, readyHealth } from '../lib/sourceHealth'
import type { SourceHealth } from '../lib/sourceHealth'
import type { ChatInfo, InAppToast, OperatorAction, ProjectInfo, WorkspaceInfo } from '../lib/types'

/** Synthetic workspace only: no real workspace name, path, chat title or message. */
export const INBOX_WORKSPACE = 'storybook-alpha'
const OTHER_WORKSPACE = 'storybook-beta'
const PROJECT_ID = 'storybook-p1'
const OTHER_PROJECT_ID = 'storybook-p2'
const FIXED_AT = '2026-11-18T12:00:00.000Z'

export const ready = readyHealth(new Date(FIXED_AT))
export const stale: SourceHealth = { status: 'stale', lastSuccessAt: FIXED_AT, error: 'Synthetic source failure' }
export const unavailable: SourceHealth = { status: 'unavailable', lastSuccessAt: '', error: 'Synthetic source failure' }
export const checking = checkingHealth()

function workspace(name: string): WorkspaceInfo {
  return { name, vault_root: name, default_provider: 'claude' }
}

function project(projectId: string, workspaceName: string): ProjectInfo {
  return {
    project_id: projectId,
    name: `Synthetic project ${projectId}`,
    workspace: workspaceName,
    context: '',
    created_at: '2026-11-01T00:00:00.000Z',
    order: 0,
    vault_folder: `storybook-${projectId}`,
  }
}

function chat(chatId: string, overrides: Partial<ChatInfo> = {}): ChatInfo {
  return {
    chat_id: chatId,
    project_id: PROJECT_ID,
    title: `Synthetic ${chatId}`,
    model: 'synthetic-model',
    provider: 'claude',
    mode: '',
    session_id: `synthetic-session-${chatId}`,
    created_at: '2026-11-18T08:00:00.000Z',
    archived: false,
    local: true,
    last_activity_at: '2026-11-18T09:00:00.000Z',
    last_read_at: '2026-11-18T09:00:00.000Z',
    ...overrides,
  }
}

/** Three inbox rows in the active workspace: one failed chat (Needs you), two unread (New replies). */
export function inboxChats(): ChatInfo[] {
  return [
    chat('storybook-blocked', {
      title: 'Synthetic blocked chat',
      last_error: 'Synthetic failure while running',
      last_activity_at: '2026-11-18T09:10:00.000Z',
      last_read_at: '2026-11-18T09:10:00.000Z',
    }),
    chat('storybook-reply', {
      title: 'Synthetic unread chat',
      last_activity_at: '2026-11-18T10:00:00.000Z',
      last_read_at: '2026-11-18T08:00:00.000Z',
      last_snippet: 'Synthetic reply text',
    }),
    chat('storybook-second-reply', {
      title: 'Synthetic second reply',
      last_activity_at: '2026-11-18T09:30:00.000Z',
      last_read_at: '2026-11-18T08:00:00.000Z',
      last_snippet: 'Another synthetic reply',
    }),
  ]
}

function otherWorkspaceChat(): ChatInfo {
  return chat('storybook-other-workspace', {
    project_id: OTHER_PROJECT_ID,
    title: 'Synthetic other-workspace chat',
    last_error: 'Synthetic failure in another workspace',
  })
}

export function operatorAction(index: number, blocking: boolean): OperatorAction {
  return {
    id: `synthetic-check-${index}`,
    kind: 'setup-check',
    severity: 20,
    title: `Synthetic ${blocking ? 'blocking' : 'review'} check ${index}`,
    detail: 'Synthetic detail for the notification inbox.',
    glyph: '',
    workspace: '',
    run_label: 'Run check',
    chat_label: '',
    chat_prompt: '',
    view_label: '',
    view_route: '',
    blocking,
  }
}

function seededActions(operator: InboxSeed['operator']): OperatorAction[] {
  if (operator === 'many') return Array.from({ length: 120 }, (_, i) => operatorAction(i + 1, i % 2 === 0))
  if (operator === 'one') return [operatorAction(1, true), operatorAction(2, false)]
  return []
}

export type InboxSeed = {
  chats?: 'none' | 'rows'
  operator?: 'none' | 'one' | 'many'
  /** HTTP health of each projected source; the chat source combines projects and task signals. */
  health?: Partial<Record<'chats' | 'operator' | 'automations' | 'updateTasks', SourceHealth>>
  /** False leaves the update-task listing stamped for another workspace, so it reads back as checking. */
  updateTasksScoped?: boolean
}

/**
 * Seeds only the stores the inbox projection reads. No bootstrap, no `init()`,
 * no fetch: every computed resolves from this state alone.
 */
export function seedInbox(pinia: Pinia, seed: InboxSeed = {}): void {
  const health = seed.health ?? {}
  const projects = useProjectStore(pinia)
  projects.workspaces = [workspace(INBOX_WORKSPACE), workspace(OTHER_WORKSPACE)]
  projects.projects = [project(PROJECT_ID, INBOX_WORKSPACE), project(OTHER_PROJECT_ID, OTHER_WORKSPACE)]
  projects.activeWorkspace = INBOX_WORKSPACE
  projects.activeChatId = null
  projects.besideChatId = null
  projects.chats = seed.chats === 'rows' ? [...inboxChats(), otherWorkspaceChat()] : []
  projects.chatsHealth = health.chats ?? ready

  const taskSignals = useTaskSignalsStore(pinia)
  taskSignals.healthWorkspace = INBOX_WORKSPACE
  taskSignals.loadedWorkspace = INBOX_WORKSPACE
  taskSignals.health = health.chats ?? ready
  taskSignals.tasks = []

  const housekeeping = useHousekeepingStore(pinia)
  housekeeping.actions = seededActions(seed.operator)
  housekeeping.actionsHealth = health.operator ?? ready
  housekeeping.updateTasks = []
  housekeeping.updateTasksWorkspace = seed.updateTasksScoped === false ? '' : INBOX_WORKSPACE
  housekeeping.updateTasksHealth = health.updateTasks ?? ready

  const automations = health.automations ?? ready
  const tasks = useTaskStore(pinia)
  tasks.schedules = []
  tasks.schedulesLoaded = automations.status === 'ready' || automations.status === 'stale'
  tasks.scheduleLoadError = automations.status === 'stale' || automations.status === 'unavailable'
    ? 'Synthetic source failure'
    : ''
}

/** Seeds the toast stack and workspace scope the production InAppToast reads. */
export function seedToasts(pinia: Pinia, toasts: InAppToast[]): void {
  const projects = useProjectStore(pinia)
  projects.workspaces = [workspace(INBOX_WORKSPACE), workspace(OTHER_WORKSPACE)]
  projects.projects = [project(PROJECT_ID, INBOX_WORKSPACE), project(OTHER_PROJECT_ID, OTHER_WORKSPACE)]
  projects.activeWorkspace = INBOX_WORKSPACE
  projects.activeChatId = null
  projects.chats = [...inboxChats(), otherWorkspaceChat()]
  // Push into the live array rather than replacing the ref: the store owns
  // `toasts`, and in-place mutation is what its own dismiss does.
  projects.toasts.splice(0, projects.toasts.length, ...toasts)
}

export function toast(id: number, fields: Omit<InAppToast, 'id'>): InAppToast {
  return { id, ...fields }
}

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  })
}

export type InboxRouteOptions = {
  /** What `/api/chats?active_only=1` answers when a row is opened. */
  chatsRead?: 'same' | 'gone'
  /** The operator source refresh: ready answers an empty list, pending never resolves. */
  operator?: 'ready' | 'pending'
  /** The automations refresh: pending never resolves. */
  automations?: 'pending'
}

/**
 * The exact read routes an interaction in this slice can reach. Only stories
 * that perform the interaction declare one; any other request is a violation.
 */
export function inboxRoutes(options: InboxRouteOptions = {}): FixtureRoute[] {
  const routes: FixtureRoute[] = []
  if (options.chatsRead) {
    routes.push({
      method: 'GET',
      path: '/api/chats?active_only=1',
      respond: () => json(options.chatsRead === 'gone' ? [] : inboxChats()),
    })
    routes.push({
      method: 'GET',
      path: `/api/tasks?workspace=${encodeURIComponent(INBOX_WORKSPACE)}`,
      respond: () => json({ workspace: INBOX_WORKSPACE, tasks: [] }),
    })
  }
  if (options.operator === 'ready') {
    routes.push({ method: 'GET', path: '/api/housekeeping', respond: () => json({ actions: [] }) })
  } else if (options.operator === 'pending') {
    routes.push({ method: 'GET', path: '/api/housekeeping', respond: () => new Promise<never>(() => {}) })
  }
  if (options.automations === 'pending') {
    routes.push({ method: 'GET', path: '/api/schedules', respond: () => new Promise<never>(() => {}) })
  }
  return routes
}

/** Inert destinations for the inbox row links: no page, no bootstrap, no fetch. */
export function notificationFixtureRoutes(): RouteRecordRaw[] {
  const inert = (path: string) => ({
    path,
    component: defineComponent({ render: () => h('p', `Inert ${path} destination; no page mounted.`) }),
  })
  return [
    inert('/chat/:chatId'),
    inert('/tasks/:taskId'),
    inert('/schedules/:scheduleId'),
    inert('/settings'),
    inert('/settings/:tab'),
  ]
}

export type ConnectedOwnerOptions = {
  component: Component
  /** Rendered-state props for the production owner. */
  props?: Record<string, unknown>
  seed: (pinia: Pinia) => void
  label: string
  /** Runs in the host's setup, before the owner mounts; removes its own effects on unmount. */
  prepare?: () => () => void
}

/**
 * Hosts one connected owner in the fixture's separate Vue app and shows its
 * request/violation ledger, so every play can prove the fence stayed quiet and
 * the story reached only the routes it declared.
 */
export function connectedOwnerStory(options: ConnectedOwnerOptions) {
  const owner = defineComponent({ name: 'NoticesOwnerWrapper', render: () => h(options.component, options.props ?? {}) })
  return defineComponent({
    name: 'NoticesConnectedStory',
    setup() {
      const host = ref<HTMLElement>()
      const fixture = shallowRef<ReturnType<typeof mountConnected>>()
      const ready = ref(false)
      const error = ref('')
      const clearPrepared = ref<(() => void) | undefined>()
      onMounted(() => {
        try {
          clearPrepared.value = options.prepare?.()
          fixture.value = mountConnected(host.value!, {
            component: owner,
            routerRoutes: notificationFixtureRoutes(),
            seed: options.seed,
          })
          // The owner is the router's `/` component, and the initial memory
          // navigation resolves after mount. Announce readiness only once the
          // route is final and its render has flushed, so a play never asserts
          // against the empty host the router is still filling.
          void fixture.value.router.isReady().then(() => nextTick()).then(() => { ready.value = true })
        } catch (cause) {
          error.value = String(cause)
        }
      })
      onBeforeUnmount(() => {
        fixture.value?.dispose()
        clearPrepared.value?.()
      })
      return () => {
        const mounted = fixture.value
        return h('div', [
          h('p', `Synthetic ${options.label} fixture for the production component.`),
          h('div', { ref: host }),
          h('p', { role: 'status', 'data-testid': 'story-ledger' },
            `Fixture requests: ${mounted?.boundary.requests.length ?? 0}. Violations: ${mounted?.boundary.violations.length ?? 0}.`),
          h('p', { 'data-testid': 'story-ready' }, ready.value ? 'Fixture ready.' : 'Fixture mounting.'),
          h('p', { 'data-testid': 'story-route' },
            `Route: ${mounted?.router.currentRoute.value.fullPath ?? '-'}`),
          error.value
            ? h('p', { role: 'alert', 'data-testid': 'story-error' }, error.value)
            : null,
          ...(mounted?.boundary.violations ?? []).map(message =>
            h('p', { role: 'alert', 'data-testid': 'story-violation' }, message)),
        ])
      }
    },
  })
}

type Canvas = {
  getByTestId: (id: string) => HTMLElement
  queryByTestId: (id: string) => HTMLElement | null
  queryByText: (text: RegExp) => HTMLElement | null
}

/**
 * The workshop's own control socket reports after render. Prove the owner
 * mounted, attempted nothing undeclared, and the fence recorded no violation.
 */
export async function assertQuietFence(canvas: Canvas): Promise<void> {
  await waitFor(() => expect(canvas.getByTestId('story-ready').textContent).toBe('Fixture ready.'))
  expect(canvas.queryByTestId('story-error')).toBeNull()
  expect(canvas.queryByText(/Story network violation/)).toBeNull()
  await waitFor(() => expect(canvas.getByTestId('story-ledger').textContent).toContain('Violations: 0'))
}

/** The fixture router's current location, as the ledger renders it. */
export function storyRoute(canvas: Canvas): string {
  return canvas.getByTestId('story-route').textContent ?? ''
}
