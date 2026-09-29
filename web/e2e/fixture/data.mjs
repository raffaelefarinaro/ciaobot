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
  // The app-owned project a memory pass runs in. The sidebar hides it, and the
  // pass chat below is filtered out of the Home tiers for the same reason, so
  // only the memory-insight row it produces is on screen.
  {
    project_id: `${workspace.name}-memory`,
    name: 'Memory',
    workspace: workspace.name,
    context: 'Synthetic memory project',
    order: 2,
    kind: 'memory',
  },
]))

function chat(workspace, n, projectId, title, extra = {}) {
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
    ...extra,
  }
}

function passHelper(sourceChatId, sourceTitle, state) {
  return {
    kind: 'memory_pass',
    source_chat_id: sourceChatId,
    archive_path: `/synthetic/${sourceChatId.split('-')[0]}/archive/${sourceChatId}.jsonl`,
    doc_path: '',
    source_title: sourceTitle,
    source_project: '',
    state,
    archive_policy: 'when_clean',
  }
}

export const CHATS = WORKSPACES.flatMap((workspace) => ([
  chat(workspace.name, 1, `${workspace.name}-general`, `${workspace.name} first conversation`),
  chat(workspace.name, 2, `${workspace.name}-notes`, `${workspace.name} second conversation`),
  // Archived, and only in the first workspace: the sidebar hides it, so every
  // other spec sees the same rows it saw before, while a deep link to its id
  // has something to open. `archive_path` is what a real archive carries (the
  // chat is viewable with or without it), and the settled `memory_pass` step
  // puts the "Open memory pass" link from #618 on screen, unreachable for the
  // same reason.
  ...(workspace === WORKSPACES[0]
    ? [
      chat(workspace.name, 3, `${workspace.name}-general`, `${workspace.name} archived conversation`, {
        archived: true,
        archive_path: '/synthetic/alpha/archive/alpha-chat-3.jsonl',
        postprocess: { steps: { memory_pass: { status: 'ok', extra: { chat_id: 'alpha-chat-2' } } } },
      }),
      // A running memory pass and the archived conversation it is distilling.
      // This is the surface the narrow-viewport journey has to measure: the
      // memory-insight row is the only thing on Home for them, and the pass
      // chat itself must not appear as a tier row or a sidebar row. Distinct
      // activity times, newest last, so the section's order is assertable.
      chat(workspace.name, 4, `${workspace.name}-general`, `${workspace.name} conversation with a running memory pass`, {
        archived: true,
        archive_path: '/synthetic/alpha/archive/alpha-chat-4.jsonl',
        last_activity_at: '2026-01-01T11:00:00Z',
        postprocess: { steps: { memory_pass: { status: 'running', extra: { chat_id: 'alpha-chat-5' } } } },
      }),
      chat(workspace.name, 5, `${workspace.name}-memory`, 'Memory pass · conversation with a running memory pass', {
        helper: passHelper('alpha-chat-4', 'alpha conversation with a running memory pass', 'running'),
        last_activity_at: '2026-01-01T11:05:00Z',
        last_snippet: 'Updated two project notes and queued one fact for review.',
      }),
      // A pass blocked on its owner, so the row's question and "needs you"
      // state are on screen in the same journey.
      chat(workspace.name, 6, `${workspace.name}-general`, `${workspace.name} conversation waiting on the pass`, {
        archived: true,
        archive_path: '/synthetic/alpha/archive/alpha-chat-6.jsonl',
        last_activity_at: '2026-01-01T10:00:00Z',
      }),
      chat(workspace.name, 7, `${workspace.name}-memory`, 'Memory pass · conversation waiting on the pass', {
        helper: passHelper('alpha-chat-6', 'alpha conversation waiting on the pass', 'running'),
        last_activity_at: '2026-01-01T10:05:00Z',
        pending_question: JSON.stringify({
          questions: [{ question: 'Which project does the ITF rollout belong to?', header: 'Project' }],
        }),
        last_snippet: '',
      }),
    ]
    : []),
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

/**
 * The vault's category list, in the shape `/api/memory/entity-types` serves it.
 *
 * One builtin, one disabled one and one custom, because those are the three
 * things the Categories panel renders differently and a fixture holding only
 * shipped-and-enabled rows would leave two of them unmeasured.
 */
export const MEMORY_CATEGORIES = [
  {
    id: 'person',
    label: 'Person',
    kind: 'entity',
    folder: 'People',
    description: 'A human the user knows or works with.',
    aliases: [],
    stale_after_days: 90,
    enabled: true,
    builtin: true,
    note_count: 1,
  },
  {
    id: 'project',
    label: 'Project',
    kind: 'entity',
    folder: 'Projects',
    description: 'Something with an outcome and a finish line.',
    aliases: [],
    stale_after_days: 30,
    enabled: true,
    builtin: true,
    note_count: 1,
  },
  {
    id: 'idea',
    label: 'Idea',
    kind: 'entity',
    folder: 'Ideas',
    description: 'A position worth testing, not yet a decision.',
    aliases: [],
    stale_after_days: 0,
    enabled: false,
    builtin: true,
    note_count: 1,
  },
  {
    id: 'customer',
    label: 'Customer',
    kind: 'entity',
    folder: 'Customers',
    description: 'A person or company the user does business with.',
    aliases: ['client'],
    stale_after_days: 0,
    enabled: true,
    builtin: false,
    note_count: 0,
  },
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
    restarting: false,
  }
}
