import { defineComponent, h, nextTick, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import { expect, waitFor } from 'storybook/test'
import type { Component } from 'vue'
import type { Pinia } from 'pinia'
import PinnedFilePanel from '../components/PinnedFilePanel.vue'
import CommentComposePopover from '../components/CommentComposePopover.vue'
import { mountConnected } from './mountConnected'
import { useFileViewerStore } from '../stores/fileViewer'
import { useProjectStore } from '../stores/projects'
import type { FixtureRoute } from './fixtureBoundary'
import { syntheticListing, type SyntheticListing } from './fileViewerFixture'

/**
 * Synthetic fixtures for the pins-and-comments stories (#3303, slice B18).
 * Names, paths and ids are invented (`storybook-*`); no real paths, tokens or
 * user text. Every request a connected story can produce is answered by one of
 * the declared route builders below (plus the shared file-viewer builders);
 * anything else fails closed.
 */

export const SB_CHAT_ID = 'chat-sb-1'
/** The folder pin the chat tile starts on (one trailing slash, pin contract). */
export const SB_FOLDER_PIN = '/notes/storybook-drafts/'
export const SB_FOLDER_PATH = '/notes/storybook-drafts'
export const SB_SUB_PATH = '/notes/storybook-drafts/storybook-sub'
export const SB_REPORT_PATH = '/notes/storybook-drafts/storybook-report.txt'
export const SB_NOTE_PATH = '/notes/storybook-note.txt'
export const SB_MD_PLAIN_PATH = '/notes/storybook-note.md'
export const SB_PPTX_PATH = '/docs/storybook-slides.pptx'

export const SB_NOTE_CONTENT = 'first note line\nsecond note line\nthird note line\n'
export const SB_NOTE_REFRESHED = 'fresh note line\n'
export const SB_REPORT_CONTENT = 'report line one\nreport line two\n'
export const SB_MD_PLAIN_CONTENT = [
  '# Storybook Note',
  '',
  'A synthetic paragraph with no frontmatter.',
  '',
].join('\n')
export const SB_CSV_BASE = 'item,count\nalpha,1\nbeta,2\n'

/** The comment seeded on a .pfp-body line for the read-popover and edit flows. */
export const SB_COMMENT_ID = 'sb-c-1'
export const SB_COMMENT_NOTE = 'A synthetic pinned note.'
export const SB_COMMENT_LINE = 2

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { 'Content-Type': 'application/json' },
})
const text = (body: string, status = 200) => new Response(body, {
  status,
  headers: { 'Content-Type': 'text/plain; charset=utf-8' },
})

/** A declaration whose respond() never resolves: the request is held until teardown, as native loading would be. */
export const held = () => new Promise<never>(() => {})

/** One call-counting responder: the first read answers `first`, later reads answer `then`. */
export function workspaceFileThenRoute(path: string, first: string, then: string): FixtureRoute {
  let calls = 0
  return {
    method: 'GET',
    path: `/api/workspace-file?path=${encodeURIComponent(path)}`,
    respond: () => text(++calls === 1 ? first : then),
  }
}

/** A dir listing answered with the chat context the docked tile always carries. */
export function workspaceDirChatRoute(path: string, chatId: string, listing: SyntheticListing): FixtureRoute {
  return {
    method: 'GET',
    path: `/api/workspace-dir?path=${encodeURIComponent(path)}&chat_id=${chatId}`,
    respond: () => json(listing),
  }
}

export function folderListing(path: string, entries: SyntheticListing['entries'], truncated = false): SyntheticListing {
  return { ...syntheticListing(path, entries), truncated }
}

export function markdownPathsRoute(paths: string[]): FixtureRoute {
  return { method: 'GET', path: '/api/vault-markdown-paths', respond: () => json({ paths }) }
}

export function libreofficeStatusRoute(available: boolean): FixtureRoute {
  return { method: 'GET', path: '/api/libreoffice-status', respond: () => json({ available }) }
}

// ── Store seeding ──────────────────────────────────────────────────────────

export type PanelSeed = {
  /** Durable file comments, written onto the project store's map. */
  comments?: { path: string, id: string, comment: string, lineStart?: number | null }[]
  /** The tiles' pinned paths (project-scoped pin ownership: no chat rows are seeded, so a dock pin write stays local and needs no PATCH route). */
  pins?: Record<string, string>
  /** The layout's dock target, which routes an open onto the chat tile instead of the viewer. */
  dock?: string
}

/** Clear this page's `ciao-*` keys the way the file-viewer fixture does, then seed the stores. */
export function seedPanelStores(pinia: Pinia, seed: PanelSeed = {}): void {
  const stale: string[] = []
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i)
    if (key && (key === 'vuefinder_config_ciao-folder-browser' || key.startsWith('ciao-'))) stale.push(key)
  }
  for (const key of stale) localStorage.removeItem(key)
  const projects = useProjectStore(pinia)
  projects.workspaces = [{ name: 'storybook-alpha', vault_root: 'storybook-alpha', default_provider: 'claude' }]
  projects.projects = [{
    project_id: 'storybook-p1',
    name: 'Synthetic project storybook-p1',
    workspace: 'storybook-alpha',
    context: '',
    created_at: '2026-11-01T00:00:00.000Z',
    order: 0,
    vault_folder: 'storybook-p1',
  }]
  projects.activeWorkspace = 'storybook-alpha'
  projects.activeChatId = SB_CHAT_ID
  projects.chats = []
  for (const c of seed.comments ?? []) {
    projects.fileComments = {
      ...projects.fileComments,
      [c.path]: [{
        id: c.id,
        path: c.path,
        selection: c.lineStart ? `line ${c.lineStart}` : '',
        comment: c.comment,
        lineStart: c.lineStart ?? null,
        lineEnd: c.lineStart ?? null,
        colIndex: null,
        createdAt: '2026-11-19T00:00:00.000Z',
      }],
    }
  }
  for (const [id, path] of Object.entries(seed.pins ?? {})) void projects.pinFile(id, path)
  const viewer = useFileViewerStore(pinia)
  viewer.isOpen = false
  viewer.dockTarget = seed.dock ?? ''
}

// ── Fixtures registry ─────────────────────────────────────────────────────

const registry = new Map<string, Pinia>()
let fixtureSeq = 0

/** The list row VueFinder renders for one explorer entry name. */
export function listRow(name: string): HTMLElement {
  const rows = [...document.querySelectorAll<HTMLElement>('.vuefinder__explorer__item-list-content')]
  const found = rows.find(row => row.textContent?.includes(name))
  if (!found) throw new TypeError(`No VueFinder list row for ${name}`)
  return found
}

export function storesByFixtureId(id: string): {
  projects: ReturnType<typeof useProjectStore>
  viewer: ReturnType<typeof useFileViewerStore>
} {
  const pinia = registry.get(id)
  if (!pinia) throw new Error(`Unknown pins fixture: ${id}`)
  return { projects: useProjectStore(pinia), viewer: useFileViewerStore(pinia) }
}

export function fixtureId(scope: unknown): string {
  const found = (scope as { querySelector?: (selector: string) => Element | null }).querySelector?.('[data-fixture-id]')
  const id = found?.getAttribute('data-fixture-id')
  if (!id) throw new Error('No pins fixture host found')
  return id
}

export function panelStores(scope: unknown) {
  return storesByFixtureId(fixtureId(scope))
}

function ledgerLine(mounted: ReturnType<typeof mountConnected> | undefined): string {
  return `Fixture requests: ${mounted?.boundary.requests.length ?? 0}. Violations: ${mounted?.boundary.violations.length ?? 0}.`
}

// ── The connected shell ───────────────────────────────────────────────────

type ShellOptions = {
  /** The host's box sizing; `narrow` is the 390px wrapper the narrow state uses. */
  hostBox?: string
  seed?: (pinia: Pinia) => void
  /** Render the owner into a div the shell fills; the owner reads only seeded stores and its own callers. */
  renderOwner: () => Component
}

function connectedShell(label: string, options: ShellOptions) {
  return defineComponent({
    name: 'PinsConnectedStory',
    setup() {
      const host = ref<HTMLElement>()
      const mounted = shallowRef<ReturnType<typeof mountConnected>>()
      const ready = ref(false)
      const error = ref('')
      const fixtureId = ref('')
      onMounted(() => {
        try {
          host.value!.dataset.fixtureId = `pins-fixture-${++fixtureSeq}`
          fixtureId.value = host.value!.dataset.fixtureId
          mounted.value = mountConnected(host.value!, {
            component: options.renderOwner(),
            seed: pinia => {
              seedPanelStores(pinia)
              options.seed?.(pinia)
              registry.set(fixtureId.value, pinia)
            },
          })
          void mounted.value.router.isReady().then(() => nextTick()).then(() => { ready.value = true })
        } catch (cause) { error.value = String(cause) }
      })
      onBeforeUnmount(() => {
        registry.delete(fixtureId.value)
        mounted.value?.dispose()
      })
      return () => h('div', { style: 'display: flex; flex-direction: column; gap: 4px; padding: 4px' }, [
        h('p', `Synthetic ${label}.`),
        h('div', { ref: host, style: options.hostBox ?? 'min-height: 420px' }),
        h('p', { role: 'status', 'data-testid': 'story-ledger' }, ledgerLine(mounted.value)),
        h('p', { 'data-testid': 'story-ready' }, ready.value ? 'Fixture ready.' : 'Fixture mounting.'),
        h('p', { 'data-testid': 'story-fixtures', style: 'display: none' }, fixtureId.value),
        error.value
          ? h('p', { role: 'alert', 'data-testid': 'story-error' }, error.value)
          : null,
        ...(mounted.value?.boundary.violations ?? []).map(message =>
          h('p', { role: 'alert', 'data-testid': 'story-violation' }, message)),
      ])
    },
  })
}

/** Prove the owner mounted, attempted nothing undeclared, and the fence recorded no violation. */
export async function assertQuietFence(canvas: {
  getByTestId: (id: string) => HTMLElement
  queryByTestId: (id: string) => HTMLElement | null
}): Promise<void> {
  await waitFor(() => expect(canvas.getByTestId('story-ready').textContent).toBe('Fixture ready.'))
  expect(canvas.queryByTestId('story-error')).toBeNull()
  expect(canvas.queryByTestId('story-violation')).toBeNull()
  await waitFor(() => expect(canvas.getByTestId('story-ledger').textContent).toContain('Violations: 0.'))
}

// ── The docked tile host ──────────────────────────────────────────────────

export type PanelRender = {
  /** The pinned path prop the tile starts with. */
  path: string
  /** When set, the host carries the parent's pin-prop channel: one button applies the path ChatLayout applies after a dock pin write lands. */
  repinTo?: string
  closeLabel?: string
  inMemoryMap?: boolean
  closeDisabled?: boolean
  narrow?: boolean
  seed?: PanelSeed
}

/**
 * The docked tile host: the production PinnedFilePanel mounts inside
 * mountConnected with seeded stores, and the ledger line reports the boundary
 * state. `repinTo` renders one labelled button that performs the only parent
 * job a story cannot run — applying the pin-prop change after a dock pin write
 * resolves — while the panel's own pendingReturn bookkeeping, its file load
 * and the Back control remain real production code paths.
 */
export function renderPinnedFilePanel(note: string, render: PanelRender) {
  const path = ref(render.path)
  return connectedShell(note, {
    hostBox: render.narrow ? 'width: 390px; height: 520px' : 'min-height: 420px',
    seed: pinia => {
      seedPanelStores(pinia, render.seed)
    },
    renderOwner: () => defineComponent({
      name: 'PinsPanelOwner',
      setup() {
        return () => [
          h(PinnedFilePanel, {
            'filePath': path.value,
            'onClose': () => {},
            'inMemoryMap': render.inMemoryMap ?? false,
            ...(render.closeLabel ? { closeLabel: render.closeLabel } : {}),
            'closeDisabled': render.closeDisabled ?? false,
          }),
          render.repinTo
            ? h('button', {
                'type': 'button',
                'data-testid': 'story-repin',
                'onClick': () => { path.value = render.repinTo! },
              }, 'Apply the parent pin write')
            : null,
        ]
      },
    }),
  })
}

// ── The compose popover host ──────────────────────────────────────────────

export type ComposeRender = {
  anchor?: { top: number, left: number } | null
  modelValue?: string
  images?: string[]
  inline?: boolean
}

/**
 * The compose popover is teleported to <body> (unless inline), so plays assert
 * against the document; the visible event log records what the popover emitted
 * without the story owning any state of its own.
 */
export function renderComposeState(note: string, render: ComposeRender = {}) {
  return connectedShell(note, {
    hostBox: 'min-height: 240px',
    renderOwner: () => {
      const Owner = defineComponent({
        name: 'CommentComposeOwner',
        setup() {
          const model = ref(render.modelValue ?? '')
          const log = ref<string[]>([])
          function record(entry: string): void { log.value = [...log.value, entry] }
          return () => [
            h(CommentComposePopover, {
              'anchor': render.anchor ?? null,
              'modelValue': model.value,
              'onUpdate:modelValue': (value: string) => { model.value = value; record(`input:${value}`) },
              'images': render.images,
              'inline': render.inline ?? false,
              'onCancel': () => record('cancel'),
              'onSave': () => record('save'),
              'onRemoveImage': (index: number) => record(`removeImage:${index}`),
            }),
            h('p', { 'data-testid': 'story-events' }, `Events: ${log.value.join(' | ') || 'none'}`),
          ]
        },
      })
      return Owner
    },
  })
}
