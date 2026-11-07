import { ref, watch, type Ref } from 'vue'
import { useTerminalStore } from '../stores/terminal'
import { useProjectStore } from '../stores/projects'
import { writeClipboard } from '../lib/codeCopy'

export type RunInTerminalResult = 'typed' | 'copied' | 'copied_multiline' | 'failed'

const LIVE_TIMEOUT_MS = 8000
// A fresh shell turns bracketed paste on when it draws its first prompt, so the
// paste mode is polled for this long after the shell goes live.
const PASTE_MODE_WAIT_MS = 2000
const PASTE_POLL_MS = 100
const PASTE_START = '\x1b[200~'
const PASTE_END = '\x1b[201~'
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
 * cannot be opened with a command, so this opens it on a project or a chat,
 * makes sure a session exists, waits for the shell to go live and sends the
 * keystrokes. Whenever that is not possible the command is copied instead,
 * never dropped.
 *
 * Paste mode types without pressing Enter: the text is wrapped in a bracketed
 * paste when the shell accepts one, so the shell inserts it at the prompt and
 * runs nothing. A multi-line command is copied instead when the shell does not
 * take a paste, because each of its newlines would run a line.
 */
export function useRunInTerminal(
  liveTimeoutMs: number = LIVE_TIMEOUT_MS,
  pasteModeWaitMs: number = PASTE_MODE_WAIT_MS,
): {
  run: (command: string, opts?: { enter?: boolean; chatId?: string; paste?: boolean }) => Promise<RunInTerminalResult>
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

  /** Resolves true once the shell on screen accepts a paste; false after the wait or if the session changes. */
  function waitBracketedPaste(ownId: string): Promise<boolean> {
    const deadline = Date.now() + pasteModeWaitMs
    return new Promise((resolve) => {
      const check = (): void => {
        if (store.activeSession?.session_id !== ownId) return resolve(false)
        if (store.bracketedPasteMode()) return resolve(true)
        if (Date.now() >= deadline) return resolve(false)
        setTimeout(check, PASTE_POLL_MS)
      }
      check()
    })
  }

  async function run(
    command: string,
    opts?: { enter?: boolean; chatId?: string; paste?: boolean },
  ): Promise<RunInTerminalResult> {
    if (busy.value) return 'failed'
    busy.value = true
    try {
      // A chat gets its own remembered session, apart from the project's.
      const chatId = opts?.chatId
      let key: string
      let target: { chatId: string } | { projectId: string }
      if (chatId) {
        key = `chat:${chatId}`
        target = { chatId }
      } else {
        const projectId = projectStore.activeProject?.project_id
          || projectStore.workspaceProjects[0]?.project_id
          || projectStore.projects[0]?.project_id
        if (!projectId) return await copy(command)
        key = projectId
        target = { projectId }
      }
      await store.open(target)
      if (store.phase === 'locked' || store.phase === 'error') return await copy(command)
      const remembered = ownSessions.get(key)
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
        ownSessions.set(key, ownId)
      }
      if (!(await waitLive(ownId))) return await copy(command)
      // The user may have switched tabs between the last check and now.
      if (store.activeSession?.session_id !== ownId) return await copy(command)
      let text: string
      if (opts?.paste) {
        if (await waitBracketedPaste(ownId)) {
          text = PASTE_START + command.replace(/\r?\n/g, '\r') + PASTE_END
        } else if (/[\r\n]/.test(command)) {
          // Each newline would run a line in a shell without bracketed paste.
          return (await writeClipboard(command)) ? 'copied_multiline' : 'failed'
        } else {
          text = command
        }
        if (store.activeSession?.session_id !== ownId) return await copy(command)
      } else {
        text = command + (opts?.enter === false ? '' : '\r')
      }
      const bytes = new TextEncoder().encode(text)
      return store.sendInput(bytes) ? 'typed' : await copy(command)
    } finally {
      busy.value = false
    }
  }

  return { run, busy }
}
