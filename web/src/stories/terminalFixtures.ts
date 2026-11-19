import type { FixtureRoute, FixtureSocketScript } from './fixtureBoundary'
import type { TerminalSessionInfo } from '../lib/terminalProtocol'

// Synthetic engine answers for the terminal stories. Every name is a
// placeholder (storybook-alpha, storybook-beta); nothing here is a real path,
// host, token or user text. The routes and sockets are declared per story and
// the fixture boundary answers only those, in memory.

export const TERMINAL_CHAT = 'storybook-alpha'
export const TERMINAL_WORKSPACE = 'storybook-alpha'
export const TERMINAL_OUTPUT = 'storybook-alpha $ '

export function json(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })
}

/** A held request: it never settles, so the pane waits as it would on a slow engine. */
export function hold(): Promise<never> {
  return new Promise<never>(() => {})
}

export function status(overrides: Record<string, unknown> = {}) {
  return {
    available: true,
    reason: '',
    platform: 'storybook',
    host: 'storybook-host',
    unlock_expires_in: 600,
    detached_ttl_seconds: 300,
    ...overrides,
  }
}

export function session(id: string, folder: string, overrides: Partial<TerminalSessionInfo> = {}): TerminalSessionInfo {
  return { session_id: id, folder, cols: 80, rows: 24, seq: 0, attached: false, exited: false, exit_code: null, ...overrides }
}

export function statusRoute(body: Record<string, unknown> = {}): FixtureRoute {
  return { method: 'GET', path: '/api/terminal/status', respond: () => json(status(body)) }
}

/** Answers the first status read and holds every later one (a reconnect that never lands). */
export function statusOnceThenHold(): FixtureRoute {
  let reads = 0
  return {
    method: 'GET',
    path: '/api/terminal/status',
    respond: () => (reads++ === 0 ? json(status()) : hold()),
  }
}

export function sessionsRoute(query: string, sessions: TerminalSessionInfo[], locations?: unknown[]): FixtureRoute {
  return {
    method: 'GET',
    path: `/api/terminal/sessions?${query}`,
    respond: () => json({ sessions, ...(locations ? { locations } : {}) }),
  }
}

export function ready(sessionId: string, folder: string, truncated = false): string {
  return JSON.stringify({ type: 'ready', session_id: sessionId, folder, cols: 80, rows: 24, seq: TERMINAL_OUTPUT.length, replay_start: 0, truncated })
}

export function output(text: string): string {
  return JSON.stringify({ type: 'output', start: 0, end: text.length, data: btoa(text) })
}

export function socketPath(sessionId: string): string {
  return `/ws/terminal/${encodeURIComponent(sessionId)}`
}

/** A shell that comes up, shows its prompt and stays open. */
export function liveShell(sessionId: string, folder: string, truncated = false): FixtureSocketScript {
  return (socket) => {
    socket.serverOpen()
    socket.serverMessage(ready(sessionId, folder, truncated))
    socket.serverMessage(output(TERMINAL_OUTPUT))
  }
}

/** A socket that opens and never sends the handshake frame. */
export function silentShell(): FixtureSocketScript {
  return (socket) => { socket.serverOpen() }
}

/**
 * A shell whose first connection ends with `code` after it came up; every
 * later connection (a takeover or a retry the story allows) stays live.
 */
export function shellEndingOnce(sessionId: string, folder: string, code: number): FixtureSocketScript {
  let connections = 0
  return (socket) => {
    connections += 1
    socket.serverOpen()
    socket.serverMessage(ready(sessionId, folder))
    socket.serverMessage(output(TERMINAL_OUTPUT))
    if (connections === 1) socket.serverClose(code, 'storybook')
  }
}

/** A shell that prints its prompt and then exits with `code`. */
export function exitedShell(sessionId: string, folder: string, code: number): FixtureSocketScript {
  return (socket) => {
    socket.serverOpen()
    socket.serverMessage(ready(sessionId, folder))
    socket.serverMessage(JSON.stringify({ type: 'exit', code }))
  }
}
