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
  { name: 'alpha', vault_root: '/synthetic/alpha', default_provider: 'claude' },
  { name: 'beta', vault_root: '/synthetic/beta', default_provider: 'claude' },
  { name: 'gamma', vault_root: '/synthetic/gamma', default_provider: 'claude' },
]

export const PROJECTS = WORKSPACES.flatMap((workspace, wi) => ([
  {
    project_id: `${workspace.name}-general`,
    name: 'General',
    workspace: workspace.name,
    context: `Synthetic ${workspace.name} general project`,
    order: 0,
    repo: wi === 0 ? { path: '/synthetic/repos/alpha-app', branch: 'main', dirty: true, missing: false, instructions: { name: 'AGENTS.md', chars: 3200 } } : null,
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
    // Server-owned chat pin wire shape (#1119). Empty by default; the pin-sync
    // journey opts a chat into a real pin below.
    pinned_file_path: '',
    dismissed_pin_paths: [],
    pin_revision: 0,
    ...extra,
  }
}

/**
 * Where an archived chat's transcript lives, in the shape the real server
 * writes it: `<logs_root>/Chats/<chat-id>/<provider>/<stem>.md`, relative to
 * the workspace root.
 *
 * The shape is not decoration. The file viewer reads the chat id back out of
 * the path to decide whether the file is a transcript it can continue
 * (FileViewerModal.vue), so a synthetic path that does not look like this
 * can never reach the "Continue this chat" button — the browser suite could
 * not exercise it at all (#1710). The real chat ids start with `chat-`; the
 * fixture's are `<workspace>-chat-<n>`, so the folder carries that prefix.
 * The folder is therefore not the row's `chat_id`: a spec must not read the
 * button's target as the archived chat it listed.
 */
function transcriptPath(chatId, stem = '2026-01-01T09-00-00') {
  return `memory-vault/Logs/Chats/chat-${chatId}/claude/${stem}-${chatId}.md`
}

function passHelper(sourceChatId, sourceTitle, state) {
  return {
    kind: 'memory_pass',
    source_chat_id: sourceChatId,
    // The transcript this pass is distilling: the same file the archived
    // conversation carries, so the two rows agree on what is being read.
    archive_path: transcriptPath(sourceChatId),
    doc_path: '',
    source_title: sourceTitle,
    source_project: '',
    state,
    archive_policy: 'when_clean',
  }
}

export const CHATS = WORKSPACES.flatMap((workspace) => ([
  chat(workspace.name, 1, `${workspace.name}-general`, `${workspace.name} first conversation`,
    // One invented chat pin in a workspace no spec opens, so the two-context
    // pin-sync journey has a real server pin to sync without disturbing the
    // alpha chats every other spec measures.
    workspace.name === 'beta'
      ? { pinned_file_path: '/synthetic/beta/pinned-report.md', dismissed_pin_paths: [], pin_revision: 1 }
      : workspace.name === 'alpha'
        ? {
          repo_location: { path: '/synthetic/repos/alpha-app-worktrees/feature-login-a-very-long-unbreakable-worktree-directory-name', branch: 'feature/login', is_worktree: true, exists: true },
        }
        : {}),
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
        archive_path: transcriptPath('alpha-chat-3'),
        postprocess: { steps: { memory_pass: { status: 'ok', extra: { chat_id: 'alpha-chat-2' } } } },
      }),
      // A running memory pass and the archived conversation it is distilling.
      // This is the surface the narrow-viewport journey has to measure: the
      // memory-insight row is the only thing on Home for them, and the pass
      // chat itself must not appear as a tier row or a sidebar row. Distinct
      // activity times, newest last, so the section's order is assertable.
      chat(workspace.name, 4, `${workspace.name}-general`, `${workspace.name} conversation with a running memory pass`, {
        archived: true,
        archive_path: transcriptPath('alpha-chat-4'),
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
        archive_path: transcriptPath('alpha-chat-6'),
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

/**
 * Two housekeeping actions for the "Needs you" list, which only a spec that opts
 * in through `POST /__fixture__/needs-you` ever sees. Two rather than one so
 * the gap below the last row is measured against a real end of the section
 * instead of a single row that also happens to be the last one.
 *
 * Each carries a `run_label`, so its lead control is a button; neither is
 * blocking or has a workspace, so both are drawn in the order given.
 */
const needsYouAction = (id, title, detail) => ({
  id,
  kind: 'setup-check',
  severity: 20,
  title,
  detail,
  glyph: '',
  workspace: '',
  run_label: 'Run check',
  chat_label: '',
  chat_prompt: '',
  view_label: '',
  view_route: '',
  blocking: false,
})

export const NEEDS_YOU_ACTIONS = [
  needsYouAction('review-legacy-rows', 'Review the rows your old notes left behind', 'Rows retired into a folder no surface reads any more.'),
  needsYouAction('confirm-backup-schedule', 'Confirm the memory backup schedule', 'The old schedule points at a branch this install does not push to.'),
]

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

// Two more healthy routines in alpha, so the open automation's title rail has
// neighbours: the sidebar (and the rail) order is Daily, Weekly, Inbox sweep.
SCHEDULES.push(
  {
    ...SCHEDULES[0],
    schedule_id: 'alpha-weekly-review',
    title: 'Weekly review',
    description: 'Look back over the week.',
    prompt: 'Review what moved this week.',
    daily_time_utc: '09:00',
    frequency: 'weekly',
    days_of_week: ['mon'],
    next_run: '2026-09-28T09:00:00Z',
    last_triggered_on: '2026-09-21',
    last_expected_run: '2026-09-21T09:00:00Z',
  },
  {
    ...SCHEDULES[0],
    schedule_id: 'alpha-inbox-sweep',
    title: 'Inbox sweep',
    description: 'Triage new mail.',
    prompt: 'Sweep the inbox.',
    frequency: 'interval',
    interval_minutes: 30,
    next_run: '2026-09-24T10:30:00Z',
  },
)

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
 * One builtin, one core (locked on), one hidden, one disabled and one custom,
 * because those are the things the Categories panel renders differently and a fixture holding only
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
    enabled: true,
    builtin: true,
    core: false,
    hidden: false,
    note_count: 1,
  },
  {
    id: 'project',
    label: 'Project',
    kind: 'entity',
    folder: 'Projects',
    description: 'Something with an outcome and a finish line.',
    aliases: [],
    enabled: true,
    builtin: true,
    core: true,
    hidden: false,
    note_count: 1,
  },
  {
    id: 'idea',
    label: 'Idea',
    kind: 'entity',
    folder: 'Ideas',
    description: 'A position worth testing, not yet a decision.',
    aliases: [],
    enabled: false,
    builtin: true,
    core: false,
    hidden: false,
    note_count: 1,
  },
  {
    id: 'customer',
    label: 'Customer',
    kind: 'entity',
    folder: 'Customers',
    description: 'A person or company the user does business with.',
    aliases: ['client'],
    enabled: true,
    builtin: false,
    core: false,
    hidden: false,
    note_count: 0,
  },
  {
    id: 'workspace',
    label: 'Workspace',
    kind: 'note',
    folder: 'Workspace',
    description: "The agent's own working surface.",
    aliases: [],
    enabled: true,
    builtin: true,
    core: true,
    hidden: true,
    note_count: 4,
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
    restarting: false,
    // Authoritative server pin map, mirroring the pin on beta-chat-1 above.
    chat_pins: {
      'beta-chat-1': { path: '/synthetic/beta/pinned-report.md', dismissed_paths: [], revision: 1 },
    },
    // The needs-you action map, complete the same way. Empty in the base
    // frame; the fixture server fills it from the session's actions.
    chat_actions: {},
  }
}

export function usageStats(range) {
  const days = [7, 30, 90, 365].includes(range) ? range : 30
  const now = new Date()
  const daily = []
  for (let i = 363; i >= 0; i--) {
    const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - i))
    daily.push({ date: d.toISOString().slice(0, 10), count: (i * 37) % 23 })
  }
  return {
    range_days: days, generated_at: now.toISOString(),
    today: { chats_opened: 14, messages_sent: 212, chats_archived: 5, projects_worked_on: 4, tasks_completed: 3, avg_chats_opened_30d: 10.4, avg_messages_sent_30d: 151, avg_chats_archived_30d: 3.8, avg_projects_worked_on_30d: 2.6, avg_tasks_completed_30d: 1.4 },
    totals: { chats_opened: 248, messages_sent: 4517, chats_archived: 171, tasks_completed: 37, projects_worked_on: 16, automation_runs: 180, tokens: 71500000 },
    previous: { chats_opened: 210, messages_sent: 3702, chats_archived: 157, tasks_completed: 31, projects_worked_on: 13, automation_runs: 173, tokens: null },
    daily_messages: daily,
    projects: [
      { project_id: 'alpha-general', name: 'General', messages: 412 },
      { project_id: 'alpha-notes', name: 'alpha-notes-a-very-long-unbreakable-project-identifier', messages: 260 },
      { project_id: 'beta-general', name: 'General', messages: 198 },
    ],
    hours: [2, 1, 0, 0, 0, 0, 1, 4, 9, 14, 16, 12, 7, 11, 15, 13, 10, 8, 6, 9, 14, 17, 12, 6],
    models: [{ model: 'claude-opus-5-5', turns: 680 }, { model: 'claude-sonnet-5-5', turns: 220 }, { model: 'openai/gpt-5.6-luna', turns: 100 }],
  }
}
