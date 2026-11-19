import { defineComponent, h, onBeforeUnmount, onMounted, ref, shallowRef, type Component, type VNode } from 'vue'
import { expect, waitFor } from 'storybook/test'
import type { OnboardingStageProps, StageState } from '../lib/onboarding'
import type { Schedule } from '../lib/types'
import type { FixtureRoute } from './fixtureBoundary'
import { mountConnected, type ConnectedFixture } from './mountConnected'

/**
 * Synthetic connected fixtures for the onboarding stable stages story slice
 * (#3309, B24): `OnboardingLocationStage`, `OnboardingCareStage` and
 * `OnboardingMemoryStage` all mount inside the #3267 boundary — the memory
 * stage reads its preferences through the project store, so even the
 * "props only" owner needs a fresh Pinia and declared routes. Synthetic
 * names, ids, paths and text only.
 */

export const WORKSPACE = 'storybook-alpha'

export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/** A declared read or write that never answers: the stage stays in its held state. */
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

/**
 * Waits for the stage's first declared read to be recorded, then one quiet
 * half second, and requires that the boundary still records no violation.
 * Every stable stage in this slice that mounts with reads has a mount-time
 * read, so a zero count there is a story bug.
 */
export async function settled(canvasElement: HTMLElement): Promise<void> {
  await waitFor(() => expect(canvasElement.textContent).toMatch(/Fixture requests: [1-9]/))
  await new Promise((resolve) => setTimeout(resolve, 500))
  expect(canvasElement.textContent).toMatch(/Violations: 0\./)
  expect(canvasElement.textContent).not.toContain('Story network violation')
}

/**
 * The synthetic installed Workspace care routine the stage looks up by
 * `system-memory-curation@<workspace>` with `scope: 'system'`.
 */
export function careRoutine(overrides: Partial<Schedule> = {}): Schedule {
  return {
    schedule_id: `system-memory-curation@${WORKSPACE}`,
    revision: 'sb-care-1',
    daily_time_utc: '08:00',
    prompt: 'Synthetic workspace care prompt.',
    chat_id: 0,
    created_at: '2026-03-01T09:00:00+00:00',
    timezone_name: 'UTC',
    last_triggered_on: '',
    days_of_week: null,
    thread_id: null,
    context_label: 'Synthetic workspace care',
    frequency: 'weekly',
    interval_minutes: 0,
    last_status: '',
    day_of_month: null,
    run_at_date: null,
    web_chat_id: null,
    web_project_id: null,
    workspace: WORKSPACE,
    model: 'claude',
    next_run: null,
    last_expected_run: null,
    missed: false,
    enabled: true,
    archive_policy: 'manual',
    title: 'Workspace care',
    scope: 'system',
    ...overrides,
  }
}

/** The synthetic workspace registry body the registry re-read after a write answers. */
export function workspacesBody(): unknown {
  return {
    workspaces: [{ name: WORKSPACE, vault_root: '/storybook/alpha', default_provider: 'claude', gws_profile: '' }],
    active: WORKSPACE,
  }
}

/** The onboarding receipt the memory stage reads to decide the offer of the draft. */
export function receipt(status: 'in_progress' | 'completed'): unknown {
  return {
    schema: 1,
    revision: 1,
    installation: { status, step: status === 'in_progress' ? 'access_memory' : 'finish' },
    workspaces: {},
  }
}

/** The handover answer: `off` unless a story overrides it. */
export function handover(handoverValue: 'off' | 'dismiss' | 'all' = 'off', actingAvailable = true): unknown {
  return { workspace: WORKSPACE, handover: handoverValue, acting_available: actingAvailable }
}

type StageExposed = Partial<{ save: () => Promise<boolean>, refresh: () => Promise<void> }>

export type StagePanelConfig = {
  label: string
  component: Component
  props: OnboardingStageProps
  narrow?: boolean
  /**
   * Exposed stage methods the host may drive for the play, e.g. `save`: it is
   * the shell's Continue seam, invoked here outside the stage rather than by a
   * copy of it.
   */
  exposed?: Array<'save' | 'refresh'>
}

/**
 * The production stage mounted as the `/` route of the fixture app. The host
 * prints the latest emitted StageState (the shell's readiness ledger), the
 * boundary's request and violation counts for the plays to read, and one
 * button per requested exposed method.
 */
export function stagePanel(config: StagePanelConfig): VNode {
  return h(defineComponent({
    name: 'OnboardingStagePanel',
    setup() {
      const host = ref<HTMLElement>()
      const fixture = shallowRef<ConnectedFixture>()
      const error = ref('')
      const lastState = ref<StageState | null>(null)
      const stage = shallowRef<StageExposed | null>(null)
      const routed = defineComponent({
        name: 'OnboardingStageRoute',
        setup: () => () => h(config.component, {
          ...config.props,
          ref: stage,
          onState: (value: StageState) => { lastState.value = value },
        }),
      })
      onMounted(() => {
        try {
          fixture.value = mountConnected(host.value!, { component: routed })
        } catch (cause) { error.value = String(cause) }
      })
      onBeforeUnmount(() => fixture.value?.dispose())
      async function run(name: 'save' | 'refresh'): Promise<void> {
        const exposed = stage.value
        if (!exposed) return
        if (name === 'save' && exposed.save) await exposed.save()
        if (name === 'refresh' && exposed.refresh) await exposed.refresh()
      }
      return () => h('div', { style: config.narrow ? 'max-width: 390px' : undefined }, [
        h('p', `Synthetic ${config.label} fixture for the production stage.`),
        h('div', { ref: host }),
        h('p', { role: 'status' }, `StageState: ${lastState.value ? JSON.stringify(lastState.value) : 'none'}.`),
        ...(config.exposed ?? []).map((name) => h(
          'button',
          { type: 'button', class: 'stage-host-button', onClick: () => { void run(name) } },
          `Call stage ${name}`,
        )),
        error.value ? h('p', { role: 'alert' }, error.value) : null,
        ...(fixture.value?.boundary.violations ?? []).map((message) => h('p', { role: 'alert' }, message)),
        h('p', { role: 'status' }, `Fixture requests: ${fixture.value?.boundary.requests.length ?? 0}. Violations: ${fixture.value?.boundary.violations.length ?? 0}.`),
      ])
    },
  }))
}
