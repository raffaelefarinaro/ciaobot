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
 */
import http from 'node:http'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { acceptUpgrade } from './ws.mjs'
import { WORKSPACES, PROJECTS, CHATS, SCHEDULES, PROPOSALS, MEMORY_NODES, MEMORY_EDGES, MEMORY_CATEGORIES, VERIFICATION_REVIEW, VERIFICATION_PROPOSALS, VERIFICATION_HISTORY, VERIFICATION_RECEIPT, VERIFICATION_NODES, snapshotFrame } from './data.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
const STATIC_ROOT = path.resolve(here, '../../../ciao/web/static')
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

const sessions = new Map()

function sessionOf(req) {
  const cookie = req.headers.cookie || ''
  const id = /(?:^|;\s*)e2e_session=([^;]+)/.exec(cookie)?.[1] || 'default'
  let state = sessions.get(id)
  if (!state) {
    state = { activeStreams: [], sockets: new Set(), connections: 0, transcript: null }
    sessions.set(id, state)
  }
  return state
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
  '/api/workspaces': () => ({
    workspaces: WORKSPACES,
    active: WORKSPACES[0].name,
    provider_options: [{ value: 'claude', label: 'Claude' }],
  }),
  // /api/projects, /api/chats and /api/schedules are bare arrays, not envelopes.
  '/api/projects': () => PROJECTS,
  '/api/chats': () => CHATS,
  '/api/schedules': () => SCHEDULES,
  '/api/subagents/running': () => ({ chats: {} }),
  '/api/housekeeping': () => ({ actions: [] }),
  // The three queues behind "To decide". A spec that wants the managed
  // verification states (a pending proposal linked from a review row, a dead
  // proposal, a settled verdict) opts in through POST /__fixture__/verification
  // rather than replacing these defaults for everybody: the other review specs
  // assert the empty and the plain states, which must stay the default.
  '/api/proposals': (req) => (sessionOf(req).verification ? { rows: [...PROPOSALS, ...VERIFICATION_PROPOSALS] } : { rows: PROPOSALS }),
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
  // Settings → Models reads these. The real endpoints always send every
  // field; an empty object crashed the tab, so the stub mirrors the shape.
  '/api/settings/routines': () => ({
    insights_model: '',
    insights_enabled: true,
    critique_models: '',
    provider_default_models: {},
    provider_default_modes: {},
    provider_default_thinking: {},
    provider_insights_models: {},
    insights_model_effective: 'synthetic-model',
    insights_model_by_workspace: {},
    critique_models_effective: '',
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
}

/** Path patterns, for the routes that carry an id. */
const GET_PATTERNS = [
  // An empty array is the "nothing to merge" short-circuit in the store; the
  // fallback `{}` would be read as a pagination envelope and crash on
  // `env.items`. The transcript is per-session, so a spec opts in through
  // POST /__fixture__/transcript rather than giving every spec one.
  [/^\/api\/chats\/[^/]+\/messages$/, (req) => sessionOf(req).transcript || []],
  [/^\/api\/chats\/[^/]+\/subagents$/, () => ({ subagents: [] })],
  // The receipt behind a settled verification's accept, so the History card's
  // Undo is the real affordance a `note_apply` carries rather than an
  // assertion about one.
  [/^\/api\/memory\/receipts\/mrcpt_fixture_2$/, () => VERIFICATION_RECEIPT],
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

// ── Request routing ─────────────────────────────────────────────────────

const server = http.createServer(async (req, res) => {
  const { pathname } = new URL(req.url, `http://127.0.0.1:${PORT}`)

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
      state.transcript = TRANSCRIPT
      return sendJson(res, { ok: true, turns: TRANSCRIPT.length })
    }
    return sendJson(res, { error: 'unknown fixture route' }, 404)
  }

  if (pathname.startsWith('/api/')) {
    if (req.method === 'GET') {
      const handler = GET_ROUTES[pathname]
        || GET_PATTERNS.find(([pattern]) => pattern.test(pathname))?.[1]
      return sendJson(res, handler ? handler(req) : {})
    }
    const body = await readBody(req)
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
  if (pathname !== '/ws/events') return // per-chat sockets: accept and stay quiet
  const state = sessionOf(req)
  state.connections += 1
  state.sockets.add(ws)
  socket.on('close', () => state.sockets.delete(ws))
  ws.send(snapshotFrame(state.activeStreams))
})

server.listen(PORT, '127.0.0.1', () => {
  console.log(`[fixture] listening on http://127.0.0.1:${PORT}`)
})
