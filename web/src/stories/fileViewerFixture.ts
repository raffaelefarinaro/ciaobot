import { defineComponent, h, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import FileViewerModal from '../components/FileViewerModal.vue'
import { mountConnected } from './mountConnected'
import { useFileViewerStore } from '../stores/fileViewer'
import { useProjectStore } from '../stores/projects'
import type { Pinia } from 'pinia'
import type { FixtureBoundary, FixtureRoute } from './fixtureBoundary'

/**
 * Synthetic fixtures for the file-viewer stories (#3302, slice B17). Names,
 * paths and ids are invented (`storybook-*`); no real paths, tokens or user
 * text. Every request a connected story can produce is answered by one of the
 * declared route builders below; anything else fails closed.
 */

export const SB_TEXT_PATH = '/notes/storybook-alpha.txt'
export const SB_MD_PATH = '/notes/storybook-alpha.md'
export const SB_CSV_PATH = '/notes/storybook-alpha.csv'
export const SB_HTML_PATH = '/notes/storybook-alpha.html'
export const SB_PDF_PATH = '/docs/storybook-report.pdf'
export const SB_PPTX_PATH = '/docs/storybook-slides.pptx'
export const SB_IMG_PATH = '/media/storybook-figure.png'
export const SB_CHAT_ID = 'chat-sb-1'
export const SB_PROJECT_KEY = 'sb-project-1'
export const SB_LOGS_PATH = `/notes/memory-vault/Logs/Chats/${SB_CHAT_ID}/storybook-provider/storybook-session.md`

export const SB_TEXT_CONTENT = 'first line\nsecond line\nthird line\n'
export const SB_TEXT_CONTENT_REFRESHED = 'refreshed line one\nrefreshed line two\n'
export const SB_MD_CONTENT = [
  '---',
  'name: Storybook Alpha',
  'type: note',
  'status: draft',
  'summary: A synthetic document for the workshop.',
  'tags: [storybook, fixture]',
  '---',
  '',
  '# Storybook Alpha',
  '',
  'A synthetic paragraph.',
  '',
].join('\n')
export const SB_CSV_PLAIN = '1,2,3\n4,5,6\n'
export const SB_CSV_HEADERED = 'fruit,count\napples,3\npears,4\n'
export const SB_HTML_SOURCE = [
  '<!doctype html>',
  '<html><body>',
  '<p>Fixed inert storybook artifact.</p>',
  '</body></html>',
  '',
].join('\n')

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: { 'Content-Type': 'application/json' },
})
const text = (body: string, status = 200) => new Response(body, {
  status,
  headers: { 'Content-Type': 'text/plain; charset=utf-8' },
})

/** A declaration whose respond() never resolves: the request is held until teardown, as native loading would be. */
const held = () => new Promise<never>(() => {})

export function workspaceFileRoute(path: string, body: string): FixtureRoute {
  return { method: 'GET', path: `/api/workspace-file?path=${encodeURIComponent(path)}`, respond: () => text(body) }
}

export function workspaceFileStatusRoute(path: string, status: number): FixtureRoute {
  return { method: 'GET', path: `/api/workspace-file?path=${encodeURIComponent(path)}`, respond: () => json({ error: `synthetic ${status}` }, status) }
}

export function workspaceImageRoute(path: string, status: number): FixtureRoute {
  return { method: 'GET', path: `/api/workspace-image?path=${encodeURIComponent(path)}&t=0`, respond: () => json({ error: `synthetic ${status}` }, status) }
}

export function chatFilePathRoute(chatId: string, fromPath: string, toPath: string): FixtureRoute {
  return {
    method: 'GET',
    path: `/api/chats/${chatId}/file-path?path=${encodeURIComponent(fromPath)}`,
    respond: () => json({ path: toPath }),
  }
}

export function saveFileRoute(status: number | 'held'): FixtureRoute {
  if (status === 'held') return { method: 'POST', path: '/api/workspace-file', respond: held }
  return { method: 'POST', path: '/api/workspace-file', respond: () => json({ error: `synthetic ${status}` }, status) }
}

export function patchPinRoute(chatId: string): FixtureRoute {
  return { method: 'PATCH', path: `/api/chats/${chatId}`, respond: held }
}

export function continueChatRoute(chatId: string, status = 500): FixtureRoute {
  return { method: 'POST', path: `/api/chats/${chatId}/continue`, respond: () => json({ error: `synthetic ${status}` }, status) }
}

export type SyntheticListing = {
  path: string
  root: { name: string; path: string }
  roots: Array<{ name: string; path: string }>
  parent: string | null
  entries: Array<{ name: string; path: string; type: 'dir' | 'file'; size: number | null; mtime: number; ext: string }>
  truncated: boolean
}

export function syntheticListing(path: string, entries: SyntheticListing['entries']): SyntheticListing {
  const root = { name: 'storybook-root', path: '/notes' }
  return { path, root, roots: [root], parent: null, entries, truncated: false }
}

export function workspaceDirRoute(path: string, listing: SyntheticListing): FixtureRoute {
  return { method: 'GET', path: `/api/workspace-dir?path=${encodeURIComponent(path)}`, respond: () => json(listing) }
}

// ── Store seeding ──────────────────────────────────────────────────────────

export type FileViewerSeed = {
  isOpen?: boolean
  kind?: 'text' | 'image' | 'pdf' | 'html' | 'folder'
  path?: string
  line?: number | null
  content?: string
  imageUrl?: string
  loading?: boolean
  error?: string
  chatId?: string | null
  dockTarget?: string
  editing?: boolean
  editBuffer?: string
  editSaving?: boolean
  editError?: string
  htmlView?: 'preview' | 'code'
  sourceLoading?: boolean
  sourceError?: string
  sourceLoaded?: boolean
  pptxNeedsLibreoffice?: boolean
  loadToken?: number
}

/** An attachment-shaped image URL the viewer renders as served chat images do. */
export const SB_IMAGE_DATA_URL = 'data:image/svg+xml;charset=utf-8,'
  + encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="80"><rect width="120" height="80" fill="#8fd0ff"/><text x="12" y="46" font-size="14">storybook</text></svg>',
  )

/**
 * Clear this page's `ciao-*` keys, then seed the file-viewer and project
 * stores. Runs before the store is created inside `mountConnected`, so the
 * project store hydrates from a clean local slate.
 */
export function seedFileViewer(pinia: Pinia, seed: FileViewerSeed = {}): void {
  // Clear the keys this fixture's stores persist to (and VueFinder stores its
  // config to), collected first because removal shifts live indices.
  const stale: string[] = []
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i)
    if (key && (key === 'vuefinder_config_ciao-folder-browser' || key.startsWith('ciao-'))) stale.push(key)
  }
  for (const key of stale) localStorage.removeItem(key)
  const viewer = useFileViewerStore(pinia)
  viewer.isOpen = seed.isOpen ?? false
  viewer.kind = seed.kind ?? 'text'
  viewer.path = seed.path ?? ''
  viewer.line = seed.line ?? null
  viewer.content = seed.content ?? ''
  viewer.imageUrl = seed.imageUrl ?? ''
  viewer.loading = seed.loading ?? false
  viewer.error = seed.error ?? ''
  viewer.chatId = seed.chatId === null ? '' : seed.chatId ?? ''
  viewer.editing = seed.editing ?? false
  viewer.editBuffer = seed.editBuffer ?? ''
  viewer.editSaving = seed.editSaving ?? false
  viewer.editError = seed.editError ?? ''
  viewer.htmlView = seed.htmlView ?? 'preview'
  viewer.sourceLoading = seed.sourceLoading ?? false
  viewer.sourceError = seed.sourceError ?? ''
  viewer.sourceLoaded = seed.sourceLoaded ?? false
  viewer.pptxNeedsLibreoffice = seed.pptxNeedsLibreoffice ?? false
  viewer.dockTarget = seed.dockTarget ?? ''
  viewer.loadToken = seed.loadToken ?? 0
}

/** Seed one durable comment on a line (the pre-branch highlight shape). */
export function seedFileComment(
  projects: ReturnType<typeof useProjectStore>,
  path: string,
  comment: { id: string; comment: string; lineStart?: number | null; colIndex?: number | null },
): void {
  projects.fileComments = {
    ...projects.fileComments,
    [path]: [{
      id: comment.id,
      path,
      selection: comment.lineStart ? `line ${comment.lineStart}` : '',
      comment: comment.comment,
      lineStart: comment.lineStart ?? null,
      lineEnd: comment.lineStart ?? null,
      colIndex: comment.colIndex ?? null,
      createdAt: '2026-11-19T00:00:00.000Z',
    }],
  }
}

// ── The connected story harness ────────────────────────────────────────────

/**
 * Registry the play functions read store state through. The dialog's fixed
 * backdrop covers the whole story page, so plays cannot click wrapper-level
 * controls; they act on the stores directly, exactly as the app would. Each
 * mount registers its Pinia, and dispose removes it again.
 */
const registry = new Map<string, Pinia>()
let fixtureSeq = 0

export type FileViewerFixture = {
  /** DOM id of this mount: plays resolve the stores with `fixtureStoresById`. */
  id: string
  boundary: FixtureBoundary
  dispose: () => void
}

export function fixtureStoresById(id: string): {
  viewer: ReturnType<typeof useFileViewerStore>
  projects: ReturnType<typeof useProjectStore>
} {
  const pinia = registry.get(id)
  if (!pinia) throw new Error(`Unknown file-viewer fixture: ${id}`)
  return { viewer: useFileViewerStore(pinia), projects: useProjectStore(pinia) }
}

export function mountFileViewer(host: HTMLElement, seed: FileViewerSeed = {}, extra?: (stores: ReturnType<typeof fixtureStoresById>) => void): FileViewerFixture {
  const id = `file-viewer-fixture-${++fixtureSeq}`
  host.dataset.fixtureId = id
  const mounted = mountConnected(host, {
    component: FileViewerModal,
    seed: pinia => {
      seedFileViewer(pinia, seed)
      registry.set(id, pinia)
      extra?.(fixtureStoresById(id))
    },
  })
  return {
    id,
    boundary: mounted.boundary,
    dispose: () => {
      registry.delete(id)
      mounted.dispose()
    },
  }
}

/**
 * The wrapper every connected file-viewer story renders: the isolated app
 * hosts the modal, and a fixture status line reports the boundary ledger and
 * the store state the plays assert on.
 */
export function renderFileViewerState(note: string, seed: FileViewerSeed = {}, extra?: (stores: ReturnType<typeof fixtureStoresById>) => void) {
  return defineComponent({
    setup() {
      const host = ref<HTMLElement>()
      const fixture = shallowRef<FileViewerFixture>()
      const error = ref('')
      onMounted(() => {
        try { fixture.value = mountFileViewer(host.value!, seed, extra) }
        catch (cause) { error.value = String(cause) }
      })
      onBeforeUnmount(() => fixture.value?.dispose())
      function status(): string {
        const f = fixture.value
        if (!f) return 'Fixture requests: 0. '
        const { viewer, projects } = fixtureStoresById(f.id)
        const comments = Object.values(projects.fileComments).reduce((n, list) => n + list.length, 0)
        return [
          `Fixture requests: ${f.boundary.requests.length}.`,
          `Violations: ${f.boundary.violations.length}.`,
          `Viewer open: ${viewer.isOpen}.`,
          `File comments: ${comments}.`,
          `Toasts: ${projects.toasts.length}.`,
        ].join(' ')
      }
      return () => h('div', { style: 'display: flex; flex-direction: column; gap: 4px; padding: 4px' }, [
        h('p', `Synthetic ${note}.`),
        h('div', { ref: host, style: 'min-height: 420px' }),
        h('p', { 'data-status': 'fixture' }, status()),
        error.value ? h('p', { role: 'alert' }, error.value) : null,
        (fixture.value?.boundary.violations ?? []).map(message => h('p', { role: 'alert' }, message)),
      ])
    },
  })
}

/** The fixture id of this story's mount, read from the host element. */
export function fixtureId(scope: unknown): string {
  const found = (scope as { querySelector?: (selector: string) => Element | null }).querySelector?.('[data-fixture-id]')
  const id = found?.getAttribute('data-fixture-id')
  if (!id) throw new Error('No file-viewer fixture host found')
  return id
}

/** The store handles a play function drives. */
export function playStores(scope: unknown) {
  return fixtureStoresById(fixtureId(scope))
}
