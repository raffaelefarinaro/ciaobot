import type { StoryObj } from '@storybook/vue3-vite'
import { userEvent, waitFor, expect } from 'storybook/test'
import { defineComponent, h, onBeforeUnmount, onMounted, ref, shallowRef, type Component } from 'vue'
import type { Pinia } from 'pinia'
import type { ProjectInfo, WorkspaceInfo } from '../lib/types'
import { useProjectStore } from '../stores/projects'
import type { FixtureRoute } from './fixtureBoundary'
import { mountConnected, type ConnectedFixture } from './mountConnected'
import { held, homeRoutes, json, narrowStyle, resetHomeState } from './homeWindowsFixture'

/** Synthetic workspaces, projects and models only: no real names, paths or tokens. */
export const intakeWorkspace = 'storybook-alpha'
export const intakeEmptyWorkspace = 'storybook-beta'
export const intakeProjectId = `storybook-project-${intakeWorkspace}`
export const intakeModels = ['storybook-model-alpha', 'storybook-model-beta']

export type ModelsVariant = 'loaded' | 'empty' | 'failed' | 'loading'
export type Canvas = Parameters<NonNullable<StoryObj['play']>>[0]['canvas']

function commandsRoute(workspace: string): FixtureRoute {
  return {
    method: 'GET',
    path: `/api/commands?provider=claude&workspace=${workspace}`,
    respond: () => json({ commands: [], skills: [] }),
  }
}

function modelsRoute(variant: ModelsVariant, workspace = intakeWorkspace): FixtureRoute {
  return {
    method: 'GET',
    path: `/api/models?workspace=${workspace}`,
    respond: (): Response | Promise<never> => {
      if (variant === 'loading') return held()
      if (variant === 'failed') return json({ error: 'Synthetic models unavailable' }, 503)
      if (variant === 'empty') return json({ models: [], default: '', provider_models: {}, provider_defaults: {} })
      return json({
        models: intakeModels,
        default: intakeModels[0],
        provider_models: { claude: intakeModels },
        provider_defaults: { claude: intakeModels[0] },
      })
    },
  }
}

/** A new-chat create call whose response never arrives: the real busy state of the start button. */
function createChatRoute(): FixtureRoute {
  return { method: 'POST', path: `/api/projects/${intakeProjectId}/chats`, respond: () => held() }
}

/** Exact declared reads: the slash-command loader per workspace, and the model list per workspace
 * (Home reads the active workspace's catalog on mount and after every switch, for its chip). */
export function intakeRoutes(options: { models?: ModelsVariant, starting?: boolean } = {}): FixtureRoute[] {
  return [
    commandsRoute(intakeWorkspace),
    commandsRoute(intakeEmptyWorkspace),
    modelsRoute(options.models ?? 'loaded'),
    modelsRoute('loaded', intakeEmptyWorkspace),
    ...(options.starting ? [createChatRoute()] : []),
  ]
}

function workspaceInfo(name: string): WorkspaceInfo {
  return { name, default_provider: 'claude', vault_root: '' } as WorkspaceInfo
}

function projectInfo(workspace: string): ProjectInfo {
  return {
    project_id: `storybook-project-${workspace}`,
    name: 'General',
    workspace,
    context: '',
    created_at: '',
    order: 0,
    vault_folder: '',
  } as ProjectInfo
}

export type SeededOptions = {
  /** Start on this workspace; the default is the one with a project. */
  workspace?: string
  /** False seeds the workspace with no project, the real no-project block. */
  projects?: boolean
  seed?: (pinia: Pinia) => void
}

/**
 * Mounts the production owner as a connected owner with the seeded store
 * state a real workspace would hold. Seeds store state only: no watcher,
 * client or fetch is started by the fixture.
 */
export function mountSeeded(host: HTMLElement, component: Component, options: SeededOptions = {}): ConnectedFixture {
  return mountConnected(host, {
    component,
    routerRoutes: homeRoutes(),
    seed: pinia => {
      const store = useProjectStore(pinia)
      store.workspaces = [workspaceInfo(intakeWorkspace), workspaceInfo(intakeEmptyWorkspace)]
      store.workspaceRegistryStatus = 'ready'
      store.projects = options.projects === false ? [] : [projectInfo(intakeWorkspace)]
      store.activeWorkspace = options.workspace ?? intakeWorkspace
      options.seed?.(pinia)
    },
  })
}

/** Sets one query parameter on the workshop URL for this mount and restores the URL on teardown. */
export function setQueryForMount(name: string, value: string): () => void {
  const original = window.location.href
  const url = new URL(original)
  url.searchParams.set(name, value)
  window.history.replaceState(window.history.state, '', url)
  return () => window.history.replaceState(window.history.state, '', original)
}

export type RenderOptions = SeededOptions & {
  label: string
  narrow?: boolean
  /** Shows the workshop's own workspace switch, so a play can change the active workspace. */
  switcher?: boolean
  beforeMount?: () => void | (() => void)
}

/** Hosts a seeded owner with the fixture ledger every intake story reports through. */
export function renderSeeded(component: Component, options: RenderOptions) {
  return defineComponent({
    setup() {
      const host = ref<HTMLElement>()
      const fixture = shallowRef<ConnectedFixture>()
      const error = ref('')
      let undoSetup: (() => void) | void
      onMounted(() => {
        try {
          resetHomeState()
          undoSetup = options.beforeMount?.()
          fixture.value = mountSeeded(host.value!, component, options)
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
        options.switcher
          ? h('label', [
            'Fixture workspace ',
            h('select', {
              'aria-label': 'Fixture workspace',
              onChange: (event: Event) => {
                if (fixture.value) useProjectStore(fixture.value.pinia).activeWorkspace = (event.target as HTMLSelectElement).value
              },
            }, [intakeWorkspace, intakeEmptyWorkspace].map(value => h('option', { value }, value))),
          ])
          : null,
        h('div', { ref: host }),
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

export function promptBox(canvas: Canvas): HTMLTextAreaElement {
  return canvas.getByRole('textbox', { name: 'Ask Ciao to do something' }) as HTMLTextAreaElement
}

/** Stages one synthetic file through the composer's own file input, as the attach button does. */
export function stageSyntheticFile(container: HTMLElement, name: string): void {
  const input = container.querySelector<HTMLInputElement>('input[type="file"]')
  if (!input) throw new Error('The composer file input is missing')
  const transfer = new DataTransfer()
  transfer.items.add(new File(['synthetic'], name, { type: 'text/plain' }))
  input.files = transfer.files
  input.dispatchEvent(new Event('change', { bubbles: true }))
}

/** The owner mounts after the fixture router resolves, so the trigger is awaited, not read at once. */
export async function openModelPicker(canvas: Canvas): Promise<void> {
  await userEvent.click(await canvas.findByRole('button', { name: /Model for the new chat/ }))
}

/** A typed draft, then the attachment list's remove control, used by every attachments state. */
export async function expectAttachmentRoundTrip(canvas: Canvas, container: HTMLElement): Promise<void> {
  stageSyntheticFile(container, 'storybook-alpha-notes.txt')
  const list = await canvas.findByRole('list', { name: 'Attachments' })
  expect(list.textContent).toContain('storybook-alpha-notes.txt')
  await userEvent.click(canvas.getByRole('button', { name: 'Remove storybook-alpha-notes.txt' }))
  await waitFor(() => expect(canvas.queryByRole('list', { name: 'Attachments' })).toBeNull())
}
