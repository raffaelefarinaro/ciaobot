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

export const SCHEDULES = [
  {
    schedule_id: 'alpha-daily-brief',
    title: 'Daily project briefing',
    description: 'Summarize movement, decisions, and anything that needs attention.',
    prompt: 'Review the latest project activity and write a concise briefing.',
    daily_time_utc: '08:00',
    chat_id: 0,
    created_at: '2026-09-01T08:00:00Z',
    timezone_name: 'UTC',
    last_triggered_on: '2026-09-24',
    last_status: 'ok',
    days_of_week: null,
    thread_id: null,
    context_label: 'General',
    context_available: true,
    frequency: 'daily',
    interval_minutes: 0,
    day_of_month: null,
    run_at_date: null,
    web_chat_id: null,
    web_project_id: 'alpha-general',
    workspace: 'alpha',
    model: 'synthetic-model',
    provider: 'claude',
    next_run: '2026-09-25T08:00:00Z',
    last_expected_run: '2026-09-24T08:00:00Z',
    missed: false,
    enabled: true,
    archive_policy: 'auto',
  },
]

export const MEMORY_NODES = [
  {
    id: 'memory-vault/alpha/Projects/Launch.md',
    title: 'Launch decision',
    type: 'project',
    tags: ['launch', 'decision'],
    aliases: [],
    description: 'The current launch outcome, constraints, and accepted trade-offs.',
    workspace: 'alpha',
    degree: 2,
    mtime: 1780000000,
    updated: '2026-09-20',
    stale: false,
    ageDays: 4,
  },
  {
    id: 'memory-vault/alpha/People/Team.md',
    title: 'Team working agreements',
    type: 'person-colleague',
    tags: ['team'],
    aliases: [],
    description: 'How the team reviews, decides, and hands work across time zones.',
    workspace: 'alpha',
    degree: 1,
    mtime: 1779000000,
    updated: '2026-08-14',
    stale: true,
    ageDays: 41,
  },
  {
    id: 'memory-vault/alpha/Ideas/Positioning.md',
    title: 'Product positioning',
    type: 'idea',
    tags: ['positioning', 'launch'],
    aliases: [],
    description: 'The product promise and the evidence that supports it.',
    workspace: 'alpha',
    degree: 1,
    mtime: 1780100000,
    updated: '2026-09-22',
    stale: false,
    ageDays: 2,
  },
]

export const MEMORY_EDGES = [
  { source: MEMORY_NODES[0].id, target: MEMORY_NODES[1].id },
  { source: MEMORY_NODES[0].id, target: MEMORY_NODES[2].id },
]

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
