// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { reactive } from 'vue'
import { useRunInTerminal } from '../useRunInTerminal'

const writeClipboard = vi.hoisted(() => vi.fn())
vi.mock('../../lib/codeCopy', () => ({ writeClipboard }))

const terminal = reactive({
  phase: 'closed' as string,
  activeSession: null as { session_id: string } | null,
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
    projects.activeProject = null
    projects.workspaceProjects = [{ project_id: 'p1' }]
    projects.projects = []
    terminal.open.mockReset().mockImplementation(async () => { terminal.phase = 'ready' })
    terminal.createSession.mockReset().mockImplementation(async () => {
      terminal.activeSession = { session_id: 's1' }
      setTimeout(() => { terminal.phase = 'live' }, 0)
      return true
    })
    terminal.sendInput.mockReset().mockReturnValue(true)
    writeClipboard.mockReset().mockResolvedValue(true)
  })

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

  it('reuses an existing live session without creating one', async () => {
    terminal.open.mockImplementation(async () => { terminal.phase = 'live'; terminal.activeSession = { session_id: 's0' } })
    const { run } = useRunInTerminal(500)
    expect(await run('x')).toBe('typed')
    expect(terminal.createSession).not.toHaveBeenCalled()
  })

  it('copies when the terminal is locked', async () => {
    terminal.open.mockImplementation(async () => { terminal.phase = 'locked' })
    const { run } = useRunInTerminal(500)
    expect(await run('claude mcp list')).toBe('copied')
    expect(writeClipboard).toHaveBeenCalledWith('claude mcp list')
    expect(terminal.sendInput).not.toHaveBeenCalled()
  })

  it('copies when the shell never goes live', async () => {
    terminal.createSession.mockImplementation(async () => { terminal.activeSession = { session_id: 's1' }; return true })
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
