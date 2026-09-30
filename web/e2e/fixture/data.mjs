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

// ── The managed note-verification pipeline, as the surfaces receive it ──────
//
// Four notes, one per state the review queue and the memory map have to tell
// apart. They are the states a browser cannot be asked about in jsdom: a link
// that has to take focus, a card whose copy changes shape, and a set of
// controls whose touch height only a real layout engine reports.

/** The check state the engine stores for one note's current revision. */
const check = (over = {}) => ({
  outcome: 'still_valid',
  checked_at: '2026-09-28',
  retry_after: '2026-10-28',
  coverage: 'complete',
  reason: 'the March release notes still name her',
  citations: 3,
  receipt_id: 'mrcpt_fixture_1',
  settled: true,
  pending: false,
  proposal_id: '',
  conflicted: false,
  ...over,
})

export const VERIFICATION_REVIEW = {
  candidates: [
    {
      // The row this child is about: a note the pass is holding for a person.
      // The queue links the proposal instead of offering Still true / Retire on
      // the same revision, and `unlinked` keeps Retire — a second, independent
      // finding about the note.
      candidate_id: 'pend0000000000000000000a1',
      workspace: 'alpha',
      path: 'memory-vault/alpha/People/Team.md',
      content_hash: 'hash-pending',
      signals: ['unlinked', 'unverified'],
      priority: 0,
      evidence: {
        backlinks: [],
        outbound_links: [],
        bridge: false,
        duplicate_group: [],
        last_update: '2024-01-05',
        type: 'person',
        age_days: 999,
        unverified: { age_days: 999, threshold_days: 90, last_verified: '2024-01-05', source: 'frontmatter' },
        verification: check({
          outcome: 'retire',
          reason: 'the team page was replaced by the org chart',
          citations: 2,
          receipt_id: '',
          pending: true,
          proposal_id: 'proposal-verify-1',
        }),
        superseded: null,
        excerpt: 'Working agreements for the release team.',
      },
      status: 'candidate',
      disposition: '',
      deferred_until: '',
      completable: false,
      completion_moves_folder: false,
      pending_verification: {
        proposal_id: 'proposal-verify-1',
        note_edit_id: 'fixture0000000000a1',
        outcome: 'retire',
        checked_at: '2026-09-28',
        retry_after: '2026-10-28',
        coverage: 'complete',
        reason: 'the team page was replaced by the org chart',
        citations: 2,
      },
      retirement_offered: true,
    },
    {
      // Held for a proposal and flagged for NOTHING else: the queue has no
      // second decision of its own to offer at all.
      candidate_id: 'pend0000000000000000000b2',
      workspace: 'alpha',
      path: 'memory-vault/alpha/Projects/Atlas.md',
      content_hash: 'hash-sole',
      signals: ['unverified'],
      priority: 0,
      evidence: {
        backlinks: [], outbound_links: [], bridge: false, duplicate_group: [],
        last_update: '2024-01-05', type: 'project', age_days: 999,
        unverified: { age_days: 999, threshold_days: 30, last_verified: '2024-01-05', source: 'frontmatter' },
        verification: check({
          outcome: 'update', coverage: 'partial', reason: 'the tracker moved to a new system',
          citations: 1, receipt_id: '', pending: true, proposal_id: 'proposal-verify-2',
        }),
        superseded: null,
        excerpt: 'Atlas ships on the last Friday of the month.',
      },
      status: 'candidate', disposition: '', deferred_until: '',
      completable: false, completion_moves_folder: false,
      pending_verification: {
        proposal_id: 'proposal-verify-2',
        note_edit_id: 'fixture0000000000b2',
        outcome: 'update', checked_at: '2026-09-28', retry_after: '2026-10-28',
        coverage: 'partial', reason: 'the tracker moved to a new system', citations: 1,
      },
      retirement_offered: false,
    },
    {
      // A proposal pinned to a revision the note has left: it can no longer be
      // applied, so the queue hands the decision back and says why.
      candidate_id: 'pend0000000000000000000c3',
      workspace: 'alpha',
      path: 'memory-vault/alpha/Projects/Borealis.md',
      content_hash: 'hash-conflicted',
      signals: ['unverified'],
      priority: 0,
      evidence: {
        backlinks: [], outbound_links: [], bridge: false, duplicate_group: [],
        last_update: '2024-01-05', type: 'project', age_days: 999,
        unverified: { age_days: 999, threshold_days: 30, last_verified: '2024-01-05', source: 'frontmatter' },
        verification: check({
          outcome: 'retire', settled: false, pending: false, conflicted: true,
          proposal_id: 'proposal-verify-3', reason: 'folded into Atlas', citations: 1, receipt_id: '',
        }),
        superseded: null,
        excerpt: 'Borealis is superseded by Atlas.',
      },
      status: 'candidate', disposition: '', deferred_until: '',
      completable: false, completion_moves_folder: false,
      pending_verification: null,
      retirement_offered: true,
    },
    {
      // A note that is CURRENT and still holding a fact nobody has re-checked.
      // The whole-note signal did not fire — the file's own `updated:` is a day
      // old — which is the only reason the entry-level block has anything to
      // add, and the only reason this row exists at all. It is a browser case
      // because the block adds a quoted line, a context line and two links into
      // a row whose path is an unbreakable token.
      candidate_id: 'pend0000000000000000000e5',
      workspace: 'alpha',
      path: 'memory-vault/alpha/People/Nadia.md',
      content_hash: 'hash-mixed',
      signals: ['unverified_entries'],
      priority: 0,
      evidence: {
        backlinks: [], outbound_links: [], bridge: false, duplicate_group: [],
        last_update: '2026-09-29', type: 'person', age_days: 1,
        unverified: null,
        verification: null,
        entry_verification: {
          entries: 3,
          checked: 3,
          exempt: 0,
          unverified: 1,
          uncovered: 1,
          stale: 1,
          coverage_ratio: 0.62,
          fully_verified: false,
          stale_entries: [
            {
              identity: 'entry-identity-aged-000000000000000000000000000000000000000000000000000000',
              line_number: 9,
              section: 'Nadia',
              excerpt: '- Reports to the CTO [verified: 2019-05-01]',
              context: ['- Based in Lisbon [verified: 2026-09-28]'],
              reason: 'aged',
              detail: 'unverified for 2698d against a 90d horizon',
              age_days: 2698,
              last_verified: '2019-05-01',
              own_date: true,
              supported: true,
            },
            {
              // Never checked, and carrying the note's date rather than one of
              // its own — the second kind of staleness, and the one a whole-note
              // age cannot express. The prose the note also carries is reported
              // by the `uncovered` count instead: a paragraph is not an entry,
              // and the server never puts one in this list.
              identity: 'entry-identity-uncov-000000000000000000000000000000000000000000000000000000',
              line_number: 11,
              section: 'Nadia',
              excerpt: '- Leads the platform team [verified: 2026-09-29]',
              context: ['- Based in Lisbon [verified: 2026-09-28]'],
              reason: 'no-stamp',
              detail: "nobody has recorded a [verified:] check on this entry; it is unverified for 1d on the note's own last-verified date",
              age_days: 1,
              last_verified: '2026-09-29',
              own_date: false,
              supported: true,
            },
          ],
          more_stale_entries: 0,
          proposals: [
            {
              identity: 'entry-identity-aged-000000000000000000000000000000000000000000000000000000',
              proposal_id: 'proposal-verify-4',
              operation: 'replace_entry',
              outcome: 'update',
              checked_at: '2026-09-28',
              retry_after: '2026-10-28',
              coverage: 'complete',
              reason: 'the reorg moved her under the COO',
              citations: 2,
              receipt_id: '',
              conflicted: false,
            },
            {
              identity: 'entry-identity-dead-0000000000000000000000000000000000000000000000000000000',
              proposal_id: 'proposal-verify-5',
              operation: 'retire_entry',
              outcome: 'retire',
              checked_at: '2026-08-01',
              retry_after: '2026-08-31',
              coverage: 'partial',
              reason: 'the old line no longer exists',
              citations: 1,
              receipt_id: '',
              conflicted: true,
            },
          ],
          more_proposals: 0,
        },
        superseded: null,
        excerpt: 'Nadia leads the platform team.',
      },
      status: 'candidate', disposition: '', deferred_until: '',
      completable: false, completion_moves_folder: false,
      pending_verification: null,
      // "Go and look again" is not a claim that a note is disposable, so a note
      // whose ONLY finding is an overdue fact gets no Retire button.
      retirement_offered: false,
    },
    {
      // Asked and answered: no proposal, and the check state says so in place
      // of the age flag the queue would otherwise raise.
      candidate_id: 'pend0000000000000000000d4',
      workspace: 'alpha',
      path: 'memory-vault/alpha/Projects/Cassiopeia.md',
      content_hash: 'hash-settled',
      signals: ['unverified'],
      priority: 0,
      evidence: {
        backlinks: [], outbound_links: [], bridge: false, duplicate_group: [],
        last_update: '2024-01-05', type: 'project', age_days: 999,
        unverified: { age_days: 999, threshold_days: 30, last_verified: '2024-01-05', source: 'frontmatter' },
        verification: check({
          outcome: 'unverified', coverage: 'partial', citations: 0, receipt_id: '',
          reason: 'the tracker connector was down', pending: false, proposal_id: '',
        }),
        superseded: null,
        excerpt: 'Cassiopeia ships quarterly.',
      },
      status: 'candidate', disposition: '', deferred_until: '',
      completable: false, completion_moves_folder: false,
      pending_verification: null,
      retirement_offered: true,
    },
  ],
  trashed: [],
  cleared: [],
}

/** The note-edit proposals the review rows link to. */
export const VERIFICATION_PROPOSALS = [
  {
    id: 'proposal-verify-1',
    kind: 'note_edit',
    target: 'People/Team.md',
    text: 'People/Team.md — retire (rev 3f9a1c02): the team page was replaced by the org chart',
    source: 'note verification · retire',
    workspace: 'alpha',
    line: 1,
    note_edit: {
      id: 'fixture0000000000a1',
      operation: 'retire',
      outcome: 'retire',
      settled: '',
      receipt_id: '',
      can_accept: true,
      reason: 'the team page was replaced by the org chart',
    },
  },
  {
    id: 'proposal-verify-2',
    kind: 'note_edit',
    target: 'Projects/Atlas.md',
    text: 'Projects/Atlas.md — replace (rev 88c0de55): the tracker moved to a new system',
    source: 'note verification · replace',
    workspace: 'alpha',
    line: 2,
    note_edit: {
      id: 'fixture0000000000b2',
      operation: 'replace',
      outcome: 'update',
      settled: '',
      receipt_id: '',
      can_accept: true,
      reason: 'the tracker moved to a new system',
    },
  },
  {
    id: 'proposal-verify-4',
    kind: 'note_edit',
    target: 'People/Nadia.md',
    text: 'People/Nadia.md — replace_entry (entry a1b2c3): the reorg moved her under the COO',
    source: 'note verification · replace_entry',
    workspace: 'alpha',
    line: 3,
    note_edit: {
      id: 'fixture0000000000e4',
      operation: 'replace_entry',
      outcome: 'update',
      settled: '',
      receipt_id: '',
      can_accept: true,
      reason: 'the reorg moved her under the COO',
    },
  },
  {
    id: 'proposal-verify-5',
    kind: 'note_edit',
    target: 'People/Nadia.md',
    text: 'People/Nadia.md — retire_entry (entry d4e5f6): the old line no longer exists',
    source: 'note verification · retire_entry',
    workspace: 'alpha',
    line: 4,
    note_edit: {
      id: 'fixture0000000000e5',
      operation: 'retire_entry',
      outcome: 'retire',
      settled: '',
      receipt_id: '',
      can_accept: true,
      reason: 'the old line no longer exists',
    },
  },
]


/** The history rows a settled verification leaves behind. */
/** The one line an entry-scope retirement removes, and the note around it. */
const NADIA_ENTRY = '- Reports to the CTO [verified: 2019-05-01]\n'
const NADIA_NOTE_BEFORE = '---\ntype: person\nupdated: 2026-09-29\n---\n\n# Nadia\n\n- Based in Lisbon [verified: 2026-09-28]\n- Leads the platform team [verified: 2026-09-29]\n- Reports to the CTO [verified: 2019-05-01]\n'
const NADIA_NOTE_AFTER = '---\ntype: person\nupdated: 2026-09-29\n---\n\n# Nadia\n\n- Based in Lisbon [verified: 2026-09-28]\n- Leads the platform team [verified: 2026-09-29]\n'
const NADIA_ENTRY_START = NADIA_NOTE_BEFORE.indexOf(NADIA_ENTRY)
const NADIA_ENTRY_END = NADIA_ENTRY_START + NADIA_ENTRY.length

export const VERIFICATION_HISTORY = {
  rows: [
    {
      id: 'hist-accepted',
      ts: '2026-09-28T10:12:00+00:00',
      action: 'accepted',
      via: 'pwa',
      kind: 'note_edit',
      text: 'Projects/Atlas.md — replace (rev 88c0de55): the tracker moved to a new system',
      source: 'note verification · replace',
      workspace: 'alpha',
      destination: 'Projects/Atlas.md',
      outcome: 'written',
      proposal_id: 'proposal-verify-9',
      note_edit: {
        id: 'fixture0000000000b9',
        relative_path: 'Projects/Atlas.md',
        operation: 'replace',
        outcome: 'update',
        coverage: 'complete',
        before: '---\ntype: project\nupdated: 2024-01-05\n---\n\nAtlas ships on the last Friday of the month.\n',
        after: '---\ntype: project\nupdated: 2026-09-28\n---\n\nAtlas ships on the last Thursday of the month.\n',
        reason: 'the tracker moved to a new system',
        evidence: [{
          source_type: 'url',
          source_ref: 'https://tracker.example.test/atlas',
          quoted: 'Atlas moved to the last Thursday of the month',
          supports: 'Projects/Atlas.md: when Atlas ships',
        }],
        settled: '2026-09-28T10:12:00Z',
        accepted: true,
        receipt_id: 'mrcpt_fixture_2',
        pending: false,
      },
      change: {
        receipt_id: 'mrcpt_fixture_2',
        kind: 'note_apply',
        status: 'applied',
        destination: 'Projects/Atlas.md',
        undoable: true,
        changed: true,
        ts: '2026-09-28T10:11:59+00:00',
      },
    },
    {
      id: 'hist-retired',
      ts: '2026-09-27T18:03:00+00:00',
      action: 'accepted',
      via: 'pwa',
      kind: 'note_edit',
      text: 'Projects/Borealis.md — retire (rev 21bd77c0): folded into Atlas',
      source: 'note verification · retire',
      workspace: 'alpha',
      // A retirement moved the note to the review trash and journalled no memory
      // receipt, so it is reversible through Vault Review's restore rather than
      // reported as a change with no snapshot.
      destination: 'Workspace/.vault-trash/fixture0000000000c3.md',
      outcome: 'written',
      proposal_id: 'proposal-verify-8',
      reversible_by: 'restore',
      note_edit: {
        id: 'fixture0000000000c3',
        relative_path: 'Projects/Borealis.md',
        operation: 'retire',
        outcome: 'retire',
        coverage: 'complete',
        before: '---\ntype: project\nupdated: 2024-01-05\n---\n\nBorealis is superseded by Atlas.\n',
        after: '',
        reason: 'folded into Atlas',
        evidence: [{
          source_type: 'chat',
          source_ref: 'chat-2026-09-20',
          quoted: 'Borealis is now part of Atlas',
          supports: 'Projects/Borealis.md: what Borealis is',
        }],
        settled: '2026-09-27T18:03:00Z',
        accepted: true,
        receipt_id: '',
        pending: false,
      },
    },
    {
      // An entry-scope retirement: one line out of a note that keeps its other
      // two facts. It is a browser case for the two halves that jsdom cannot
      // judge — whether the card says the rest of the file is untouched, and
      // whether the Undo affordance is real (a `note_apply` receipt, so it is).
      id: 'hist-entry-retired',
      ts: '2026-09-30T09:04:00+00:00',
      action: 'accepted',
      via: 'pwa',
      kind: 'note_edit',
      text: 'People/Nadia.md — retire_entry (entry a1b2c3): the reorg ended this reporting line',
      source: 'note verification · retire_entry',
      workspace: 'alpha',
      destination: 'People/Nadia.md',
      outcome: 'written',
      proposal_id: 'proposal-verify-5',
      note_edit: {
        id: 'fixture0000000000e5',
        relative_path: 'People/Nadia.md',
        operation: 'retire_entry',
        outcome: 'retire',
        coverage: 'complete',
        before: NADIA_NOTE_BEFORE,
        after: NADIA_NOTE_AFTER,
        reason: 'the reorg ended this reporting line',
        evidence: [{
          source_type: 'url',
          source_ref: 'https://intranet.example.test/reorg',
          quoted: 'Nadia now reports to the COO.',
          supports: 'People/Nadia.md: who Nadia reports to',
        }],
        settled: '2026-09-30T09:04:00Z',
        accepted: true,
        receipt_id: 'mrcpt_fixture_entry',
        pending: false,
        scope: 'entry',
        entry_identity: 'entry-identity-aged-000000000000000000000000000000000000000000000000000000',
        entry_fingerprint: 'f'.repeat(64),
        entry_span: [NADIA_ENTRY_START, NADIA_ENTRY_END],
        entry_before: NADIA_ENTRY,
        entry_after: '',
        entry_removed: true,
        entry_recovery_error: '',
      },
      change: {
        receipt_id: 'mrcpt_fixture_entry',
        kind: 'note_apply',
        status: 'applied',
        destination: 'People/Nadia.md',
        undoable: true,
        changed: true,
        ts: '2026-09-30T09:03:59+00:00',
      },
    },
  ],
  total: 3,
  truncated: false,
  limit: 200,
  at_max: false,
}

/**
 * The note receipt a settled verification's accept left behind, in the shape
 * `GET /api/memory/receipts/{id}` serves. A `note_apply` carries the note's own
 * before/after pair, so it is both viewable and undoable — which is the whole
 * difference between an applied verdict and a dismissal.
 */
export const VERIFICATION_RECEIPT = {
  receipt_id: 'mrcpt_fixture_2',
  kind: 'note_apply',
  status: 'applied',
  destination: 'Projects/Atlas.md',
  actor: 'operator',
  source: 'curation',
  ts: '2026-09-28T10:11:59+00:00',
  undoable: true,
  changed: true,
  has_snapshot: true,
  reason: '',
  before_text: '---\ntype: project\nupdated: 2024-01-05\n---\n\nAtlas ships on the last Friday of the month.\n',
  after_text: '---\ntype: project\nupdated: 2026-09-28\n---\n\nAtlas ships on the last Thursday of the month.\n',
  diff: [
    { op: 'removed', text: 'updated: 2024-01-05' },
    { op: 'added', text: 'updated: 2026-09-28' },
    { op: 'removed', text: 'Atlas ships on the last Friday of the month.' },
    { op: 'added', text: 'Atlas ships on the last Thursday of the month.' },
  ],
  diff_truncated: false,
}

/**
 * The receipt behind an entry-scope accept. A `note_apply` carries the note's
 * whole before/after pair, so an entry retirement is reversible the same way a
 * whole-note one is — which is the claim the History card makes, and a browser
 * test is the only place the Undo button can be shown to be real.
 */
export const VERIFICATION_ENTRY_RECEIPT = {
  receipt_id: 'mrcpt_fixture_entry',
  kind: 'note_apply',
  status: 'applied',
  destination: 'People/Nadia.md',
  actor: 'operator',
  source: 'curation',
  ts: '2026-09-30T09:03:59+00:00',
  undoable: true,
  changed: true,
  has_snapshot: true,
  reason: '',
  before_text: NADIA_NOTE_BEFORE,
  after_text: NADIA_NOTE_AFTER,
  diff: [
    { op: 'removed', text: '- Reports to the CTO [verified: 2019-05-01]' },
  ],
  diff_truncated: false,
}

/**
 * The graph as the server sends it once the managed pass has been at this
 * vault: the same three notes, each carrying the check state for its CURRENT
 * revision. A settled check clears the node's `stale` flag, which is the whole
 * point — the map used to count these as unchecked and link onward to a queue
 * that had deliberately stopped asking.
 */
/** One note's per-entry coverage, as the map reports it. */
const entryCoverage = (over = {}) => ({
  entries: 3, checked: 3, exempt: 0, unverified: 1, uncovered: 0, stale: 1,
  coverage_ratio: 0.72, fully_verified: false,
  // Both kinds the counts above name, so the tile's own numbers and its list
  // agree: a server that reported `unverified: 1` and sent no such entry would
  // be describing a fact the reader cannot see.
  stale_entries: [
    {
      identity: 'entry-identity-aged-000000000000000000000000000000000000000000000000000000',
      excerpt: '- Reports to the CTO [verified: 2019-05-01]',
      reason: 'aged',
      detail: 'unverified for 2698d against a 90d horizon',
      age_days: 2698,
      last_verified: '2019-05-01',
      own_date: true,
    },
    {
      identity: 'entry-identity-uncov-000000000000000000000000000000000000000000000000000000',
      excerpt: '- Leads the platform team',
      reason: 'no-stamp',
      detail: "nobody has recorded a [verified:] check on this entry; it is unverified for 1d on the note's own last-verified date",
      age_days: 1,
      last_verified: '2026-09-29',
      own_date: false,
    },
  ],
  more_stale_entries: 0,
  ...over,
})

export const VERIFICATION_NODES = [
  {
    ...MEMORY_NODES[1],
    stale: false,
    age_days: 999,
    threshold_days: 90,
    check: check({ outcome: 'retire', reason: 'the team page was replaced by the org chart', citations: 2, receipt_id: '', pending: true, proposal_id: 'proposal-verify-1' }),
  },
  {
    ...MEMORY_NODES[0],
    stale: false,
    age_days: 4,
    threshold_days: 30,
    check: check({ outcome: 'unverified', coverage: 'partial', reason: 'the tracker connector was down', citations: 0, pending: false, proposal_id: '' }),
  },
  {
    ...MEMORY_NODES[2],
    stale: true,
    age_days: 900,
    threshold_days: 180,
    check: check({ outcome: 'retire', checked_at: '2026-08-01', retry_after: '2026-08-31', settled: false, pending: false, conflicted: true, proposal_id: 'proposal-verify-3', reason: 'folded into Atlas', citations: 1, receipt_id: '' }),
  },  {
    // A note whose own date is current and which still holds a fact from 2019.
    // Absent from the `stale` count and present in the facts one, which is the
    // whole reason the toolbar reports the two separately. A new node rather
    // than a repurposed one: the three above are each already the subject of an
    // assertion in note-verification.spec.ts.
    id: 'memory-vault/alpha/People/Nadia.md',
    title: 'Nadia',
    type: 'person-colleague',
    tags: ['team'],
    aliases: [],
    description: 'Who Nadia is, where she sits, and who she works with.',
    workspace: 'alpha',
    degree: 1,
    mtime: 1780200000,
    updated: '2026-09-29',
    stale: false,
    age_days: 1,
    threshold_days: 90,
    check: null,
    entry_coverage: entryCoverage(),
  },
]
