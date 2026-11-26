import { defineComponent, h } from 'vue'
import type { Component } from 'vue'
import type { RouteRecordRaw } from 'vue-router'
import type { MemoryGraphEntryCoverage } from '../stores/memoryMap'
import { useMemoryMapStore } from '../stores/memoryMap'
import { useProjectStore } from '../stores/projects'
import type { FixtureRoute } from './fixtureBoundary'
import { mountConnected, type ConnectedFixture } from './mountConnected'

/**
 * Synthetic fixtures for the Memory map workshop stories (slice B14). Names,
 * paths and payloads are `storybook-*` only: no real workspace, vault path,
 * token or user text. Every read is an exact declared route; everything else
 * the owners try fails closed at the preview boundary.
 */

export const mapWorkspace = 'storybook-alpha'
export const secondWorkspace = 'storybook-beta'

const response = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

/** A declare-able response that never resolves: the request is held until teardown. */
const held = (): Promise<never> => new Promise(() => {})

// ── Synthetic vault graph ────────────────────────────────────────────────────

type SyntheticNode = {
  id: string
  title: string
  type: string
  tags: string[]
  aliases: string[]
  description: string
  workspace: string
  degree: number
  mtime: number
  updated: string
  stale: boolean
  age_days: number | null
  threshold_days: number | null
  entry_coverage?: MemoryGraphEntryCoverage | null
}

type SyntheticGraph = { nodes: SyntheticNode[]; edges: Array<{ source: string; target: string }> }

const MTIME = 1_764_000_000

function makeNode(
  id: string,
  title: string,
  type: string,
  degree: number,
  extra: Partial<SyntheticNode> = {},
): SyntheticNode {
  return {
    id, title, type,
    tags: extra.tags ?? [],
    aliases: extra.aliases ?? [],
    description: extra.description ?? '',
    workspace: mapWorkspace,
    degree,
    mtime: extra.mtime ?? MTIME,
    updated: extra.updated ?? '2026-11-01',
    stale: extra.stale ?? false,
    age_days: extra.age_days === undefined ? 18 : extra.age_days,
    threshold_days: extra.threshold_days ?? null,
    entry_coverage: extra.entry_coverage ?? null,
  }
}

const factsCoverage: MemoryGraphEntryCoverage = {
  entries: 4, checked: 3, exempt: 0, unverified: 1, uncovered: 1, stale: 1,
  coverage_ratio: 0.75, fully_verified: false, more_stale_entries: 0,
  stale_entries: [{
    identity: 'storybook-fact-1', excerpt: 'Storybook fact recorded in the vault.',
    reason: 'no-stamp', detail: 'Never stamped.', age_days: null, last_verified: '', own_date: false,
  }],
}

/** The hub id, reused by the tile's neighbour stories. */
export const mapHubId = 'storybook-vault/Storybook hub.md'
export const mapNoteIds = {
  person: 'storybook-vault/Storybook person.md',
  project: 'storybook-vault/Storybook project.md',
  reference: 'storybook-vault/Storybook reference.md',
  meeting: 'storybook-vault/Storybook meeting notes.md',
  archive: 'storybook-vault/Storybook archive.md',
  readingList: 'storybook-vault/Storybook reading list.md',
  facts: 'storybook-vault/Storybook facts audit.md',
} as const

/**
 * The small synthetic vault: 11 notes, one stale note, one orphan, one undated
 * note and one note with facts nobody re-checked. Consistent by construction:
 * every node's degree is counted from the edge list.
 */
export function syntheticVault(): SyntheticGraph {
  const edges = [
    { source: mapHubId, target: mapNoteIds.person },
    { source: mapHubId, target: mapNoteIds.project },
    { source: mapHubId, target: mapNoteIds.reference },
    { source: mapHubId, target: 'storybook-vault/Storybook idea.md' },
    { source: mapHubId, target: 'storybook-vault/Storybook colleague.md' },
    { source: mapHubId, target: mapNoteIds.archive },
    { source: mapHubId, target: mapNoteIds.readingList },
    { source: mapHubId, target: mapNoteIds.facts },
    { source: mapNoteIds.reference, target: mapNoteIds.meeting },
    { source: mapNoteIds.person, target: mapNoteIds.project },
  ]
  const degree = new Map<string, number>()
  for (const edge of edges) {
    for (const id of [edge.source, edge.target]) degree.set(id, (degree.get(id) ?? 0) + 1)
  }
  const d = (id: string): number => degree.get(id) ?? 0
  return {
    nodes: [
      makeNode(mapHubId, 'Storybook hub', 'hub', d(mapHubId), { updated: '2026-10-20', age_days: 30 }),
      makeNode(mapNoteIds.person, 'Storybook person', 'person', d(mapNoteIds.person), { tags: ['self'], updated: '2026-11-10', age_days: 9 }),
      makeNode(mapNoteIds.project, 'Storybook project', 'project', d(mapNoteIds.project), { updated: '2026-05-01', age_days: 202, stale: true, threshold_days: 30 }),
      makeNode(mapNoteIds.reference, 'Storybook reference', 'reference', d(mapNoteIds.reference), { updated: '2026-09-15', age_days: 65 }),
      makeNode(mapNoteIds.meeting, 'Storybook meeting notes', 'log', d(mapNoteIds.meeting), { updated: '', age_days: null, mtime: 0 }),
      makeNode('storybook-vault/Storybook idea.md', 'Storybook idea', 'idea', 1),
      makeNode('storybook-vault/Storybook colleague.md', 'Storybook colleague', 'person', d('storybook-vault/Storybook colleague.md'), { tags: ['colleague'] }),
      makeNode('storybook-vault/Storybook orphan.md', 'Storybook orphan', 'note', 0, { updated: '2026-11-05', age_days: 14 }),
      makeNode(mapNoteIds.archive, 'Storybook archive', 'note', d(mapNoteIds.archive), { updated: '2026-10-28', age_days: 22 }),
      makeNode(mapNoteIds.readingList, 'Storybook reading list', 'note', d(mapNoteIds.readingList)),
      makeNode(mapNoteIds.facts, 'Storybook facts audit', 'note', d(mapNoteIds.facts), { entry_coverage: factsCoverage }),
    ],
    edges,
  }
}

/**
 * A vault dense enough to cross the label gate (more than 40 visible notes), so
 * the fit-scale view renders without on-canvas titles: the zoomed-out state.
 */
export function denseSyntheticVault(): SyntheticGraph {
  const ids: string[] = []
  for (let i = 1; i <= 44; i += 1) ids.push(`storybook-vault/Storybook note ${i}.md`)
  const edges: SyntheticGraph['edges'] = []
  for (let i = 1; i <= 24; i += 1) edges.push({ source: ids[0], target: ids[i] })
  for (let i = 1; i + 1 <= 43; i += 4) edges.push({ source: ids[i], target: ids[i + 1] })
  const degree = new Map<string, number>()
  for (const edge of edges) {
    for (const id of [edge.source, edge.target]) degree.set(id, (degree.get(id) ?? 0) + 1)
  }
  return {
    nodes: ids.map((id, index) => makeNode(id, `Storybook note ${index + 1}`, ['note', 'project', 'resource', 'person'][index % 4], degree.get(id) ?? 0, {
      stale: index % 11 === 0,
      threshold_days: index % 11 === 0 ? 30 : null,
      age_days: index % 11 === 0 ? 202 : 12,
    })),
    edges,
  }
}

// ── Synthetic categories ─────────────────────────────────────────────────────

type SyntheticEntityRow = {
  id: string; label: string; kind: 'entity' | 'note'; folder: string; description: string
  aliases: string[]; enabled: boolean; builtin: boolean
  core: boolean; hidden: boolean; note_count: number
}

const entityTypesRows: SyntheticEntityRow[] = [
  { id: 'person', label: 'Storybook people', kind: 'entity', folder: 'Storybook people',
    description: 'Synthetic record-of-a-thing category for the workshop.',
    aliases: ['storybook folk'], enabled: true, builtin: true, core: true, hidden: false, note_count: 3 },
  { id: 'project', label: 'Storybook projects', kind: 'entity', folder: 'Storybook projects',
    description: 'Synthetic project category for the workshop.',
    aliases: [], enabled: true, builtin: true, core: false, hidden: false, note_count: 2 },
  { id: 'storybook-custom', label: 'Storybook custom type', kind: 'note', folder: 'Storybook notes',
    description: 'A synthetic category the workshop can edit and save.',
    aliases: ['storybook type'], enabled: true, builtin: false, core: false, hidden: false, note_count: 1 },
  { id: 'storybook-agent-type', label: 'Storybook agent type', kind: 'note', folder: 'Storybook agent',
    description: 'A synthetic agent-written type the Categories page never lists.',
    aliases: [], enabled: true, builtin: false, core: false, hidden: true, note_count: 0 },
]

function entityTypesBody(): unknown {
  return { workspace: mapWorkspace, vault: 'storybook-vault', types: entityTypesRows }
}

/**
 * The PATCH echo: the categories list as the panel will save it (the custom
 * row switched off), carrying the server's own fields.
 */
function entityTypesAfterSave(): unknown {
  return {
    workspace: mapWorkspace,
    vault: 'storybook-vault',
    types: entityTypesRows.map((row) =>
      row.id === 'storybook-custom' ? { ...row, enabled: false } : row),
  }
}

// ── Declared routes ──────────────────────────────────────────────────────────

export type GraphMode = 'loaded' | 'held' | 'error'

function graphRoute(mode: GraphMode, body: SyntheticGraph): FixtureRoute {
  return {
    method: 'GET',
    path: `/api/vault/graph?workspace=${mapWorkspace}`,
    respond: () => mode === 'held'
      ? held()
      : mode === 'error' ? response({ error: 'Synthetic vault graph unavailable' }, 503)
        : response(body),
  }
}

/**
 * The always-loaded rail reads the workspace guide. The qualified AGENTS.md
 * answers first, so the bare-name fallbacks are never requested.
 */
const workspaceGuideRoute: FixtureRoute = {
  method: 'GET',
  path: `/api/workspace-file?exact=1&path=${encodeURIComponent(`${mapWorkspace}/AGENTS.md`)}`,
  respond: () => new Response('# Storybook guide\n\nSynthetic workspace guide for the workshop. It holds no real facts.\n', {
    status: 200,
    headers: { 'Content-Type': 'text/plain' },
  }),
}

/** The Unsure list the section reads from the vault, and the guide its rail reads. */
export const unsureListBody = [
  '# Unsure',
  '',
  '- [ ] memory: Storybook prefers short briefs — unsure because it was said once; 2026-11-01',
  '- [x] people: Storybook colleague works on the project — source: storybook; 2026-11-02',
  '',
].join('\n')

export function memoryPageRoutes(): FixtureRoute[] {
  return [
    workspaceGuideRoute,
    {
      method: 'GET',
      path: `/api/workspace-file?exact=1&path=${encodeURIComponent(`memory-vault/${mapWorkspace}/Workspace/Unsure.md`)}`,
      respond: () => new Response(unsureListBody, { status: 200, headers: { 'Content-Type': 'text/plain' } }),
    },
  ]
}

const noteFileBody = (title: string): string =>
  `# ${title}\n\nSynthetic note body recorded for the workshop stories. It holds no real facts.\n`

/** The selected note's own file, read by the tile's PinnedFilePanel. */
function noteFileRoute(id: string): FixtureRoute {
  return {
    method: 'GET',
    path: `/api/workspace-file?path=${encodeURIComponent(id)}`,
    respond: () => response(noteFileBody(id.split('/').pop() ?? id)),
  }
}

/** Markdown paths back the tile's wiki-link rendering; one shared read. */
const markdownPathsRoute: FixtureRoute = {
  method: 'GET',
  path: '/api/vault-markdown-paths',
  respond: () => response({ paths: [] }),
}

/** The exact declared reads and writes for one MemoryMapView story. */
export function mapRoutes(options: { graph: GraphMode; dense?: boolean; notes?: string[] }): FixtureRoute[] {
  const routes = [
    graphRoute(options.graph, (options.dense ? denseSyntheticVault : syntheticVault)()),
    ...memoryPageRoutes(),
  ]
  for (const id of options.notes ?? []) routes.push(noteFileRoute(id))
  routes.push(markdownPathsRoute)
  return routes
}

export type CategoriesMode = 'loaded' | 'held' | 'error' | 'empty'
export type CategoriesPatchMode = 'echo' | 'held' | 'error'

/** Declared reads and writes for one MemoryCategoriesPanel story. */
export function categoriesRoutes(options: {
  mode: CategoriesMode
  patch?: CategoriesPatchMode
  /** The stale story reads a second workspace and fails. */
  failingSecondWorkspace?: boolean
}): FixtureRoute[] {
  const respondAlpha = (): Response | Promise<never> => {
    if (options.mode === 'held') return held()
    if (options.mode === 'error') return response({ error: 'Synthetic categories unavailable' }, 503)
    return response(options.mode === 'empty' ? { workspace: mapWorkspace, vault: 'storybook-vault', types: [] } : entityTypesBody())
  }
  const routes: FixtureRoute[] = [{
    method: 'GET',
    path: `/api/memory/entity-types?workspace=${mapWorkspace}`,
    respond: respondAlpha,
  }]
  if (options.failingSecondWorkspace) {
    routes.push({
      method: 'GET',
      path: `/api/memory/entity-types?workspace=${secondWorkspace}`,
      respond: () => response({ error: 'Synthetic categories unavailable' }, 503),
    })
  }
  const patchMode: CategoriesPatchMode = options.patch ?? 'echo'
  routes.push({
    method: 'PATCH',
    path: `/api/memory/entity-types?workspace=${mapWorkspace}`,
    respond: () => patchMode === 'held'
      ? held()
      : patchMode === 'error' ? response({ error: 'Synthetic categories write refused' }, 503)
        : response(entityTypesAfterSave()),
  })
  return routes
}

// ── Routers ──────────────────────────────────────────────────────────────────

/** Inert Memory destinations so the tiles' RouterLinks resolve without mounting the app. */
export function memoryRouterRoutes(): RouteRecordRaw[] {
  return [{
    path: '/memory/:section?',
    component: defineComponent({ render: () => h('p', 'Inert Memory destination for workshop routing only.') }),
  }]
}

// ── Connected mounts ─────────────────────────────────────────────────────────

export type MapFixtureOptions = {
  section?: 'unsure'
  view?: 'graph' | 'list'
  selectedId?: string
}

/** Mounts the production MemoryMapView section owner into the preview's boundary. */
export function mountMapFixture(host: HTMLElement, component: Component, options: MapFixtureOptions = {}): ConnectedFixture {
  return mountConnected(host, {
    component,
    routerRoutes: memoryRouterRoutes(),
    seed: (pinia) => {
      useProjectStore(pinia).activeWorkspace = mapWorkspace
      const mm = useMemoryMapStore(pinia)
      if (options.section) mm.setSection(options.section)
      if (options.view) {
        mm.section = 'map'
        mm.view = options.view
        mm.mapView = options.view
      }
      if (options.selectedId) mm.selectedId = options.selectedId
    },
  })
}

export type CategoriesFixtureOptions = {
  initialWorkspace?: string
}

/** Mounts the production MemoryCategoriesPanel owner into the preview's boundary. */
export function mountCategoriesFixture(host: HTMLElement, component: Component, options: CategoriesFixtureOptions = {}): ConnectedFixture {
  return mountConnected(host, {
    component,
    seed: (pinia) => {
      useProjectStore(pinia).activeWorkspace = options.initialWorkspace ?? mapWorkspace
    },
  })
}
