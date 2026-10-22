// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import { flushPromises, mount, type DOMWrapper } from '@vue/test-utils'
import SettingsMcpServers from '../settings/SettingsMcpServers.vue'
import { useMcpServers } from '../../composables/useMcpServers'
import type { McpStatus } from '../../lib/types'

const runMock = vi.hoisted(() => vi.fn())
const writeClipboard = vi.hoisted(() => vi.fn())
vi.mock('../../composables/useRunInTerminal', () => ({ useRunInTerminal: () => ({ run: runMock, busy: ref(false) }) }))
vi.mock('../../lib/codeCopy', () => ({ writeClipboard }))

const PREFIX = 'cd -- /workspace/personal && '

function panel(change?: (status: McpStatus) => void, props: { defaultProvider?: string } = {}) {
  const status: McpStatus = { workspace: 'personal', agent_root: '/workspace/personal', shell: { kind: 'posix', cd_prefix: PREFIX }, providers: [
    { provider: 'claude', available: true, inventory_state: 'loaded', servers: [
      { name: 'notion', scope: 'project', source: '.mcp.json', allowed: false, connections: [] },
    ] },
    { provider: 'opencode', available: true, inventory_state: 'loaded', servers: [
      { name: 'notion', scope: 'project', source: 'opencode.json', allowed: null, connections: [{ chat_id: 'chat-one', state: 'failed', error: 'MCP server connection failed.' }, { chat_id: 'chat-two', state: 'needs_auth' }] },
    ] },
  ] }
  change?.(status)
  const get = vi.fn().mockResolvedValue(status)
  const mcp = useMcpServers({ api: { get } })
  mcp.status.value = status
  return { wrapper: mount(SettingsMcpServers, { props: { mcp, ...props }, attachTo: document.body }), get }
}

const tab = (wrapper: ReturnType<typeof panel>['wrapper'], label: string) => wrapper.findAll('[role="tab"]').find(t => t.text().startsWith(label))!
const row = (wrapper: ReturnType<typeof panel>['wrapper'], key: string) => wrapper.get(`.mcp-commands li[data-row="${key}"]`)
const button = (scope: Pick<DOMWrapper<Element>, 'findAll'>, text: string) => scope.findAll('button').find(b => b.text() === text)!

describe('Settings MCP native inventory', () => {
  it('shows providers and live connections without inferring readiness', async () => {
    const { wrapper } = panel()
    expect(wrapper.findAll('h3').map(row => row.text())).toEqual(['Easiest: ask Ciao in a chat'])
    expect(wrapper.get('.mcp-workspace-path').text()).toBe('/workspace/personal')
    const tabs = wrapper.findAll('[role="tab"]').map(t => t.text())
    expect(tabs[0]).toContain('Claude Code')
    expect(tabs[1]).toContain('OpenCode')
    expect(wrapper.text()).toContain('blocked in workspace')
    expect(wrapper.text()).toContain('Connection not checked.')
    await tab(wrapper, 'OpenCode').trigger('click')
    expect(wrapper.text()).toContain('connection failed')
    expect(wrapper.text()).toContain('sign-in required')
    expect(wrapper.findAll('.badge--success')).toHaveLength(0)
    expect(wrapper.findAll('.mcp-connection a').map(link => link.attributes('href'))).toEqual(['/chat/chat-one', '/chat/chat-two'])
    wrapper.unmount()
  })
  it('distinguishes unreadable configuration from empty inventory', async () => {
    const { wrapper } = panel(status => {
      status.providers[0]!.inventory_state = 'unknown'
      status.providers[0]!.servers = []
      status.providers[1]!.servers = []
      status.providers[1]!.available = false
    })
    expect(wrapper.text()).toContain('Some CLI configuration could not be read')
    await tab(wrapper, 'OpenCode').trigger('click')
    expect(wrapper.text()).toContain('No MCP servers configured')
    expect(wrapper.text()).toContain('not installed')
    wrapper.unmount()
  })
  it('refreshes the read-only inventory', async () => {
    const { wrapper, get } = panel()
    await wrapper.get('button').trigger('click')
    expect(get).toHaveBeenCalledWith('/api/mcp/status')
    wrapper.unmount()
  })
  it('puts the Ask Ciao box first and emits ask-ciao', async () => {
    const { wrapper } = panel()
    expect(wrapper.get('.mcp-ask').text()).toContain('Easiest: ask Ciao in a chat')
    expect(wrapper.get('.mcp-ask').element.compareDocumentPosition(wrapper.get('[role="tablist"]').element) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    await button(wrapper, 'Ask Ciao to add one').trigger('click')
    expect(wrapper.emitted('ask-ciao')).toHaveLength(1)
    wrapper.unmount()
  })
  it("selects the default provider's tab first", () => {
    const { wrapper } = panel(undefined, { defaultProvider: 'opencode' })
    const first = wrapper.findAll('[role="tab"]')[0]!
    expect(first.text()).toContain('OpenCode')
    expect(first.attributes('aria-selected')).toBe('true')
    expect(wrapper.get('[role="tabpanel"]').attributes('aria-labelledby')).toBe(first.attributes('id'))
    wrapper.unmount()
  })
  it('tabs are keyboard navigable', async () => {
    const { wrapper } = panel()
    const [first, second] = wrapper.findAll('[role="tab"]')
    expect(first!.attributes('aria-selected')).toBe('true')
    await first!.trigger('keydown', { key: 'ArrowRight' })
    await flushPromises()
    expect(second!.attributes('aria-selected')).toBe('true')
    expect(first!.attributes('aria-selected')).toBe('false')
    expect(wrapper.findAll('[role="tabpanel"]')).toHaveLength(1)
    wrapper.unmount()
  })
  it('builds the add command live and quotes it', async () => {
    const { wrapper } = panel()
    const add = row(wrapper, 'add')
    expect(add.get('code').text()).toContain('<name>')
    expect(add.findAll('button').every(b => b.attributes('disabled') !== undefined)).toBe(true)
    await wrapper.get('#mcp-server-name').setValue('notion')
    await wrapper.get('#mcp-server-url').setValue('https://example.com/mcp')
    expect(add.get('code').text()).toBe(`${PREFIX}claude mcp add --scope local --transport http notion https://example.com/mcp`)
    expect(add.findAll('button').every(b => b.attributes('disabled') === undefined)).toBe(true)
    await wrapper.get('#mcp-server-url').setValue('ftp://nope')
    expect(wrapper.find('[role="alert"]').exists()).toBe(true)
    expect(add.findAll('button').every(b => b.attributes('disabled') !== undefined)).toBe(true)
    await wrapper.get('#mcp-server-url').setValue('https://example.com/mcp')
    await wrapper.get('#mcp-server-name').setValue('a b')
    expect(wrapper.find('[role="alert"]').exists()).toBe(true)
    expect(add.findAll('button').every(b => b.attributes('disabled') !== undefined)).toBe(true)
    wrapper.unmount()
  })
  it('shows fixed rows per CLI', async () => {
    const { wrapper } = panel()
    expect(wrapper.findAll('.mcp-commands li').map(li => li.attributes('data-row'))).toEqual(['add', 'list', 'login', 'remove'])
    await tab(wrapper, 'OpenCode').trigger('click')
    expect(wrapper.findAll('.mcp-commands li').map(li => li.attributes('data-row'))).toEqual(['add', 'list', 'login'])
    expect(row(wrapper, 'login').get('code').text()).toContain('opencode mcp auth')
    expect(wrapper.text()).toContain('OpenCode has no remove command')
    wrapper.unmount()
  })
  it('Run in terminal types the command', async () => {
    runMock.mockReset().mockResolvedValue('typed')
    const { wrapper } = panel()
    await button(row(wrapper, 'list'), 'Run in terminal').trigger('click')
    expect(runMock).toHaveBeenCalledWith(`${PREFIX}claude mcp list`, undefined)
    await wrapper.get('#mcp-server-name').setValue('notion')
    await button(row(wrapper, 'remove'), 'Type in terminal').trigger('click')
    expect(runMock).toHaveBeenLastCalledWith(`${PREFIX}claude mcp remove notion`, { enter: false })
    await flushPromises()
    expect(wrapper.text()).toContain('Typed in a separate Ciao terminal session below')
    runMock.mockResolvedValue('copied')
    await button(row(wrapper, 'list'), 'Run in terminal').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Copied. Paste it into the terminal.')
    wrapper.unmount()
  })
  it('shows sign in for every server and emphasises needs_auth', async () => {
    runMock.mockReset().mockResolvedValue('typed')
    const { wrapper } = panel()
    const quiet = button(wrapper, 'Sign in in terminal')
    expect(quiet.classes()).toContain('btn-small')
    await quiet.trigger('click')
    expect(runMock).toHaveBeenLastCalledWith(`${PREFIX}claude mcp login notion`, undefined)
    await tab(wrapper, 'OpenCode').trigger('click')
    const loud = button(wrapper, 'Sign in in terminal')
    expect(loud.classes()).toContain('btn-primary')
    expect(wrapper.text()).toContain('Sign-in required')
    await loud.trigger('click')
    expect(runMock).toHaveBeenLastCalledWith(`${PREFIX}opencode mcp auth notion`, undefined)
    wrapper.unmount()
  })
  it('copies the remove command from the more menu', async () => {
    writeClipboard.mockReset().mockResolvedValue(true)
    const { wrapper } = panel()
    const more = wrapper.get('[aria-label="More actions for notion"]')
    await more.trigger('click')
    const items = wrapper.findAll('[role="menuitem"]')
    expect(items.map(i => i.text())).toEqual(['Sign in in terminal', 'Copy remove command'])
    await items[1]!.trigger('click')
    await flushPromises()
    expect(writeClipboard).toHaveBeenCalledWith(`${PREFIX}claude mcp remove notion`)
    expect(wrapper.findAll('[role="status"]').map(s => s.text())).toContain('Copied')
    await more.trigger('click')
    expect(wrapper.find('[role="menu"]').exists()).toBe(true)
    await wrapper.get('.mcp-more').trigger('keydown', { key: 'Escape' })
    expect(wrapper.find('[role="menu"]').exists()).toBe(false)
    expect(document.activeElement).toBe(more.element)
    await tab(wrapper, 'OpenCode').trigger('click')
    await wrapper.get('[aria-label="More actions for notion"]').trigger('click')
    expect(wrapper.findAll('[role="menuitem"]').map(i => i.text())).toEqual(['Sign in in terminal', 'Copy sign-out command'])
    wrapper.unmount()
  })
  it('keeps the reload note', () => {
    const { wrapper } = panel()
    expect(wrapper.text()).toContain('ciao chat reload')
    wrapper.unmount()
  })
  it('shows the failure message with the command when neither terminal nor clipboard works', async () => {
    runMock.mockReset().mockResolvedValue('failed')
    const { wrapper } = panel()
    await button(row(wrapper, 'list'), 'Run in terminal').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain(`Could not reach the terminal or the clipboard. Run this yourself: ${PREFIX}claude mcp list`)
    wrapper.unmount()
  })
  it('hides the workspace line when there is no agent root', () => {
    const { wrapper } = panel(status => { status.agent_root = '' })
    expect(wrapper.find('.mcp-workspace-path').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('Commands run in this workspace folder')
    wrapper.unmount()
  })
  it('renders no workspace line or controls when the status failed to load', () => {
    const mcp = useMcpServers({ api: { get: vi.fn() } })
    const wrapper = mount(SettingsMcpServers, { props: { mcp } })
    expect(wrapper.text()).not.toContain('Commands run in this workspace folder')
    expect(wrapper.find('.mcp-workspace-path').exists()).toBe(false)
    wrapper.unmount()
  })
  it('shows the sign-in button only when sign-in may be needed', async () => {
    const { wrapper } = panel(status => {
      status.providers[0]!.servers[0]!.connections = [{ chat_id: 'c', state: 'connected' }]
    })
    expect(wrapper.findAll('.mcp-signin')).toHaveLength(0)
    expect(wrapper.text()).not.toContain('Sign-in can only be detected')
    await wrapper.get('[aria-label="More actions for notion"]').trigger('click')
    expect(wrapper.findAll('[role="menuitem"]')[0]!.text()).toBe('Sign in in terminal')
    wrapper.unmount()
  })
  it('explains servers whose names cannot be used in a command', () => {
    const { wrapper } = panel(status => {
      status.providers[0]!.servers = [{ name: 'claude.ai Gmail', scope: 'account', source: 'provider', allowed: null, connections: [] }]
    })
    expect(wrapper.text()).toContain('This server can’t be managed with a command here; use Claude Code directly.')
    expect(wrapper.findAll('.mcp-signin')).toHaveLength(0)
    expect(wrapper.find('[aria-label="More actions for claude.ai Gmail"]').exists()).toBe(false)
    wrapper.unmount()
  })
  it('explains a url the Windows terminal cannot pass safely', async () => {
    const { wrapper } = panel(status => { status.shell = { kind: 'powershell', cd_prefix: "Set-Location -LiteralPath 'C:\\w'; " } })
    await wrapper.get('#mcp-server-name').setValue('notion')
    await wrapper.get('#mcp-server-url').setValue('https://example.com/a&b')
    expect(wrapper.get('[role="alert"]').text()).toContain("This URL has characters the Windows terminal can't pass safely. Type the add command yourself in a terminal.")
    expect(row(wrapper, 'add').findAll('button').every(b => b.attributes('disabled') !== undefined)).toBe(true)
    wrapper.unmount()
  })
  it('closes the menu when focus moves outside it', async () => {
    const { wrapper } = panel()
    await wrapper.get('[aria-label="More actions for notion"]').trigger('click')
    expect(wrapper.find('[role="menu"]').exists()).toBe(true)
    await wrapper.get('.mcp-more').trigger('focusout', { relatedTarget: wrapper.get('#mcp-server-name').element })
    expect(wrapper.find('[role="menu"]').exists()).toBe(false)
    wrapper.unmount()
  })
})
