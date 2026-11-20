import { defineComponent, h, onBeforeUnmount, onMounted, ref, shallowRef, type Component, type VNode } from 'vue'
import type { Pinia } from 'pinia'
import { expect, waitFor } from 'storybook/test'
import { useTaskSignalsStore } from '../stores/taskSignals'
import type { Task } from '../lib/types'
import { mountConnected, type ConnectedFixture } from './mountConnected'
import { seedSheetStore, sheetWorkspace } from './taskSheetsFixture'

/**
 * Synthetic fixtures for the "Task notes and peek" rows (#3298, the B13
 * slice): the chat-side origin note and the item peek host. Both read the
 * app-wide task signals and the peek store, so the connected mounts seed those
 * stores and declare only the reads and writes their plays perform. Synthetic
 * names, ids and text only.
 */

/** A host component that renders one production owner with fixed props. */
export function peekHost(render: () => VNode): Component {
  return defineComponent({
    name: 'TaskPeekStoryHost',
    setup: () => render,
  })
}

/**
 * Seed the app-wide task signals the way a loaded read would leave them: the
 * synthetic workspace, its rows, and whether the read has landed. Store state
 * only; the component's own reads and watchers stay untouched.
 */
export function seedTaskSignals(tasks: readonly Task[], loaded: boolean) {
  return (pinia: Pinia): void => {
    seedSheetStore(pinia)
    const signals = useTaskSignalsStore(pinia)
    signals.tasks = [...tasks]
    if (loaded) signals.loadedWorkspace = sheetWorkspace
  }
}

/**
 * The connected view every #3298 story shares: a labelled wrapper, the
 * production owner mounted through `mountConnected`, and a ledger line that
 * names whether the mount landed and what the boundary recorded.
 */
export function peekView(config: {
  label: string
  component: Component
  seed: (pinia: Pinia) => void
  maxWidth?: number
}) {
  return defineComponent({
    name: 'TaskPeekStoryView',
    setup() {
      const host = ref<HTMLElement>()
      const fixture = shallowRef<ConnectedFixture>()
      const error = ref('')
      onMounted(() => {
        try {
          fixture.value = mountConnected(host.value!, { component: config.component, seed: config.seed })
        } catch (cause) {
          error.value = String(cause)
        }
      })
      onBeforeUnmount(() => fixture.value?.dispose())
      return () => h('div', { style: `max-width: ${config.maxWidth ?? 390}px` }, [
        h('p', `Synthetic ${config.label} fixture for the production owner.`),
        h('div', { ref: host }),
        h('p', { role: 'status' }, `Fixture mounted: ${fixture.value ? 'yes' : 'no'}. Fixture requests: ${fixture.value?.boundary.requests.length ?? 0}. Violations: ${fixture.value?.boundary.violations.length ?? 0}.`),
        error.value ? h('p', { role: 'alert' }, error.value) : null,
        ...(fixture.value?.boundary.violations ?? []).map((message) => h('p', { role: 'alert' }, message)),
      ])
    },
  })
}

/**
 * Wait for the mount, then one quiet half second, and require that the
 * boundary still records no violation. Requests are not required: a story
 * with no reads is a valid state.
 */
export async function settlePeek(canvasElement: HTMLElement): Promise<void> {
  await waitFor(() => {
    const ledger = canvasElement.lastElementChild?.textContent ?? ''
    expect(ledger).toMatch(/Fixture mounted: yes\./)
    expect(ledger).toMatch(/Violations: 0\./)
  })
  await new Promise((resolve) => setTimeout(resolve, 500))
  expect(canvasElement.textContent).not.toContain('Story network violation')
  expect(canvasElement.lastElementChild?.textContent ?? '').toMatch(/Violations: 0\./)
}
