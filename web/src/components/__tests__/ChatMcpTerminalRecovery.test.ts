// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { reactive, nextTick } from 'vue'
import ChatMcpTerminalRecovery from '../ChatMcpTerminalRecovery.vue'
import source from '../ChatMcpTerminalRecovery.vue?raw'
import type { McpNativeServer, McpStatus } from '../../lib/types'
const clipboard = vi.hoisted(() => vi.fn())
vi.mock('../../lib/codeCopy', () => ({ writeClipboard: clipboard }))
const server = (over: Partial<McpNativeServer> = {}): McpNativeServer => ({ name: 'linear', scope: 'project', source: 'settings', kind: 'http', allowed: true, connections: [], ...over })
const status = (): McpStatus => ({ workspace: 'work', agent_root: '/work', providers: [], shell: { kind: 'posix', cd_prefix: "cd '/work/it'\\''s' && " } })
const props = () => ({ chatId: 'a', provider: 'claude' as const, server: server(), status: status() })
afterEach(() => { clipboard.mockReset(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
describe('terminal recovery', () => {
  it('eligibility matrix', () => {
    for (const provider of ['claude', 'opencode'] as const) {
      for (const bad of [{ enabled: false }, { allowed: false }, { scope: 'claudeai' }, { name: 'bad;touch' }, { name: 'bad\nname' }]) {
        const w = mount(ChatMcpTerminalRecovery, { props: { ...props(), provider, status: status(), server: server(bad) } })
        expect(w.find('details').exists()).toBe(false); w.unmount()
      }
      for (const allowed of [true, null]) for (const enabled of [true, undefined]) for (const kind of ['http', 'sse', 'remote', 'stdio', 'local', 'unknown', undefined]) {
        const w = mount(ChatMcpTerminalRecovery, { props: { ...props(), provider, status: status(), server: server({ allowed, enabled, kind }) } })
        expect(w.get('details').attributes('open')).toBeUndefined()
        expect(w.findAll('code')).toHaveLength(['http', 'sse', 'remote'].includes(kind ?? '') ? 2 : 1); w.unmount()
      }
      const noShell = mount(ChatMcpTerminalRecovery, { props: { ...props(), provider, status: { ...status(), shell: undefined } as never } })
      expect(noShell.find('details').exists()).toBe(false); noShell.unmount()
      const noServer = mount(ChatMcpTerminalRecovery, { props: { ...props(), provider, server: undefined as never } })
      expect(noServer.find('details').exists()).toBe(false); noServer.unmount()
    }
  })
  it('copy has no terminal effects and copies exact displayed command', async () => {
    const fetch = vi.fn(); vi.stubGlobal('fetch', fetch)
    const open = vi.spyOn(window, 'open'); clipboard.mockResolvedValue(true)
    for (const provider of ['claude', 'opencode'] as const) {
      const w = mount(ChatMcpTerminalRecovery, { props: { ...props(), provider, status: status() } })
      const commands = w.findAll('code').map(e => e.text())
      expect(commands[0]).toBe(status().shell.cd_prefix + `${provider} mcp list`)
      expect(commands[1]).toBe(status().shell.cd_prefix + (provider === 'claude' ? 'claude mcp login linear' : 'opencode mcp auth linear'))
      await w.findAll('button')[0]!.trigger('click'); await flushPromises()
      expect(clipboard).toHaveBeenLastCalledWith(commands[0], expect.any(Function))
      await w.findAll('button')[1]!.trigger('click'); await flushPromises()
      expect(clipboard).toHaveBeenLastCalledWith(commands[1], expect.any(Function))
      expect(w.get('[role=status]').text()).toBe('Copied. Open Chat details → Open → Terminal, choose Workspace if offered, review and paste it yourself.')
      expect(w.text()).toContain('run'); w.unmount()
    }
    expect(fetch).not.toHaveBeenCalled(); expect(open).not.toHaveBeenCalled()
    expect(source).not.toMatch(/stores\/terminal|useRunInTerminal|sendInput|createSession|selectSession|terminal\.open|lib\/api|location\.reload|v-html/)
  })
  it.each([false, 'reject'])('preserves preview and offers hand copy on clipboard failure %s', async failure => {
    clipboard.mockImplementation(() => failure === 'reject' ? Promise.reject(new Error('private')) : Promise.resolve(false))
    const w = mount(ChatMcpTerminalRecovery, { props: props() })
    await w.get('button').trigger('click'); await flushPromises()
    expect(w.get('code').text()).toContain('claude mcp list')
    expect(w.get('[role=status]').text()).toContain('copy it by hand')
    expect(w.text()).not.toContain('private'); w.unmount()
  })
  it('late clipboard completion cannot act on a new context', async () => {
    let resolve!: (v: boolean) => void
    clipboard.mockImplementation(() => new Promise<boolean>(r => { resolve = r }))
    const w = mount(ChatMcpTerminalRecovery, { props: props() })
    const old = w.get('button').element as HTMLButtonElement
    old.click(); old.click(); expect(clipboard).toHaveBeenCalledTimes(1)
    await w.setProps({ chatId: 'b', server: server({ name: 'other' }), status: { ...status(), workspace: 'other', shell: { kind: 'posix', cd_prefix: 'cd /other && ' } } })
    resolve(true); await flushPromises(); expect(w.text()).not.toContain('Copied.')
    expect(clipboard).toHaveBeenCalledTimes(1); w.unmount()
    const w2 = mount(ChatMcpTerminalRecovery, { props: props() })
    ;(w2.get('button').element as HTMLButtonElement).click(); w2.unmount(); resolve(true); await flushPromises()
    expect(clipboard).toHaveBeenCalledTimes(2)
  })
  it('does not copy a changed command from a stale rendered button', async () => {
    clipboard.mockResolvedValue(true)
    const current = reactive(status())
    const w = mount(ChatMcpTerminalRecovery, { props: { ...props(), status: current } })
    const button = w.get('button').element as HTMLButtonElement
    current.shell.cd_prefix = 'cd /changed && '
    button.click(); await nextTick(); await flushPromises()
    expect(clipboard).not.toHaveBeenCalled(); w.unmount()
  })
  it('refuses a stale button when eligibility changes before rendering', async () => {
    const current = reactive(server())
    const w = mount(ChatMcpTerminalRecovery, { props: { ...props(), server: current } })
    const button = w.get('button').element as HTMLButtonElement
    current.allowed = false
    button.click(); await nextTick()
    expect(clipboard).not.toHaveBeenCalled()
    expect(w.find('details').exists()).toBe(false); w.unmount()
  })
  it('hides recovery when the engine still reports powershell', () => {
    const w = mount(ChatMcpTerminalRecovery, { props: { ...props(), status: { ...status(), shell: { kind: 'powershell', cd_prefix: 'x' } } as never } })
    expect(w.find('details').exists()).toBe(false)
    w.unmount()
  })
})
