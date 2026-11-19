import type { Pinia } from 'pinia'
import { defineComponent, h, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import type { Component } from 'vue'
import type { RouteRecordRaw } from 'vue-router'
import type {
  ProposalHistoryRow,
  ProposalPreview,
  ProposalRow,
  VaultReviewCandidate,
  VaultTrashedNote,
} from '../lib/types'
import { useProjectStore } from '../stores/projects'
import { useProposalsStore } from '../stores/proposals'
import { useVaultReviewStore } from '../stores/vaultReview'
import type { FixtureRoute } from './fixtureBoundary'
import { mountConnected, type ConnectedFixture } from './mountConnected'

/**
 * Synthetic fixtures for the Memory review workshop stories (slice B15). Names,
 * paths and payloads are `storybook-*` only: no real workspace, vault path,
 * token or user text. Every read is an exact declared route; anything else the
 * owners try fails closed at the preview boundary.
 */

export const reviewWorkspace = 'storybook-alpha'

const response = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

/** A declared response that never resolves: the request is held until teardown. */
const held = (): Promise<never> => new Promise(() => {})

/** A refused read, as the server answers a failing list. */
const refused = (): Response => response({ error: 'Synthetic review list unavailable' }, 503)

// ── Synthetic proposals ──────────────────────────────────────────────────────

export const proposalRows: ProposalRow[] = [
  {
    id: 'storybook-proposal-alpha',
    kind: 'memory',
    text: 'Storybook alpha keeps its synthetic fixtures tidy',
    source: '',
    workspace: reviewWorkspace,
    path: 'storybook-alpha/Workspace/Memory-Proposals.md',
    line: 3,
    region: 'memory',
  },
  {
    id: 'storybook-proposal-beta',
    kind: 'memory',
    text: 'Storybook beta prefers short synthetic reviews',
    source: 'storybook-chat',
    workspace: reviewWorkspace,
    path: 'storybook-alpha/Workspace/Memory-Proposals.md',
    line: 4,
    region: 'memory',
    decider: 'you',
    decide_reason: 'Needs a second look',
  },
]

function previewFor(row: ProposalRow): ProposalPreview {
  return {
    id: row.id,
    workspace: row.workspace,
    kind: row.kind,
    text: row.text,
    source: row.source,
    action: 'edit_region',
    operation: 'add',
    destination: 'ciao:memory',
    destination_path: `${reviewWorkspace}/AGENTS.md`,
    revision: 'storybook-rev-1',
    before: '- An older synthetic fact.',
    after: `- An older synthetic fact.\n- ${row.text}`,
    exact: true,
    truncated: false,
    can_accept: true,
    reason: '',
    separator: '\n',
  }
}

// ── Synthetic decision history ───────────────────────────────────────────────

export const historyRows: ProposalHistoryRow[] = [
  {
    id: 'storybook-history-alpha',
    ts: '2026-09-01T10:00:00+00:00',
    action: 'accepted',
    via: 'pwa',
    kind: 'memory',
    text: 'Storybook alpha kept a synthetic fact',
    source: '',
    workspace: reviewWorkspace,
    destination: 'ciao:memory',
    outcome: 'written',
    proposal_id: 'storybook-proposal-old-alpha',
  },
  {
    id: 'storybook-history-beta',
    ts: '2026-09-02T10:00:00+00:00',
    action: 'dismissed',
    via: 'pwa',
    kind: 'memory',
    text: 'Storybook beta dismissed a synthetic suggestion',
    source: '',
    workspace: reviewWorkspace,
    destination: 'ciao:memory',
    outcome: 'dismissed',
    proposal_id: 'storybook-proposal-old-beta',
    reason: 'Synthetic reason for the workshop',
  },
]

export type HistoryLoad = 'rows' | 'empty' | 'truncated' | 'held' | 'error'

function historyBody(load: HistoryLoad): unknown {
  if (load === 'empty') return { rows: [], total: 0, truncated: false, limit: 200, at_max: false }
  if (load === 'truncated') return { rows: historyRows, total: 260, truncated: true, limit: 200, at_max: false }
  return { rows: historyRows, total: historyRows.length, truncated: false, limit: 200, at_max: false }
}

export function historyRoute(load: HistoryLoad): FixtureRoute {
  return {
    method: 'GET',
    path: `/api/proposals/history?limit=200&workspace=${reviewWorkspace}`,
    respond: () => load === 'held' ? held() : load === 'error' ? refused() : response(historyBody(load)),
  }
}

export type QueueLoad = 'rows' | 'empty' | 'held' | 'error'

/**
 * The proposal panel's reads: the queue, a preview per row it prefetches, the
 * decision history it shows on mount, and the chat list its link pruning reads.
 */
export function proposalRoutes(options: { queue: QueueLoad, history?: HistoryLoad, previews?: 'loaded' | 'error' }): FixtureRoute[] {
  const queue: FixtureRoute = {
    method: 'GET',
    path: '/api/proposals',
    respond: () => options.queue === 'held' ? held()
      : options.queue === 'error' ? refused()
        : response({ rows: options.queue === 'empty' ? [] : proposalRows }),
  }
  const previews: FixtureRoute[] = proposalRows.map((row) => ({
    method: 'GET',
    path: `/api/proposals/${row.id}/preview`,
    respond: () => options.previews === 'error' ? refused() : response({ preview: previewFor(row) }),
  }))
  return [
    queue,
    ...previews,
    historyRoute(options.history ?? 'rows'),
    { method: 'GET', path: '/api/chats', respond: () => response([]) },
  ]
}

/** The history panel's own reads. */
export function historyRoutes(load: HistoryLoad): FixtureRoute[] {
  return [historyRoute(load)]
}

// ── Synthetic vault review ───────────────────────────────────────────────────

export const vaultCandidates: VaultReviewCandidate[] = [
  {
    candidate_id: 'storybook-candidate-alpha',
    workspace: reviewWorkspace,
    path: 'storybook-vault/Storybook alpha.md',
    content_hash: 'storybook-hash-alpha',
    signals: ['unlinked', 'weak_provenance'],
    priority: 1,
    evidence: {
      backlinks: [],
      outbound_links: [],
      bridge: false,
      duplicate_group: [],
      last_update: '',
      type: 'note',
      age_days: null,
    },
    status: 'candidate',
    disposition: '',
    deferred_until: '',
    completable: false,
    completion_moves_folder: false,
  },
  {
    candidate_id: 'storybook-candidate-beta',
    workspace: reviewWorkspace,
    path: 'storybook-vault/Storybook beta.md',
    content_hash: 'storybook-hash-beta',
    signals: ['possible_duplicate'],
    priority: 2,
    evidence: {
      backlinks: [],
      outbound_links: [],
      bridge: false,
      duplicate_group: ['storybook-vault/Storybook alpha.md'],
      last_update: '',
      type: 'note',
      age_days: null,
    },
    status: 'candidate',
    disposition: '',
    deferred_until: '',
    completable: false,
    completion_moves_folder: false,
  },
]

export const vaultTrashed: VaultTrashedNote[] = [
  {
    candidate_id: 'storybook-trashed-alpha',
    workspace: reviewWorkspace,
    original_path: 'storybook-vault/Storybook retired.md',
    content_hash: 'storybook-hash-retired',
    trashed_at: '2026-09-03T10:00:00+00:00',
  },
]

export type VaultLoad = 'loaded' | 'empty' | 'held' | 'error'

/** The retirement queue's one read, which carries both the candidates and the trash. */
export function vaultRoutes(load: VaultLoad): FixtureRoute[] {
  return [{
    method: 'GET',
    path: `/api/vault/review?workspace=${reviewWorkspace}&include=trashed,cleared`,
    respond: () => load === 'held' ? held()
      : load === 'error' ? refused()
        : response({
          candidates: load === 'empty' ? [] : vaultCandidates,
          trashed: load === 'empty' ? [] : vaultTrashed,
          cleared: [],
        }),
  }]
}

/** Inert destinations so the owners' router links resolve without mounting the app. */
export function reviewRouterRoutes(): RouteRecordRaw[] {
  return [{
    path: '/memory/:section?',
    component: defineComponent({ render: () => h('p', 'Inert Memory destination for workshop routing only.') }),
  }]
}

// ── Connected mounts ─────────────────────────────────────────────────────────

export type ReviewMountOptions = {
  /** Props of the production owner, passed through a one-line wrapper. */
  props?: Record<string, unknown>
  /** Store state a snapshot starts from, before the owner's own mount read. */
  seed?: (pinia: Pinia) => void
  /** Runs once the owner is mounted; used to start a refresh of a seeded snapshot. */
  afterMount?: (fixture: ConnectedFixture) => void
}

/** Mounts one production review owner into the preview's fenced boundary. */
export function mountReview(host: HTMLElement, component: Component, options: ReviewMountOptions = {}): ConnectedFixture {
  const owner = defineComponent({ render: () => h(component, options.props ?? {}) })
  const fixture = mountConnected(host, {
    component: owner,
    routerRoutes: reviewRouterRoutes(),
    seed: (pinia) => {
      useProjectStore(pinia).activeWorkspace = reviewWorkspace
      options.seed?.(pinia)
    },
  })
  options.afterMount?.(fixture)
  return fixture
}

/** A seeded proposal snapshot, as a refresh that failed would leave it. */
export function seedProposalSnapshot(pinia: Pinia): void {
  const store = useProposalsStore(pinia)
  store.rows = proposalRows
  store.loaded = true
}

/** A seeded retirement snapshot for the workspace the panel reads. */
export function seedVaultSnapshot(pinia: Pinia): void {
  const store = useVaultReviewStore(pinia)
  store.candidates = vaultCandidates
  store.trashed = vaultTrashed
  store.loadedWorkspace = reviewWorkspace
}

/** Starts the refresh a seeded vault snapshot needs: the panel's own mount read is skipped once loaded. */
export function refreshVaultSnapshot(fixture: ConnectedFixture): void {
  void useVaultReviewStore(fixture.pinia).fetch(reviewWorkspace, { force: true })
}

/** Starts the refresh a seeded history snapshot needs: the mount read is skipped once loaded. */
export function refreshHistorySnapshot(fixture: ConnectedFixture): void {
  void useProposalsStore(fixture.pinia).fetchHistory({ force: true, workspace: reviewWorkspace })
}

/**
 * The workshop frame around one connected owner: a description, the mounted
 * owner, the fixture's request count and any recorded violation.
 */
export function reviewStage(description: string, mount: (host: HTMLElement) => ConnectedFixture, narrow = false) {
  return defineComponent({
    setup() {
      const host = ref<HTMLElement>()
      const fixture = shallowRef<ConnectedFixture>()
      const error = ref('')
      onMounted(() => {
        try { fixture.value = mount(host.value!) }
        catch (cause) { error.value = String(cause) }
      })
      onBeforeUnmount(() => fixture.value?.dispose())
      return () => h('div', { style: narrow ? 'max-width: 390px' : undefined }, [
        h('p', description),
        h('div', { ref: host }),
        h('p', { role: 'status' }, `Fixture requests: ${fixture.value?.boundary.requests.length ?? 0}. Violations: ${fixture.value?.boundary.violations.length ?? 0}.`),
        error.value ? h('p', { role: 'alert' }, error.value) : null,
        ...(fixture.value?.boundary.violations ?? []).map((message) => h('p', { role: 'alert' }, message)),
      ])
    },
  })
}
