/**
 * The synthetic workspace the browser suite runs against.
 *
 * Everything here is invented. The suite never reads a real vault, never holds
 * a model credential and never touches launchd — the whole "backend" is the
 * Node process in `server.mjs`, so a run cannot reach the user's data even by
 * accident.
 *
 * Three workspaces, in this order, because the workspace-shortcut journey
 * needs a stable 1/2/3 mapping to assert against.
 */
export const WORKSPACES = [
  { name: 'alpha', vault_root: '/synthetic/alpha', default_provider: 'claude', gws_profile: 'alpha' },
  { name: 'beta', vault_root: '/synthetic/beta', default_provider: 'claude', gws_profile: 'beta' },
  { name: 'gamma', vault_root: '/synthetic/gamma', default_provider: 'claude', gws_profile: 'gamma' },
]

export const PROJECTS = WORKSPACES.flatMap((workspace, wi) => ([
  {
    project_id: `${workspace.name}-general`,
    name: 'General',
    workspace: workspace.name,
    context: `Synthetic ${workspace.name} general project`,
    order: 0,
    color: ['#ff4d6d', '#6a47b8', '#37b39c'][wi],
  },
  {
    project_id: `${workspace.name}-notes`,
    // Long unbreakable token on purpose: the narrow-viewport journey asserts
    // that a flex child with an unbreakable string does not widen the document
    // past the viewport (the `min-width: 0` trap in web/README.md).
    name: `${workspace.name}-notes-a-very-long-unbreakable-project-identifier`,
    workspace: workspace.name,
    context: 'Synthetic notes project',
    order: 1,
    color: '#6a47b8',
  },
]))

function chat(workspace, n, projectId, title) {
  return {
    chat_id: `${workspace}-chat-${n}`,
    project_id: projectId,
    title,
    model: 'synthetic-model',
    provider: 'claude',
    mode: 'auto',
    thinking_level: '',
    session_id: '',
    created_at: '2026-01-01T09:00:00Z',
    archived: false,
    last_activity_at: '2026-01-01T09:05:00Z',
    last_read_at: '2026-01-01T09:05:00Z',
    last_snippet: '',
    // "pending" renders a shimmer that never settles, which would make every
    // screenshot and every stability check a coin flip.
    title_status: 'ready',
    pending_question: '',
    pending_permission: '',
    forked_from_chat_id: '',
    forked_from_turn_index: null,
    fork_root_chat_id: '',
    fork_index: 0,
    fork_base_title: '',
    schedule_id: '',
    schedule_title: '',
    helper: {},
    retry: null,
    local: true,
  }
}

export const CHATS = WORKSPACES.flatMap((workspace) => ([
  chat(workspace.name, 1, `${workspace.name}-general`, `${workspace.name} first conversation`),
  chat(workspace.name, 2, `${workspace.name}-notes`, `${workspace.name} second conversation`),
]))

/** One pending row, so the review queue renders something to tab onto. */
export const PROPOSALS = [
  {
    id: 'proposal-1',
    kind: 'memory',
    text: 'Synthetic proposal for the browser suite',
    source: 'alpha-chat-1',
    workspace: 'alpha',
    path: 'memory-vault/alpha/MEMORY.md',
    line: 1,
    region: 'memory',
    leak_warning: false,
  },
]

/**
 * The `/ws/events` snapshot. `activeStreams` is mutated by the fixture's
 * control endpoint so the reconnect journey can prove the client re-applied a
 * *new* snapshot rather than merely re-opening a socket.
 */
export function snapshotFrame(activeStreams) {
  return {
    type: 'snapshot',
    active_streams: activeStreams.map((chat_id) => ({ chat_id, project_id: '' })),
    background_agents: {},
    background_runs: {},
    postprocessing: [],
    restarting: false,
  }
}
