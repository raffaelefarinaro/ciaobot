/**
 * Synthetic host for the browser regression suite.
 *
 * Serves the built PWA out of `ciao/web/static` and answers the API and
 * WebSocket routes it boots against with invented data. Nothing here reads a
 * vault, spawns a model, or touches launchd: the suite must be runnable on a
 * contributor's laptop and in CI without a Python environment, credentials or
 * any of the user's own state.
 *
 * Control surface (only ever called by the tests):
 *   POST /__fixture__/streams   { chat_ids: [...] }  set the next snapshot
 *   POST /__fixture__/drop-ws                        sever every events socket
 *   GET  /__fixture__/ws-count                       sockets opened so far
 *   POST /__fixture__/transcript                     give this session a chat history
 *   POST /__fixture__/update-tasks                   give this session update tasks
 *   POST /__fixture__/chat-frame                     speak on a chat's own socket
 *   POST /__fixture__/terminal-offer { chat_id, command }  offer a suggested command
 */
import http from 'node:http'
import fs from 'node:fs'
import path from 'node:path'
import { randomBytes } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { acceptUpgrade } from './ws.mjs'
import { WORKSPACES, PROJECTS, CHATS, SCHEDULES, PROPOSALS, MEMORY_NODES, MEMORY_EDGES, MEMORY_CATEGORIES, UPDATE_TASKS, VERIFICATION_REVIEW, VERIFICATION_PROPOSALS, VERIFICATION_HISTORY, VERIFICATION_RECEIPT, VERIFICATION_ENTRY_RECEIPT, VERIFICATION_NODES, snapshotFrame } from './data.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
const STATIC_ROOT = path.resolve(here, '../../../ciao/web/static')
// The terminal renderer's browser harness, in its own build tree. It is a
// test-only page: the production build writes to a different directory, so
// the PWA that ships can never carry it.
const HARNESS_ROOT = path.resolve(here, './harness-dist')
const PORT = Number(process.env.CIAO_E2E_PORT || 4599)

if (!fs.existsSync(path.join(STATIC_ROOT, 'index.html'))) {
  console.error(`[fixture] no build at ${STATIC_ROOT} — run \`npm run build\` first`)
  process.exit(1)
}

// ── Mutable fixture state, keyed per browser context ────────────────────
//
// One server serves every worker, so a spec that changes the fixture's mind
// (the reconnect journey rewrites the snapshot) must not be visible to a spec
// running beside it. Each test sets an `e2e_session` cookie on its context;
// the cookie rides both the API calls and the WebSocket handshake, so state
// stays per-test even under `fullyParallel`.
/**
 * One task on the board, and a reply that links it by its `/tasks/<id>`
 * address, so a spec can click the link in a real transcript and see the task
 * open in place rather than in a second tab (lib/appLinks.ts).
 */
const TASK_ID = '0123456789abcdef0123456789abcdef'
const TASK = {
  id: TASK_ID,
  title: 'Reply to Ivo in the Feedback Tracker',
  status: 'backlog',
  project_id: '',
  due: '',
  assignee: 'user',
  chat_id: '',
  attempt_id: '',
  created_at: '2026-01-01T09:00:00+00:00',
  updated_at: '2026-01-01T09:00:00+00:00',
  revision: 'a'.repeat(64),
  relative_path: `Workspace/Tasks/${TASK_ID}.md`,
  attempt_state: '',
  attempt_outcome: '',
  attempt_summary: '',
  attempt_detail: '',
  live_attempt_id: '',
}
const TASK_LINK_TRANSCRIPT = [
  { role: 'user', content: 'What is left from yesterday?', sent_at: '2026-01-01T09:50:00Z', turn_index: 0 },
  {
    role: 'assistant',
    content: `One carryover item: [Reply to Ivo](/tasks/${TASK_ID}).`,
    sent_at: '2026-01-01T09:50:20Z',
    duration_ms: 20000,
    effective_model: 'synthetic-model',
    usage: { input_tokens: 18, output_tokens: 64, context_pct: '1%' },
    turn_index: 0,
  },
]

/**
 * Two settled turns, with turn metadata on each closing reply, so a spec can
 * select a message and see the action footer a real transcript renders.
 */
const TRANSCRIPT = [
  { role: 'user', content: 'First request.', sent_at: '2026-01-01T09:50:00Z', turn_index: 0 },
  {
    role: 'assistant',
    content: 'First answer.',
    sent_at: '2026-01-01T09:50:20Z',
    duration_ms: 20000,
    effective_model: 'synthetic-model',
    usage: { input_tokens: 18, output_tokens: 6478, context_pct: '39.2%' },
    turn_index: 0,
  },
  { role: 'user', content: 'Second request.', sent_at: '2026-01-01T09:52:00Z', turn_index: 1 },
  {
    role: 'assistant',
    content: 'Second answer, and the last turn in the transcript.',
    sent_at: '2026-01-01T09:53:00Z',
    duration_ms: 60000,
    effective_model: 'synthetic-model',
    usage: { input_tokens: 18, output_tokens: 6478, context_pct: '39.2%' },
    turn_index: 1,
  },
]

/**
 * One turn whose facts are wider than a phone: a long model name and six-digit
 * token counts, so the footer strip has to scroll sideways rather than wrap or
 * clip. Guards the action row's facts staying on the buttons' line at 390px.
 */
const TURN_FACTS_TRANSCRIPT = [
  { role: 'user', content: 'Summarise the release.', sent_at: '2026-01-01T09:50:00Z', turn_index: 0 },
  {
    role: 'assistant',
    content: 'The release is ready.',
    sent_at: '2026-01-01T09:51:23Z',
    duration_ms: 83000,
    effective_model: 'claude-opus-5-5-with-a-deliberately-long-model-name',
    usage: { input_tokens: 123456, output_tokens: 987654, context_pct: '99.9%' },
    turn_index: 0,
  },
]

/**
 * A settled reply followed by the user's comment-reference turn: the sent
 * `<user-comment-reference>` card (see lib/commentContext.ts) sits immediately
 * under the reply it annotates. It exists so a browser spec can select the
 * reply and measure that its accent outline does not cross the reference card
 * below it (#968), which jsdom's 0x0 rects cannot see.
 */
const COMMENT_TRANSCRIPT = [
  { role: 'user', content: 'First request.', sent_at: '2026-01-01T09:50:00Z', turn_index: 0 },
  {
    role: 'assistant',
    content: 'First answer, long enough to wrap on a phone-sized transcript.',
    sent_at: '2026-01-01T09:50:20Z',
    duration_ms: 20000,
    effective_model: 'synthetic-model',
    usage: { input_tokens: 18, output_tokens: 6478, context_pct: '39.2%' },
    turn_index: 0,
  },
  {
    role: 'user',
    content: [
      '<user-comment-reference>',
      '<reference-source>assistant message, paragraph 1</reference-source>',
      '<quoted-text>First answer, long enough to wrap on a phone-sized transcript.</quoted-text>',
      '<user-comment>we already have this</user-comment>',
      '</user-comment-reference>',
      '',
      'and a note that rides along with it',
    ].join('\n'),
    sent_at: '2026-01-01T09:52:00Z',
    turn_index: 1,
  },
]

/**
 * One OpenCode-shaped turn as the server replays it: a phase-less text row per
 * part, then tool work, then a short reply that opens like narration, then a
 * reasoning-only step. Guards the reply against being folded into Activity.
 */
const FOLD_TRANSCRIPT = [
  { role: 'user', content: 'Update both files.', sent_at: '2026-01-01T09:50:00Z', turn_index: 0 },
  { role: 'assistant', content: `Both files need the same change. ${'Detail. '.repeat(30)}` },
  { role: 'system', content: 'Edit a.vue', tool_name: '_activity' },
  { role: 'assistant', content: 'Good — updated both files.', sent_at: '2026-01-01T09:50:20Z', turn_index: 0 },
  { role: 'system', content: 'wrapping up', tool_name: '_thinking' },
]

const sessions = new Map()

/** The workspace registry as this session sees it: a saved Memory handover shows on its workspace. */
function workspacesPayload(state) {
  return {
    workspaces: WORKSPACES.map(w => (state.handover[w.name] ? { ...w, memory_handover: state.handover[w.name] } : w)),
    active: WORKSPACES[0].name,
    provider_options: [{ value: 'claude', label: 'Claude' }],
  }
}

function sessionOf(req) {
  const cookie = req.headers.cookie || ''
  const id = /(?:^|;\s*)e2e_session=([^;]+)/.exec(cookie)?.[1] || 'default'
  let state = sessions.get(id)
  if (!state) {
    state = { activeStreams: [], sockets: new Set(), chatSockets: new Map(), pendingChatFrames: new Map(), connections: 0, transcript: null, terminals: new Map(), terminalOffers: new Map(), terminalUnlocked: false, appearance: { text_contrast: 'normal', theme: null, font_scale: null, revision: 'a0' }, handover: {} }
    sessions.set(id, state)
  }
  return state
}

// ── The suggested command ───────────────────────────────────────────────
//
// The engine holds one optional offer per chat: a command the assistant
// suggested, which the user reviews before running. The fixture keeps it per
// browser session, for the same reason the transcript is per session — one
// server serves every worker, and a spec that offers a command in one chat
// must not be visible to a spec running beside it.
//
// The offer is never executed here. It is a string the browser shows, copies
// and dismisses; nothing in this file runs it.

// A fresh 32-hex offer id, the way the engine mints one.
function mintOfferId() {
  return randomBytes(16).toString('hex')
}

/** The offer id shape the engine mints: 32 lowercase hex characters. */
const OFFER_ID_PATTERN = /^[0-9a-f]{32}$/

/** The longest command the engine accepts, in bytes. */
const OFFER_COMMAND_MAX_BYTES = 4096

/** The offer this session holds for a chat, or null. */
function offerFor(state, chatId) {
  return state.terminalOffers.get(chatId) || null
}

/** A chat row carrying this session's offer. */
function chatWithOffer(state, chat) {
  return { ...chat, terminal_offer: offerFor(state, chat.chat_id) }
}

/** The chat list, with this session's offers on the rows. */
function chatsWithOffers(state) {
  return CHATS.map(chat => chatWithOffer(state, chat))
}

/** One chat's row, with this session's offer on it. */
function chatDetail(state, chatId) {
  const chat = CHATS.find(c => c.chat_id === chatId)
  if (!chat) return {}
  return chatWithOffer(state, chat)
}

/** The chat id in an `/api/chats/<id>` path, or ''. */
function chatIdFromPath(pathname) {
  const match = /^\/api\/chats\/([^/]+)$/.exec(pathname)
  return match ? decodeURIComponent(match[1]) : ''
}

/**
 * Dismiss the command a chat offered.
 *
 * The body is exactly `{terminal_offer:{expected_id}}` and nothing else: a
 * dismissal never carries a pin, a title or any other update, so it can
 * never race another write on the same chat — and a body that does carry one
 * is refused rather than half-applied. The shape is parsed before the chat
 * is looked up, so a malformed request is a static 400 and never a 404.
 *
 * An offer with a different id is a conflict — the client asked to dismiss a
 * command that is no longer the one on offer — and the engine's current
 * offer comes back with it. No offer at all is an idempotent success.
 */
function dismissOffer(state, chatId, body) {
  const keys = Object.keys(body)
  if (keys.length !== 1 || keys[0] !== 'terminal_offer') {
    return { status: 400, body: { error: 'bad_request' } }
  }
  const request = body.terminal_offer
  if (!request || typeof request !== 'object' || Array.isArray(request)) {
    return { status: 400, body: { error: 'bad_request' } }
  }
  const requestKeys = Object.keys(request)
  if (requestKeys.length !== 1 || requestKeys[0] !== 'expected_id') {
    return { status: 400, body: { error: 'bad_request' } }
  }
  const expectedId = request.expected_id
  if (typeof expectedId !== 'string' || !OFFER_ID_PATTERN.test(expectedId)) {
    return { status: 400, body: { error: 'bad_request' } }
  }
  const chat = CHATS.find(c => c.chat_id === chatId)
  if (!chat) return { status: 404, body: { error: 'not found' } }
  const current = offerFor(state, chatId)
  if (current && current.id !== expectedId) {
    return { status: 409, body: { error: 'terminal_offer_conflict', terminal_offer: current } }
  }
  state.terminalOffers.delete(chatId)
  return { status: 200, body: { ...chat, terminal_offer: null } }
}

// ── The browser terminal ────────────────────────────────────────────────
//
// A synthetic shell. It answers the terminal's own HTTP routes and speaks
// its byte protocol on a socket, so the pane can be driven end to end in a
// real browser: open it from a chat, type at it, watch the output, resize
// it, hide it and open it again.
//
// The shell is invented rather than real: `echo` answers with its argument
// and nothing else, so the suite never needs a shell on the contributor's
// machine, and a stray command cannot touch anything.

const TERMINAL_FOLDER = '/synthetic/workspace'
const TERMINAL_PLATFORM = 'macos'
const TERMINAL_HOST = 'fixture.local'

/** The sessions this browser context owns, keyed by id. */
function terminalSessions(state) {
  if (!state.terminals) state.terminals = new Map()
  return state.terminals
}

/** The scope a session belongs to: one chat, or one project. */
function terminalScope(url) {
  const chat = url.searchParams.get('chat_id')
  if (chat) return `chat:${chat}`
  const project = url.searchParams.get('project_id')
  if (project) return `project:${project}`
  return ''
}

/** What the pane's session list shows. */
function terminalInfo(session) {
  return {
    session_id: session.session_id,
    folder: session.folder,
    cols: session.cols,
    rows: session.rows,
    seq: session.seq,
    attached: session.sockets.size > 0,
    exited: session.exited,
    exit_code: session.exit_code,
  }
}

/** What a shell answers to a line the user typed. */
function shellOutput(line) {
  const command = line.replace(/[\r\n]+$/, '')
  const echoed = `${command}\r\n`
  if (command.startsWith('echo ')) return echoed + `${command.slice(5)}\r\n`
  return echoed
}

/** Push bytes down the stream, keeping the offsets honest. */
function terminalEmit(session, ws, text) {
  const bytes = Buffer.from(text, 'utf8')
  const start = session.seq
  session.output = Buffer.concat([session.output, bytes])
  session.seq = session.output.length
  ws.send({ type: 'output', start, end: session.seq, data: bytes.toString('base64') })
}

/** The terminal's HTTP half. */
async function terminalApi(req, res, pathname, url, state) {
  const terminals = terminalSessions(state)
  if (pathname === '/api/terminal/status' && req.method === 'GET') {
    // No grant until the browser has asked for one: the pane obtains it
    // by itself, so a spec that opens the terminal exercises that path
    // rather than finding a live grant waiting for it.
    return sendJson(res, {
      available: true,
      reason: '',
      platform: TERMINAL_PLATFORM,
      host: TERMINAL_HOST,
      unlock_expires_in: state.terminalUnlocked ? 900 : 0,
      detached_ttl_seconds: 600,
    })
  }
  if (pathname === '/api/terminal/unlock' && req.method === 'POST') {
    // The engine's route takes no body: the signed-in session is the
    // terminal's trust boundary, so a request that carries one is refused.
    const raw = await readRawBody(req)
    if (raw.length > 0) {
      return sendJson(
        res,
        { ok: false, reason: 'bad_request', error: 'this route takes no body.' },
        400,
      )
    }
    state.terminalUnlocked = true
    return sendJson(res, { ok: true, expires_in: 900 })
  }
  if (pathname === '/api/terminal/sessions' && req.method === 'GET') {
    const scope = terminalScope(url)
    const sessions = [...terminals.values()].filter((s) => s.scope === scope)
    return sendJson(res, { sessions: sessions.map(terminalInfo) })
  }
  if (pathname === '/api/terminal/sessions' && req.method === 'POST') {
    const body = await readBody(req)
    const scope = body?.chat_id ? `chat:${body.chat_id}` : body?.project_id ? `project:${body.project_id}` : ''
    const id = `term-${terminals.size + 1}`
    const session = {
      session_id: id,
      scope,
      folder: TERMINAL_FOLDER,
      cols: 80,
      rows: 24,
      seq: 0,
      output: Buffer.alloc(0),
      pending: '',
      sockets: new Set(),
      exited: false,
      exit_code: null,
    }
    terminals.set(id, session)
    return sendJson(res, { session: terminalInfo(session) })
  }
  if (pathname.startsWith('/api/terminal/sessions/') && req.method === 'DELETE') {
    const id = decodeURIComponent(pathname.slice('/api/terminal/sessions/'.length))
    terminals.delete(id)
    return sendJson(res, { ok: true })
  }
  return sendJson(res, { error: 'unknown terminal route' }, 404)
}

// ── API ─────────────────────────────────────────────────────────────────

/**
 * Exact-path GET responses. Anything not listed falls through to `{}` with a
 * 200, which is what the real server does for the long tail of optional
 * panels: the suite asserts on behaviour, not on the stub's completeness, and
 * a 404 here would surface as the PWA's "redeploy the server" banner.
 */
const GET_ROUTES = {
  '/api/auth/check': () => ({ ok: true }),
  '/api/startup-status': () => ({
    phases: [],
    overall_ready: true,
    version: '0.0.0-e2e',
    node_role: 'host',
    host_url: null,
    has_host_session: false,
    auth_required: false,
    update_available: false,
  }),
  '/api/workspaces': (req) => workspacesPayload(sessionOf(req)),
  // /api/projects, /api/chats and /api/schedules are bare arrays, not envelopes.
  '/api/projects': () => PROJECTS,
  // The chat list carries this session's suggested commands: a chat holds the
  // command the assistant suggested for it, or none.
  '/api/chats': (req) => chatsWithOffers(sessionOf(req)),
  '/api/schedules': () => SCHEDULES,
  '/api/subagents/running': () => ({ chats: {} }),
  '/api/housekeeping': () => ({ actions: [] }),
  '/api/tasks': () => ({ workspace: WORKSPACES[0].name, tasks: [TASK] }),
  // "After this update" is a second, per-workspace question, so it carries a
  // query string and cannot live in the exact-path table. A spec that needs the
  // section measured on screen opts in through POST /__fixture__/update-tasks;
  // the default stays empty so the other specs keep the Home they already
  // assert on.
  '/api/update-tasks': (req) => (sessionOf(req).updateTasks
    ? { tasks: UPDATE_TASKS }
    : { tasks: [] }),
  // The three queues behind "To decide". A spec that wants the managed
  // verification states (a pending proposal linked from a review row, a dead
  // proposal, a settled verdict) opts in through POST /__fixture__/verification
  // rather than replacing these defaults for everybody: the other review specs
  // assert the empty and the plain states, which must stay the default.
  '/api/proposals': (req) => (sessionOf(req).verification ? { rows: [...PROPOSALS, ...VERIFICATION_PROPOSALS] } : { rows: PROPOSALS }),
  // Settings > Workspace > Overview's Memory handover card.
  '/api/proposals/handover': (req, url) => ({
    workspace: url.searchParams.get('workspace') || WORKSPACES[0].name,
    handover: sessionOf(req).handover[url.searchParams.get('workspace') || WORKSPACES[0].name] || 'off',
    acting_available: true,
    agreement: { decided: 0, matched: 0, rate: null, window: 50, min_decisions: 20, threshold: 0.9, predicted_accept: 0, accept_matched: 0, predicted_dismiss: 0, dismiss_matched: 0, suggest_handover: false },
  }),
  '/api/proposals/history': (req) => (sessionOf(req).verification ? VERIFICATION_HISTORY : { rows: [], total: 0, truncated: false, limit: 200, at_max: true }),
  '/api/vault/graph': (req) => (sessionOf(req).verification
    ? { workspace: WORKSPACES[0].name, workspaces: WORKSPACES.map(w => w.name), nodes: VERIFICATION_NODES, edges: MEMORY_EDGES }
    : { workspace: WORKSPACES[0].name, workspaces: WORKSPACES.map(w => w.name), nodes: MEMORY_NODES, edges: MEMORY_EDGES }),
  '/api/vault/review': (req) => (sessionOf(req).verification ? VERIFICATION_REVIEW : { candidates: [], trashed: [], cleared: [] }),
  '/api/memory/entity-types': () => ({ workspace: WORKSPACES[0].name, vault: '/fixture/vault', types: MEMORY_CATEGORIES }),
  '/api/models': () => ({
    models: ['synthetic-model'],
    default: 'synthetic-model',
    provider_models: { claude: ['synthetic-model'] },
    provider_defaults: { claude: 'synthetic-model' },
    providers: [{ id: 'claude', label: 'Claude', short_label: 'Claude', capabilities: {} }],
    thinking_levels: { claude: ['low', 'high'] },
  }),
  // Settings → Providers reads these. The real endpoints always send every
  // field; an empty object crashed the tab, so the stub mirrors the shape.
  // Engine-wide appearance, kept per browser session like the real engine keeps
  // one document: GET reads it, the PATCH branch below merges known keys and
  // bumps `revision`. theme and font_scale start null (never stored).
  '/api/settings/appearance': (req) => sessionOf(req).appearance,
  '/api/settings/routines': () => ({
    provider_default_models: {},
    provider_default_modes: {},
    provider_default_thinking: {},
    provider_insights_models: {},
    pwa_host: '',
    log_level: '',
    dev_mode: false,
    app_repo: '',
    server_defaults: { pwa_host: '0.0.0.0', log_level: 'info', log_levels: ['debug', 'info', 'warning', 'error'] },
    server_running: { pwa_host: '0.0.0.0', log_level: 'info' },
    model_options: { anthropic: ['synthetic-model'] },
    backends: { anthropic: true, opencode: false },
    workspace_context: { workspace_root: '/fixture/workspace', vault_root: '/fixture/workspace/memory-vault' },
  }),
  '/api/settings/providers': () => ({
    connections: {
      claude: { name: 'claude', label: 'Claude Code', short_label: 'Claude', ok: true, auth: 'ok', command: 'claude', version: '2.0.0', account: 'fixture@example.com' },
    },
  }),
  '/api/status': () => ({ active_model: 'synthetic-model', mode: 'auto', cost: 0 }),
  '/api/stats': () => ({}),
  '/api/package/status': () => ({
    current_version: '0.0.0-e2e',
    latest_version: '0.0.0-e2e',
    update_available: false,
    mode: 'dev',
  }),
  // Settings → Memory backup. The `{}` catch-all would read as a configured
  // install in an unknown state, so the section is served the shape the real
  // route always sends.
  '/api/local/backup': () => ({
    state: 'ready',
    scope: 'memory-vault, skills, subagents, commands',
    branch: 'main',
    remote: 'https://github.com/person/memory.git',
    last_remote: 'https://github.com/person/memory.git',
    enabled: true,
    interval_s: 300,
    last_attempt_at: '2026-01-01T09:05:00Z',
    last_success_at: '2026-01-01T09:05:03Z',
    last_success_commit: 'abc1234',
    pending_changes: 0,
    pending_commits: 0,
    reason: 'up to date',
  }),
  // Settings → Start at sign-in. Read-only on purpose: the panel's one write
  // flips a real machine's launchd/Task Scheduler state, so the fixture answers
  // the read and nothing else. An unknown GET would fall through to the `{}`
  // catch-all, which this panel (rightly) refuses to read as a position.
  '/api/service/login': () => ({
    platform: 'macos',
    supported: true,
    installed: true,
    enabled: true,
    can_change: true,
    reason: 'The engine service will start when this user signs in.',
    setup_command: null,
  }),
}

/**
 * The chats of one project, in the shape the real route answers
 * (`ciao/web/routes_api.py::project_chats`).
 *
 * Without `?archived=1` it answers the project's chats as a bare array; with
 * it, a page envelope over the archived conversations, which is what
 * ProjectView's "Show archived chats" section reads. The `{}` catch-all used
 * to answer this path, and ProjectView read `items` off a payload that had
 * none: the section listed nothing and the whole project pane went blank
 * (#1710).
 */
function projectChats(req, url) {
  const projectId = decodeURIComponent(url.pathname.split('/')[3] || '')
  const chats = CHATS.filter(chat => chat.project_id === projectId)
  if (url.searchParams.get('archived') !== '1') return chats
  const archived = chats
    .filter(chat => chat.archived)
    // The engine sorts by (created_at, chat_id), newest first.
    .sort((a, b) => (a.created_at === b.created_at
      ? b.chat_id.localeCompare(a.chat_id)
      : b.created_at.localeCompare(a.created_at)))
  const offset = Math.max(0, Number(url.searchParams.get('offset')) || 0)
  const limit = Math.max(1, Number(url.searchParams.get('limit')) || 10)
  return {
    items: archived.slice(offset, offset + limit).map(chat => ({
      chat_id: chat.chat_id,
      title: chat.title,
      created_at: chat.created_at,
      archive_path: chat.archive_path || '',
    })),
    total: archived.length,
    offset,
    limit,
  }
}

/** Path patterns, for the routes that carry an id. */
const GET_PATTERNS = [
  // An empty array is the "nothing to merge" short-circuit in the store; the
  // fallback `{}` would be read as a pagination envelope and crash on
  // `env.items`. The transcript is per-session, so a spec opts in through
  // POST /__fixture__/transcript rather than giving every spec one.
  [/^\/api\/chats\/[^/]+\/messages$/, (req) => sessionOf(req).transcript || []],
  [/^\/api\/chats\/[^/]+\/subagents$/, () => ({ subagents: [] })],
  // One chat's row, with this session's suggested command on it.
  [/^\/api\/chats\/[^/]+$/, (req, url) => chatDetail(sessionOf(req), decodeURIComponent(url.pathname.split('/')[3] || ''))],
  [/^\/api\/tasks\/[0-9a-f]{32}$/, () => ({ workspace: WORKSPACES[0].name, task: { ...TASK, body: 'Find his comment and draft a reply.' } })],
  // The receipt behind a settled verification's accept, so the History card's
  // Undo is the real affordance a `note_apply` carries rather than an
  // assertion about one.
  [/^\/api\/memory\/receipts\/mrcpt_fixture_2$/, () => VERIFICATION_RECEIPT],
  [/^\/api\/memory\/receipts\/mrcpt_fixture_entry$/, () => VERIFICATION_ENTRY_RECEIPT],
  [/^\/api\/projects\/[^/]+\/chats$/, projectChats],
]

/**
 * Write routes the suite drives, keyed by path.
 *
 * The categories registry is the one write with a contract a stub has to keep:
 * a `PATCH` is the whole desired list and answers with the list it stored, so a
 * stub answering `{ok: true}` would empty the panel the moment anything saved.
 * It reads the submission and hands it back with the count restored, which is
 * what the server does.
 */
const WRITE_ROUTES = {
  '/api/memory/entity-types': (body) => {
    const submitted = Array.isArray(body?.types) ? body.types : []
    return {
      workspace: WORKSPACES[0].name,
      vault: '/fixture/vault',
      types: submitted.map((row) => ({
        ...row,
        // The count is the server's, and the client does not send it back.
        note_count: MEMORY_CATEGORIES.find((c) => c.id === row?.id)?.note_count ?? 0,
      })),
    }
  },
}

function sendJson(res, body, status = 200) {
  const raw = JSON.stringify(body)
  res.writeHead(status, {
    'content-type': 'application/json',
    'cache-control': 'no-store',
    'content-length': Buffer.byteLength(raw),
  })
  res.end(raw)
}

async function readBody(req) {
  const chunks = []
  for await (const chunk of req) chunks.push(chunk)
  if (!chunks.length) return {}
  try { return JSON.parse(Buffer.concat(chunks).toString('utf8')) } catch { return {} }
}

/** The raw request body, as bytes: what the engine's unlock route looks at. */
async function readRawBody(req) {
  const chunks = []
  for await (const chunk of req) chunks.push(chunk)
  return Buffer.concat(chunks)
}

// ── Static files ────────────────────────────────────────────────────────

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.woff2': 'font/woff2',
  '.webmanifest': 'application/manifest+json',
}

function serveStatic(res, pathname) {
  // Resolve inside STATIC_ROOT and refuse anything that climbs out, so a
  // crafted URL cannot read the rest of the checkout.
  const resolved = path.resolve(STATIC_ROOT, `.${pathname}`)
  const inRoot = resolved === STATIC_ROOT || resolved.startsWith(STATIC_ROOT + path.sep)
  const file = inRoot && fs.existsSync(resolved) && fs.statSync(resolved).isFile()
    ? resolved
    : path.join(STATIC_ROOT, 'index.html') // SPA fallback
  const body = fs.readFileSync(file)
  res.writeHead(200, {
    'content-type': MIME[path.extname(file)] || 'application/octet-stream',
    'cache-control': 'no-store',
    'content-length': body.length,
  })
  res.end(body)
}

/**
 * The terminal renderer's harness page, served from its own build tree.
 *
 * The tree is written by `npm run build:harness` (and by the e2e build the
 * test script runs), so a checkout that never built it answers with a
 * message that says so rather than with the PWA's shell.
 */
function serveHarness(res, pathname) {
  const relative = pathname.slice('/__fixture__/harness/'.length)
  const resolved = path.resolve(HARNESS_ROOT, relative)
  const inRoot = resolved === HARNESS_ROOT || resolved.startsWith(HARNESS_ROOT + path.sep)
  if (!inRoot || !fs.existsSync(resolved) || !fs.statSync(resolved).isFile()) {
    return sendJson(res, { error: 'the terminal harness is not built (npm run build:harness)' }, 404)
  }
  const body = fs.readFileSync(resolved)
  res.writeHead(200, {
    'content-type': MIME[path.extname(resolved)] || 'application/octet-stream',
    'cache-control': 'no-store',
    'content-length': body.length,
  })
  res.end(body)
}

// ── Request routing ─────────────────────────────────────────────────────

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://127.0.0.1:${PORT}`)
  const { pathname } = url

  if (pathname.startsWith('/__fixture__/harness/')) {
    return serveHarness(res, pathname)
  }

  if (pathname.startsWith('/__fixture__/')) {
    const state = sessionOf(req)
    if (pathname === '/__fixture__/streams') {
      const body = await readBody(req)
      state.activeStreams = Array.isArray(body.chat_ids) ? body.chat_ids : []
      return sendJson(res, { ok: true, active_streams: state.activeStreams })
    }
    if (pathname === '/__fixture__/drop-ws') {
      const dropped = state.sockets.size
      for (const socket of state.sockets) socket.drop()
      state.sockets.clear()
      return sendJson(res, { ok: true, dropped })
    }
    if (pathname === '/__fixture__/ws-count') {
      return sendJson(res, { connections: state.connections, open: state.sockets.size })
    }
    if (pathname === '/__fixture__/verification') {
      // Opt this session into the managed-verification payloads. Per session for
      // the same reason the transcript is: one server serves every worker, and a
      // spec that emptied the review queue would be visible to a spec running
      // beside it.
      state.verification = true
      return sendJson(res, { ok: true, candidates: VERIFICATION_REVIEW.candidates.length, history: VERIFICATION_HISTORY.rows.length })
    }
    if (pathname === '/__fixture__/transcript') {
      // Opt this session's chat into having a transcript. The default is an
      // empty history so the other specs see the same chat they saw before; the
      // action-footer journey needs real turns to select, ending at the bottom
      // of the transcript, to reproduce a footer falling under the composer.
      const body = await readBody(req)
      state.transcript = body?.shape === 'opencode-fold'
        ? FOLD_TRANSCRIPT
        : body?.shape === 'comment'
          ? COMMENT_TRANSCRIPT
          : body?.shape === 'task-link'
            ? TASK_LINK_TRANSCRIPT
            : body?.shape === 'turn-facts'
              ? TURN_FACTS_TRANSCRIPT
              : TRANSCRIPT
      return sendJson(res, { ok: true, turns: state.transcript.length })
    }
    if (pathname === '/__fixture__/update-tasks') {
      // Opt this session into having "After this update" on Home. Per session
      // for the same reason the transcript is: the section must appear for the
      // spec that measures the gaps around it without appearing for the specs
      // beside it, which assert the Home they already know.
      state.updateTasks = true
      return sendJson(res, { ok: true, tasks: UPDATE_TASKS.length })
    }
    if (pathname === '/__fixture__/chat-frame') {
      // Speak on a chat's own socket. The fixture is otherwise silent there,
      // and #1413's move chip needs the frames a broker would send for an
      // OpenCode foreground shell: a `tool_use` carrying `backgroundable`, then
      // the `tool_result` and `result` that end it. Frames are just delivered,
      // in order — what they mean is the spec's business. Held for the socket
      // that opens next when none is up yet, so a spec may opt in before it
      // navigates.
      const body = await readBody(req)
      const chatId = String(body?.chat_id || '')
      const frames = Array.isArray(body?.frames) ? body.frames : []
      if (!chatId || !frames.length) {
        return sendJson(res, { error: 'chat_id and a non-empty frames array are required' }, 400)
      }
      const open = state.chatSockets.get(chatId)
      if (open?.size) {
        for (const ws of open) for (const frame of frames) ws.send(frame)
      } else {
        state.pendingChatFrames.set(chatId, [...(state.pendingChatFrames.get(chatId) || []), ...frames])
      }
      return sendJson(res, { ok: true, delivered: open?.size || 0 })
    }
    if (pathname === '/__fixture__/terminal') {
      // What the synthetic shell currently believes: the sessions this
      // browser context owns, and the size it was last told about. A spec
      // reads it to prove a resize actually reached the engine.
      const terminals = terminalSessions(state)
      return sendJson(res, {
        sessions: [...terminals.values()].map(terminalInfo),
      })
    }
    if (pathname === '/__fixture__/terminal-offer') {
      // Offer a suggested command to one chat. The replacement is silent: the
      // engine announces an accepted change over the events socket, and this
      // one is not announced, so the browser only learns about it from a read
      // — or from the conflict a stale dismissal gets back. The id is fresh
      // every time, so a dismissal that names the previous one is stale.
      const body = await readBody(req)
      const chatId = String(body?.chat_id || '')
      const command = String(body?.command || '')
      if (!chatId || !command) {
        return sendJson(res, { error: 'chat_id and command are required' }, 400)
      }
      // The chat has to exist: the engine holds one offer per real chat.
      if (!CHATS.some(c => c.chat_id === chatId)) {
        return sendJson(res, { error: 'unknown chat' }, 404)
      }
      // The command is bounded, like the engine's.
      if (Buffer.byteLength(command, 'utf8') > OFFER_COMMAND_MAX_BYTES) {
        return sendJson(res, { error: 'the command is too long' }, 400)
      }
      const offer = { id: mintOfferId(), command }
      state.terminalOffers.set(chatId, offer)
      return sendJson(res, { ok: true, offer })
    }
    return sendJson(res, { error: 'unknown fixture route' }, 404)
  }

  if (pathname.startsWith('/api/terminal/')) {
    return terminalApi(req, res, pathname, url, sessionOf(req))
  }

  if (pathname.startsWith('/api/')) {
    if (req.method === 'GET') {
      const handler = GET_ROUTES[pathname]
        || GET_PATTERNS.find(([pattern]) => pattern.test(pathname))?.[1]
      return sendJson(res, handler ? handler(req, url) : {})
    }
    const body = await readBody(req)
    // The dismissal of a suggested command: exactly `{terminal_offer:{expected_id}}`,
    // and only a PATCH carries one. Every other write keeps the behaviour it
    // had.
    if (req.method === 'PATCH' && pathname === '/api/settings/appearance') {
      const state = sessionOf(req)
      for (const key of ['text_contrast', 'theme', 'font_scale']) {
        if (body && body[key] !== undefined) state.appearance[key] = body[key]
      }
      state.appearance.revision = `a${Number(state.appearance.revision.slice(1)) + 1}`
      return sendJson(res, state.appearance)
    }
    // A workspace PATCH keeps the Memory handover it saves, per session, and
    // answers with the registry (the store replaces its list from the reply).
    const workspacePatch = req.method === 'PATCH' ? /^\/api\/workspaces\/([^/]+)$/.exec(pathname) : null
    if (workspacePatch) {
      const state = sessionOf(req)
      const name = decodeURIComponent(workspacePatch[1])
      if (body && WORKSPACES.some(w => w.name === name) && typeof body.memory_handover === 'string') {
        state.handover[name] = body.memory_handover
      }
      return sendJson(res, workspacesPayload(state))
    }
    if (req.method === 'PATCH') {
      const chatId = chatIdFromPath(pathname)
      if (chatId && body && typeof body === 'object' && body.terminal_offer !== undefined) {
        const answer = dismissOffer(sessionOf(req), chatId, body)
        return sendJson(res, answer.body, answer.status)
      }
    }
    const write = WRITE_ROUTES[pathname]
    return sendJson(res, write ? write(body) : { ok: true })
  }

  if (pathname === '/sw.js') {
    // An empty worker, not a 404. The PWA registers one unconditionally; a 404
    // logs a browser-level console error the specs would have to allow-list,
    // and the real worker's cache is shared state across specs — exactly what
    // makes a browser suite flaky.
    res.writeHead(200, { 'content-type': 'text/javascript; charset=utf-8' })
    res.end('/* e2e: no-op service worker */\n')
    return
  }

  serveStatic(res, pathname)
})

server.on('upgrade', (req, socket) => {
  const { pathname } = new URL(req.url, `http://127.0.0.1:${PORT}`)
  const ws = acceptUpgrade(req, socket, pathname)
  if (!ws) return
  if (pathname.startsWith('/ws/terminal/')) {
    // The terminal's byte stream. The client attaches first; the fixture
    // answers with the session's state and replays whatever the shell printed
    // before this connection, so a reconnect resumes instead of starting
    // over.
    const sessionId = decodeURIComponent(pathname.slice('/ws/terminal/'.length))
    const state = sessionOf(req)
    const terminals = terminalSessions(state)
    let session = terminals.get(sessionId)
    if (!session) {
      session = {
        session_id: sessionId,
        scope: '',
        folder: TERMINAL_FOLDER,
        cols: 80,
        rows: 24,
        seq: 0,
        output: Buffer.alloc(0),
        pending: '',
        sockets: new Set(),
        exited: false,
        exit_code: null,
      }
      terminals.set(sessionId, session)
    }
    session.sockets.add(ws)
    socket.on('close', () => session.sockets.delete(ws))
    ws.onMessage = (text) => {
      let frame
      try { frame = JSON.parse(text) } catch { return }
      if (frame.type === 'attach') {
        ws.send({
          type: 'ready',
          session_id: sessionId,
          folder: session.folder,
          cols: session.cols,
          rows: session.rows,
          seq: session.seq,
          replay_start: 0,
          truncated: false,
        })
        if (session.seq > 0) {
          ws.send({ type: 'output', start: 0, end: session.seq, data: session.output.toString('base64') })
        }
        ws.send({ type: 'replay_end', seq: session.seq, truncated: false })
        return
      }
      if (frame.type === 'input') {
        session.pending += Buffer.from(String(frame.data || ''), 'base64').toString('utf8')
        if (!/[\r\n]/.test(session.pending)) return
        const line = session.pending
        session.pending = ''
        terminalEmit(session, ws, shellOutput(line))
        return
      }
      if (frame.type === 'resize') {
        session.cols = Number(frame.cols) || session.cols
        session.rows = Number(frame.rows) || session.rows
        return
      }
      if (frame.type === 'ping') ws.send({ type: 'pong' })
    }
    return
  }
  if (pathname !== '/ws/events') {
    // Per-chat sockets: quiet, except for the frames a spec asked for. Holding
    // them in the session state lets a spec deliver a call to the socket that
    // opens after the request (see /__fixture__/chat-frame).
    const chatId = decodeURIComponent(pathname.replace('/ws/chat/', ''))
    const state = sessionOf(req)
    if (!state.chatSockets.has(chatId)) state.chatSockets.set(chatId, new Set())
    state.chatSockets.get(chatId).add(ws)
    socket.on('close', () => state.chatSockets.get(chatId)?.delete(ws))
    for (const frame of state.pendingChatFrames.get(chatId) || []) ws.send(frame)
    state.pendingChatFrames.delete(chatId)
    return
  }
  const state = sessionOf(req)
  state.connections += 1
  state.sockets.add(ws)
  socket.on('close', () => state.sockets.delete(ws))
  const frame = snapshotFrame(state.activeStreams)
  // The suggested-command map is complete, like the pin map: the snapshot
  // replaces the browser's copy, so it must carry every offer this session
  // holds — a chat it does not name holds none.
  frame.chat_terminal_offers = Object.fromEntries(state.terminalOffers)
  ws.send(frame)
})

server.listen(PORT, '127.0.0.1', () => {
  console.log(`[fixture] listening on http://127.0.0.1:${PORT}`)
})
