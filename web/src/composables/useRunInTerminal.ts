import { ref, watch, type Ref } from 'vue'
import { useTerminalStore } from '../stores/terminal'
import { useProjectStore } from '../stores/projects'
import { writeClipboard } from '../lib/codeCopy'

export type RunInTerminalResult = 'typed' | 'copied' | 'failed'

const LIVE_TIMEOUT_MS = 8000

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

  function waitLive(): Promise<boolean> {
    if (store.phase === 'live') return Promise.resolve(true)
    return new Promise((resolve) => {
      let stop = (): void => {}
      const timer = setTimeout(() => { stop(); resolve(false) }, liveTimeoutMs)
      stop = watch(() => store.phase, (phase) => {
        if (phase === 'live') { clearTimeout(timer); stop(); resolve(true) }
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
      if (!store.activeSession && !(await store.createSession())) return await copy(command)
      if (!(await waitLive())) return await copy(command)
      const bytes = new TextEncoder().encode(command + (opts?.enter === false ? '' : '\r'))
      return store.sendInput(bytes) ? 'typed' : await copy(command)
    } finally {
      busy.value = false
    }
  }

  return { run, busy }
}
