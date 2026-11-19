import { defineComponent, h, onBeforeUnmount, onMounted, ref, shallowRef, type Component, type VNode } from 'vue'
import type { Pinia } from 'pinia'
import { expect, waitFor } from 'storybook/test'
import { useProjectStore } from '../stores/projects'
import type { FixtureRoute } from './fixtureBoundary'
import { mountConnected, type ConnectedFixture } from './mountConnected'

/**
 * Synthetic connected fixtures for the Settings access and memory story slice
 * (#3307, B22): `SettingsDevices`, `SettingsMemoryBackup`,
 * `SettingsMemoryHandover` and `SettingsUpdateTasks` each read their own data
 * on mount, so every story mounts the production owner through `mountConnected`
 * with exactly the routes its play performs. Synthetic names, ids and text only.
 */

/** The one synthetic workspace the connected owners are seeded with. */
export const WORKSPACE = 'storybook-alpha'

export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/** A declared read or write that never answers: the owner stays in its held state. */
export function held(): Promise<never> {
  return new Promise<never>(() => {})
}

export function route(
  method: FixtureRoute['method'],
  path: string,
  respond: () => Response | Promise<never>,
): FixtureRoute {
  return { method, path, respond }
}

/** Seeds the one synthetic workspace the project store reads as active. */
export function seedWorkspace(pinia: Pinia): void {
  const store = useProjectStore(pinia)
  store.workspaces = [
    { name: WORKSPACE, vault_root: '/storybook/alpha', default_provider: 'claude', gws_profile: '' },
  ]
  store.activeWorkspace = WORKSPACE
}

/** A stubbed navigator.clipboard: real clipboard access is not a story fact. */
export function clipboardStub(): () => void {
  const original = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: { writeText: async () => {} },
  })
  return () => {
    if (original) Object.defineProperty(navigator, 'clipboard', original)
    else Reflect.deleteProperty(navigator, 'clipboard')
  }
}

export type OwnerPanelConfig = {
  label: string
  component: Component
  seed?: (pinia: Pinia) => void
  /** Stubs navigator.clipboard for the owner's copy buttons. */
  clipboard?: boolean
  /** A 390px wrapper, for the narrow layout. */
  narrow?: boolean
}

/**
 * The production owner mounted as the `/` route of the fixture app. The owner's
 * `open-chat` emit is collected by a route wrapper and drawn as a visible line,
 * so a story can prove the emit without a router. The ledger line reports the
 * boundary's request and violation counts for the play functions to read.
 */
export function ownerPanel(config: OwnerPanelConfig): VNode {
  return h(defineComponent({
    name: 'SettingsAccessOwnerPanel',
    setup() {
      const host = ref<HTMLElement>()
      const fixture = shallowRef<ConnectedFixture>()
      const error = ref('')
      const openedChat = ref('')
      const routed = defineComponent({
        name: 'SettingsAccessOwnerRoute',
        setup: () => () => h(config.component, {
          onOpenChat: (chatId: string) => { openedChat.value = chatId },
        }),
      })
      let restoreClipboard: (() => void) | undefined
      onMounted(() => {
        try {
          if (config.clipboard) restoreClipboard = clipboardStub()
          fixture.value = mountConnected(host.value!, { component: routed, seed: config.seed })
        } catch (cause) { error.value = String(cause) }
      })
      onBeforeUnmount(() => {
        fixture.value?.dispose()
        restoreClipboard?.()
      })
      return () => h('div', { style: config.narrow ? 'max-width: 390px' : undefined }, [
        h('p', `Synthetic ${config.label} fixture for the production owner.`),
        h('div', { ref: host }),
        openedChat.value ? h('p', { role: 'status' }, `Open chat emit: ${openedChat.value}`) : null,
        error.value ? h('p', { role: 'alert' }, error.value) : null,
        ...(fixture.value?.boundary.violations ?? []).map((message) => h('p', { role: 'alert' }, message)),
        h('p', { role: 'status' }, `Fixture requests: ${fixture.value?.boundary.requests.length ?? 0}. Violations: ${fixture.value?.boundary.violations.length ?? 0}.`),
      ])
    },
  }))
}

/**
 * Waits for the owner's first declared read to be recorded, then one quiet
 * half second, and requires that the boundary still records no violation.
 * Every connected owner in this slice reads on mount, so a zero count is a
 * story bug.
 */
export async function settled(canvasElement: HTMLElement): Promise<void> {
  await waitFor(() => expect(canvasElement.textContent).toMatch(/Fixture requests: [1-9]/))
  await new Promise((resolve) => setTimeout(resolve, 500))
  expect(canvasElement.textContent).toMatch(/Violations: 0\./)
  expect(canvasElement.textContent).not.toContain('Story network violation')
}

/** A read that answers `first` once and then refuses, for a refresh over data already on screen. */
export function firstThenFailing(first: unknown, failure: string): () => Response {
  let calls = 0
  return () => {
    calls += 1
    return calls === 1 ? json(first) : json({ error: failure }, 503)
  }
}
