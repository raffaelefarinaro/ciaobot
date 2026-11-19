/**
 * Needs-you body fixture: the synthetic chat action the cards pass in, the
 * held and refused requests the bodies can make, and the wrapper that mounts a
 * production body as the `/` route of a connected fixture. Synthetic values
 * only: storybook-alpha ids, no real paths, tokens or user text.
 */
import { defineComponent, h, onBeforeUnmount, onMounted, ref, shallowRef, type Component } from 'vue'
import type { RouteRecordRaw } from 'vue-router'
import type { ChatAction, ChatActionKind, ChatActionState } from '../lib/types'
import type { FixtureRoute } from './fixtureBoundary'
import { hold } from './terminalFixtures'
import { mountConnected, type ConnectedFixture } from './mountConnected'

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

export const CHAT_ID = 'storybook-alpha'
export const ACTION_ID = 'storybook-action'
export const ACTION_PATH = `/api/chats/${CHAT_ID}/actions/${ACTION_ID}`

/** A synthetic needs-you action as the engine sends it on the chat row. */
export function chatAction(
  state: ChatActionState,
  kind: ChatActionKind,
  params: Record<string, string> = {},
  error = '',
): ChatAction {
  return { id: ACTION_ID, kind, origin: 'engine', state, params, error, created_at: 1 }
}

/** A declared write that never answers: the control stays pending until teardown. */
export function pendingWrite(verb: string): FixtureRoute {
  return { method: 'POST', path: `${ACTION_PATH}/${verb}`, respond: hold }
}

/** A declared write the engine refuses with a plain error payload. */
export function refusedWrite(verb: string, error: string): FixtureRoute {
  return { method: 'POST', path: `${ACTION_PATH}/${verb}`, respond: () => json({ error }, 400) }
}

/** Inert destinations so a card's navigation lands on a visible page and fetches nothing. */
export const settingsDestinations: RouteRecordRaw[] = [
  { path: '/settings', component: inertPage('Settings page') },
  { path: '/settings/advanced', component: inertPage('Advanced settings page') },
  { path: '/settings/workspace', component: inertPage('Workspace settings page') },
]

function inertPage(label: string): Component {
  return defineComponent({ render: () => h('p', { role: 'status' }, label) })
}

export interface BodyOptions {
  /** Renders the body inside a 390px container. */
  narrow?: boolean
  /** Replaces the clipboard write: `ok` resolves, `fail` rejects and the legacy copy refuses too. */
  clipboard?: 'ok' | 'fail'
  /** Stubs `window.open` with an inert tab, so a popup the body opens stays inside the fixture. */
  popup?: boolean
  routerRoutes?: RouteRecordRaw[]
}

function stubBrowser(options: BodyOptions): Array<() => void> {
  const restore: Array<() => void> = []
  if (options.clipboard) {
    const nav = navigator as Navigator & { clipboard?: unknown }
    const writeText = options.clipboard === 'ok'
      ? async () => {}
      : async () => { throw new Error('Synthetic clipboard refusal') }
    Object.defineProperty(nav, 'clipboard', { configurable: true, value: { writeText } })
    restore.push(() => { Reflect.deleteProperty(nav, 'clipboard') })
    if (options.clipboard === 'fail') {
      const doc = document as Document & { execCommand: (command: string) => boolean }
      const original = Object.getOwnPropertyDescriptor(doc, 'execCommand')
      doc.execCommand = () => false
      restore.push(() => {
        if (original) Object.defineProperty(doc, 'execCommand', original)
        else Reflect.deleteProperty(doc, 'execCommand')
      })
    }
  }
  if (options.popup) {
    const original = window.open
    const tab = { close() {}, location: { href: '' }, opener: null as unknown }
    window.open = () => tab as unknown as Window
    restore.push(() => { window.open = original })
  }
  return restore
}

/**
 * Mounts a production needs-you body with the given props as the `/` route of
 * a connected fixture. Emitted `notice` and `error` messages are shown in the
 * wrapper, so a story can assert what the card would be told.
 */
export function renderBody(component: Component, props: Record<string, unknown>, options: BodyOptions = {}) {
  return defineComponent({
    setup() {
      const host = ref<HTMLElement>()
      const fixture = shallowRef<ConnectedFixture>()
      const notice = ref('')
      const alert = ref('')
      const failure = ref('')
      let restore: Array<() => void> = []
      const route = defineComponent({
        render: () => h(component, {
          ...props,
          onNotice: (message: string) => { notice.value = message },
          onError: (message: string) => { alert.value = message },
        }),
      })
      onMounted(() => {
        restore = stubBrowser(options)
        try {
          fixture.value = mountConnected(host.value!, { component: route, routerRoutes: options.routerRoutes })
        } catch (cause) {
          failure.value = String(cause)
        }
      })
      onBeforeUnmount(() => {
        fixture.value?.dispose()
        restore.reverse().forEach(fn => fn())
      })
      return () => h('div', { style: options.narrow ? 'max-width: 390px' : '' }, [
        h('div', { ref: host }),
        notice.value ? h('p', { role: 'status' }, notice.value) : null,
        alert.value ? h('p', { role: 'alert' }, alert.value) : null,
        failure.value ? h('p', { role: 'alert' }, failure.value) : null,
        ...(fixture.value?.boundary.violations ?? []).map(message => h('p', { role: 'alert' }, message)),
      ])
    },
  })
}
