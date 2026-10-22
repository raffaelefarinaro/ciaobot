// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { reactive } from 'vue'
import { resetRunInTerminalSession, useRunInTerminal } from '../useRunInTerminal'

const writeClipboard = vi.hoisted(() => vi.fn())
vi.mock('../../lib/codeCopy', () => ({ writeClipboard }))

const terminal = reactive({
  phase: 'closed' as string,
  activeSession: null as { session_id: string; exited?: boolean } | null,
  sessions: [] as { session_id: string; exited: boolean }[],
  selectSession: vi.fn(),
  open: vi.fn(),
  createSession: vi.fn(),
  sendInput: vi.fn(),
})
const projects = reactive({
  activeProject: null as { project_id: string } | null,
  workspaceProjects: [{ project_id: 'p1' }] as { project_id: string }[],
  projects: [] as { project_id: string }[],
})
vi.mock('../../stores/terminal', () => ({ useTerminalStore: () => terminal }))
vi.mock('../../stores/projects', () => ({ useProjectStore: () => projects }))

const decode = (bytes: Uint8Array) => new TextDecoder().decode(bytes)

describe('useRunInTerminal', () => {
  beforeEach(() => {
    terminal.phase = 'closed'
    terminal.activeSession = null
    terminal.sessions = []
    resetRunInTerminalSession()
    terminal.selectSession.mockReset().mockImplementation((id: string) => {
      const found = terminal.sessions.find(e => e.session_id === id)
      if (!found) return false
      terminal.activeSession = found
      terminal.phase = 'live'
      return true
    })
    projects.activeProject = null
    projects.workspaceProjects = [{ project_id: 'p1' }]
    projects.projects = []
    terminal.open.mockReset().mockImplementation(async () => { terminal.phase = 'ready' })
    terminal.createSession.mockReset().mockImplementation(async () => {
      const created = { session_id: `s${terminal.sessions.length + 1}`, exited: false }
      terminal.sessions = [...terminal.sessions, created]
      terminal.activeSession = created
      setTimeout(() => { terminal.phase = 'live' }, 0)
      return true
    })
    terminal.sendInput.mockReset().mockReturnValue(true)
    writeClipboard.mockReset().mockResolvedValue(true)
  })

  afterEach(async () => { await new Promise(resolve => setTimeout(resolve, 5)) })

  it('types the command with Enter once the shell is live', async () => {
    const { run, busy } = useRunInTerminal(500)
    const result = await run('claude mcp list')
    expect(result).toBe('typed')
    expect(terminal.open).toHaveBeenCalledWith({ projectId: 'p1' })
    expect(terminal.sendInput).toHaveBeenCalledTimes(1)
    expect(decode(terminal.sendInput.mock.calls[0]![0])).toBe('claude mcp list\r')
    expect(writeClipboard).not.toHaveBeenCalled()
    expect(busy.value).toBe(false)
  })

  it('omits Enter when asked', async () => {
    const { run } = useRunInTerminal(500)
    await run('claude mcp remove notion', { enter: false })
    expect(decode(terminal.sendInput.mock.calls[0]![0])).toBe('claude mcp remove notion')
  })

  it('never types into a session it did not create', async () => {
    const foreign = { session_id: 'foreign', exited: false }
    terminal.sessions = [foreign]
    terminal.open.mockImplementation(async () => { terminal.phase = 'live'; terminal.activeSession = foreign })
    const { run } = useRunInTerminal(500)
    expect(await run('x')).toBe('typed')
    expect(terminal.createSession).toHaveBeenCalledTimes(1)
    expect(terminal.activeSession?.session_id).not.toBe('foreign')
    expect(terminal.selectSession).not.toHaveBeenCalledWith('foreign')
  })

  it('reuses its own session for a second run', async () => {
    const { run } = useRunInTerminal(500)
    expect(await run('one')).toBe('typed')
    terminal.open.mockImplementation(async () => { terminal.phase = 'live'; terminal.activeSession = { session_id: 'foreign' } })
    expect(await run('two')).toBe('typed')
    expect(terminal.createSession).toHaveBeenCalledTimes(1)
    expect(terminal.selectSession).toHaveBeenCalledWith('s1')
    expect(terminal.activeSession?.session_id).toBe('s1')
    expect(terminal.sendInput).toHaveBeenCalledTimes(2)
  })

  it('creates a new session when its own session has ended', async () => {
    const { run } = useRunInTerminal(500)
    await run('one')
    terminal.sessions = terminal.sessions.map(e => ({ ...e, exited: true }))
    terminal.phase = 'ready'
    expect(await run('two')).toBe('typed')
    expect(terminal.createSession).toHaveBeenCalledTimes(2)
    expect(terminal.activeSession?.session_id).toBe('s2')
  })

  it('copies when a new session cannot be created', async () => {
    terminal.createSession.mockResolvedValue(false)
    const { run } = useRunInTerminal(500)
    expect(await run('cmd')).toBe('copied')
    expect(terminal.sendInput).not.toHaveBeenCalled()
  })

  it.each(['error', 'locked', 'ended', 'takeover'])('copies at once when the shell goes %s while waiting', async (phase) => {
    terminal.createSession.mockImplementation(async () => {
      const created = { session_id: 's1', exited: false }
      terminal.sessions = [created]
      terminal.activeSession = created
      terminal.phase = 'connecting'
      setTimeout(() => { terminal.phase = phase }, 0)
      return true
    })
    const started = Date.now()
    const { run } = useRunInTerminal(5000)
    expect(await run('cmd')).toBe('copied')
    expect(Date.now() - started).toBeLessThan(1000)
    expect(writeClipboard).toHaveBeenCalledWith('cmd')
  })

  it('reports failed when the clipboard also fails', async () => {
    terminal.open.mockImplementation(async () => { terminal.phase = 'locked' })
    writeClipboard.mockResolvedValue(false)
    const { run } = useRunInTerminal(500)
    expect(await run('cmd')).toBe('failed')
  })

  it('copies when the terminal is locked', async () => {
    terminal.open.mockImplementation(async () => { terminal.phase = 'locked' })
    const { run } = useRunInTerminal(500)
    expect(await run('claude mcp list')).toBe('copied')
    expect(writeClipboard).toHaveBeenCalledWith('claude mcp list')
    expect(terminal.sendInput).not.toHaveBeenCalled()
  })

  it('copies when the shell never goes live', async () => {
    terminal.createSession.mockImplementation(async () => { const c = { session_id: 's1', exited: false }; terminal.sessions = [c]; terminal.activeSession = c; return true })
    const { run } = useRunInTerminal(10)
    expect(await run('cmd')).toBe('copied')
    expect(writeClipboard).toHaveBeenCalledWith('cmd')
    expect(terminal.sendInput).not.toHaveBeenCalled()
  })

  it('copies when there is no project', async () => {
    projects.workspaceProjects = []
    const { run } = useRunInTerminal(500)
    expect(await run('cmd')).toBe('copied')
    expect(terminal.open).not.toHaveBeenCalled()
    expect(writeClipboard).toHaveBeenCalledWith('cmd')
  })

  it('refuses a second call while busy', async () => {
    const { run } = useRunInTerminal(500)
    const first = run('one')
    expect(await run('two')).toBe('failed')
    expect(await first).toBe('typed')
    expect(terminal.sendInput).toHaveBeenCalledTimes(1)
  })
})
