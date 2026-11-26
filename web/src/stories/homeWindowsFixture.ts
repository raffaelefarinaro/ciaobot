import type { StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import { defineComponent, h, onBeforeUnmount, onMounted, ref, shallowRef, type Component } from 'vue'
import { useRoute, type RouteRecordRaw } from 'vue-router'
import { resetWelcomeForTests } from '../composables/useWelcome'
import { resetHomeNoticeWindows } from '../composables/useHomeNoticeWindows'
import { resetHostTerminalForTests } from '../composables/useHostTerminal'
import { _resetInstallPromptForTests } from '../lib/installPrompt'
import { pendingConfirm } from '../lib/confirm'
import { useProjectStore } from '../stores/projects'
import { mountConnected, type ConnectedFixture } from './mountConnected'

/** Synthetic workspace and paths only: no real names, tokens or user text. */
export const homeWorkspace = 'storybook-alpha'
export const narrowStyle = 'max-width: 390px'

export function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/** A request whose response never arrives, for a real blocked/busy state. */
export function held(): Promise<never> {
  return new Promise<never>(() => {})
}

/** Module-level notice, welcome and host state must not leak between stories. */
export function resetHomeState(): void {
  pendingConfirm.value?.resolve(false)
  resetHomeNoticeWindows()
  resetWelcomeForTests()
  resetHostTerminalForTests()
  _resetInstallPromptForTests()
}

function destination() {
  return defineComponent({
    setup() {
      const route = useRoute()
      return () => h('p', `Workshop destination: ${route.fullPath}`)
    },
  })
}

/** Inert router destinations: no bootstrap, no fetches, no app orchestration. */
export function homeRoutes(): RouteRecordRaw[] {
  return ['/onboarding', '/settings', '/settings/advanced', '/settings/providers', '/settings/access',
    '/memory/unsure', '/stats', '/chat/:chatId'].map(path => ({ path, component: destination() }))
}

export type HomeMount = ConnectedFixture

export function mountHome(
  host: HTMLElement,
  component: Component,
  options: { workspace?: string } = {},
): HomeMount {
  return mountConnected(host, {
    component,
    routerRoutes: homeRoutes(),
    seed: pinia => {
      if (options.workspace) useProjectStore(pinia).activeWorkspace = options.workspace
    },
  })
}

type Canvas = Parameters<NonNullable<StoryObj['play']>>[0]['canvas']

/** The visible ledger makes a fail-closed request observable in every story. */
export async function assertFenceQuiet(canvas: Canvas, minimumRequests = 1): Promise<void> {
  const ledger = canvas.getByRole('status', { name: 'Fixture ledger' })
  await waitFor(() => {
    const count = Number(/Fixture requests: (\d+)/.exec(ledger.textContent ?? '')?.[1] ?? 0)
    expect(count).toBeGreaterThanOrEqual(minimumRequests)
  })
  // The runner's own control traffic settles after the first component read.
  await new Promise(resolve => setTimeout(resolve, 500))
  const networkViolations = canvas.queryAllByRole('alert')
    .map(node => node.textContent ?? '')
    .filter(text => text.includes('Story network violation'))
  expect(`${ledger.textContent ?? ''}\n${networkViolations.join('\n')}`).toMatch(/Violations: 0\./)
  expect(canvas.queryByText(/Story network violation/)).toBeNull()
}

type RenderOptions = {
  label: string
  workspace?: string
  narrow?: boolean
  /** Start the fresh memory router at a query-bearing route (notice links). */
  route?: string
  /** The app's explicit recovery target, for focus-after-close assertions. */
  recovery?: boolean
  /** A workshop stand-in for the app's confirm dialog host. */
  confirm?: boolean
  beforeMount?: () => void | (() => void)
}

/** Host the production owner in a fresh app, with its declared fixture ledger. */
export function renderHome(component: Component, options: RenderOptions) {
  return defineComponent({
    setup() {
      const host = ref<HTMLElement>()
      const fixture = shallowRef<HomeMount>()
      const error = ref('')
      let undoSetup: (() => void) | void
      onMounted(() => {
        try {
          resetHomeState()
          undoSetup = options.beforeMount?.()
          fixture.value = mountHome(host.value!, component, { workspace: options.workspace })
          if (options.route) void fixture.value.router.replace(options.route)
        } catch (cause) {
          error.value = String(cause)
        }
      })
      onBeforeUnmount(() => {
        fixture.value?.dispose()
        undoSetup?.()
      })
      return () => h('div', options.narrow ? { style: narrowStyle } : {}, [
        h('p', options.label),
        h('div', { ref: host }),
        options.recovery
          ? h('button', {
            type: 'button',
            'data-home-notices-reopen': '',
            'aria-label': 'Show closed notices',
          }, 'Show closed notices')
          : null,
        options.confirm && pendingConfirm.value
          ? h('button', {
            type: 'button',
            onClick: () => pendingConfirm.value?.resolve(true),
          }, 'Confirm workshop dialog')
          : null,
        h('p', {
          role: 'status',
          'aria-label': 'Fixture ledger',
        }, `Fixture requests: ${fixture.value?.boundary.requests.length ?? 0}. Violations: ${fixture.value?.boundary.violations.length ?? 0}.`),
        error.value ? h('p', { role: 'alert' }, error.value) : null,
        ...(fixture.value?.boundary.violations ?? []).map(message => h('p', { role: 'alert' }, message)),
      ])
    },
  })
}
