import { ref, watch, type Ref } from 'vue'
import { useTerminalStore } from '../stores/terminal'
import { useProjectStore } from '../stores/projects'
import { writeClipboard } from '../lib/codeCopy'

export type RunInTerminalResult = 'typed' | 'copied' | 'failed'

const LIVE_TIMEOUT_MS = 8000
// The session this composable created for each project scope, kept across remounts. A command is only
// ever typed into this one, never into a shell the user may have left running
// ssh, a REPL or a TUI in; it is reused while it lives so repeated runs do not
// burn the terminal's per-scope session cap.
const ownSessions = new Map<string, string>()

const DEAD_PHASES = new Set<string>(['error', 'locked', 'ended', 'takeover'])

/** Test hook: forget the remembered session. */
export function resetRunInTerminalSession(): void { ownSessions.clear() }

/**
 * Type a command into the in-app terminal the user is watching. The terminal
 * cannot be opened with a command, so this opens it on a project, makes sure
 * a session exists, waits for the shell to go live and sends the keystrokes.
 * Whenever that is not possible the command is copied instead, never dropped.
 */
export function useRunInTerminal(liveTimeoutMs: number = LIVE_TIMEOUT_MS): {
  run: (command: string, opts?: { enter?: boolean }) => Promise<RunInTerminalResult>
  busy: Ref<boolean>
} {
  const store = useTerminalStore()
  const projectStore = useProjectStore()
  const busy = ref(false)

  async function copy(command: string): Promise<RunInTerminalResult> {
    return (await writeClipboard(command)) ? 'copied' : 'failed'
  }

  /** Resolves true once our session is live; false if it dies or is no longer the active one. */
  function waitLive(ownId: string): Promise<boolean> {
    if (store.activeSession?.session_id !== ownId) return Promise.resolve(false)
    if (store.phase === 'live') return Promise.resolve(true)
    if (DEAD_PHASES.has(store.phase)) return Promise.resolve(false)
    return new Promise((resolve) => {
      let stop = (): void => {}
      const settle = (ok: boolean): void => { clearTimeout(timer); stop(); resolve(ok) }
      const timer = setTimeout(() => settle(false), liveTimeoutMs)
      stop = watch(() => [store.phase, store.activeSession?.session_id] as const, ([phase, activeId]) => {
        if (activeId !== ownId || DEAD_PHASES.has(phase)) settle(false)
        else if (phase === 'live') settle(true)
      })
    })
  }

  async function run(command: string, opts?: { enter?: boolean }): Promise<RunInTerminalResult> {
    if (busy.value) return 'failed'
    busy.value = true
    try {
      const projectId = projectStore.activeProject?.project_id
        || projectStore.workspaceProjects[0]?.project_id
        || projectStore.projects[0]?.project_id
      if (!projectId) return await copy(command)
      await store.open({ projectId })
      if (store.phase === 'locked' || store.phase === 'error') return await copy(command)
      const remembered = ownSessions.get(projectId)
      const own = remembered ? store.sessions.find(entry => entry.session_id === remembered) : undefined
      let ownId: string
      if (own && !own.exited) {
        // sendInput writes to the active session's socket, so make ours active.
        if (!store.selectSession(own.session_id)) return await copy(command)
        ownId = own.session_id
      } else {
        const before = new Set(store.sessions.map(entry => entry.session_id))
        if (!(await store.createSession())) return await copy(command)
        const created = store.sessions.find(entry => !before.has(entry.session_id))
        if (!created || store.activeSession?.session_id !== created.session_id) return await copy(command)
        ownId = created.session_id
        ownSessions.set(projectId, ownId)
      }
      if (!(await waitLive(ownId))) return await copy(command)
      // The user may have switched tabs between the last check and now.
      if (store.activeSession?.session_id !== ownId) return await copy(command)
      const bytes = new TextEncoder().encode(command + (opts?.enter === false ? '' : '\r'))
      return store.sendInput(bytes) ? 'typed' : await copy(command)
    } finally {
      busy.value = false
    }
  }

  return { run, busy }
}
