// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount, RouterLinkStub, type DOMWrapper } from '@vue/test-utils'
import SettingsMcpServers from '../settings/SettingsMcpServers.vue'
import { useMcpServers, type McpApiClient } from '../../composables/useMcpServers'
import type { McpStatus } from '../../lib/types'

const runMock = vi.hoisted(() => vi.fn())
const writeClipboard = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
const routerPush = vi.hoisted(() => vi.fn(async () => {}))
vi.mock('../../composables/useRunInTerminal', () => ({ useRunInTerminal: () => ({ run: runMock, busy: ref(false) }) }))
vi.mock('../../lib/codeCopy', () => ({ writeClipboard }))
vi.mock('../../lib/api', () => ({ api: { get: vi.fn(), post: apiPost, patch: vi.fn(), del: vi.fn(), postForm: vi.fn() } }))
vi.mock('../../router', () => ({ router: { push: routerPush } }))

beforeEach(() => {
  setActivePinia(createPinia())
  apiPost.mockReset()
  routerPush.mockClear()
})

const PREFIX = 'cd -- /workspace/personal && '

function panel(change?: (status: McpStatus) => void, props: { defaultProvider?: string } = {}, post?: ReturnType<typeof vi.fn>) {
  const status: McpStatus = { workspace: 'personal', agent_root: '/workspace/personal', shell: { kind: 'posix', cd_prefix: PREFIX }, providers: [
    { provider: 'claude', available: true, inventory_state: 'loaded', servers: [
      { name: 'notion', scope: 'project', source: '.mcp.json', allowed: false, applies_to: '/workspace', connections: [] },
    ] },
    { provider: 'opencode', available: true, inventory_state: 'loaded', servers: [
      { name: 'notion', scope: 'project', source: 'opencode.json', allowed: null, applies_to: '/workspace/personal/opencode.json', connections: [{ chat_id: 'chat-one', state: 'failed', error: 'MCP server connection failed.' }, { chat_id: 'chat-two', state: 'needs_auth' }] },
    ] },
  ] }
  change?.(status)
  const get = vi.fn().mockResolvedValue(status)
  const mcp = useMcpServers({ api: post ? { get, post } as McpApiClient : { get } })
  mcp.status.value = status
  return { wrapper: mount(SettingsMcpServers, { props: { mcp, ...props }, attachTo: document.body, global: { stubs: { RouterLink: RouterLinkStub } } }), get, post }
}

const tab = (wrapper: ReturnType<typeof panel>['wrapper'], label: string) => wrapper.findAll('[role="tab"]').find(t => t.text().startsWith(label))!
const row = (wrapper: ReturnType<typeof panel>['wrapper'], key: string) => wrapper.get(`.mcp-commands li[data-row="${key}"]`)
const button = (scope: Pick<DOMWrapper<Element>, 'findAll'>, text: string) => scope.findAll('button').find(b => b.text() === text)!

describe('Settings MCP native inventory', () => {
  it('shows providers and live connections without inferring readiness', async () => {
    const { wrapper } = panel()
    expect(wrapper.findAll('h3')).toHaveLength(0)
    expect(wrapper.get('.mcp-workspace-path').text()).toBe('/workspace/personal')
    const tabs = wrapper.findAll('[role="tab"]').map(t => t.text())
    expect(tabs[0]).toContain('Claude Code')
    expect(tabs[1]).toContain('OpenCode')
    expect(wrapper.text()).toContain('blocked in workspace')
    expect(wrapper.get('.mcp-unchecked').text()).toBe('not checked')
    expect(wrapper.text()).toContain('Connection status comes from open chats.')
    await tab(wrapper, 'OpenCode').trigger('click')
    expect(wrapper.text()).toContain('connection failed')
    expect(wrapper.text()).toContain('sign-in required')
    expect(wrapper.findAll('.badge--success')).toHaveLength(0)
    expect(wrapper.findAll('.mcp-connection a').map(link => link.attributes('href'))).toEqual(['/chat/chat-one', '/chat/chat-two'])
    wrapper.unmount()
  })
  it('shows one status row per state across open chats', async () => {
    const { wrapper } = panel(status => {
      status.providers[0]!.servers = [{ name: 'session-only', scope: 'account', source: 'provider', allowed: null, connections: ['a', 'b', 'c', 'd'].map(id => ({ chat_id: `chat-${id}`, state: 'needs_auth' as const, error: 'MCP server needs authentication.' })) }]
    })
    expect(wrapper.findAll('.mcp-connection')).toHaveLength(1)
    expect(wrapper.get('.mcp-connection').text()).toContain('sign-in required')
    expect(wrapper.get('.mcp-connection').text()).toContain('in 4 chats')
    expect(wrapper.text().split('MCP server needs authentication.')).toHaveLength(2)
    expect(wrapper.findAll('.mcp-connection a').map(link => link.attributes('href'))).toEqual(['/chat/chat-a'])
    wrapper.unmount()
  })
  it('uses grammatical provider chat instructions', async () => {
    const { wrapper } = panel(status => { status.providers[1]!.servers[0]!.connections = [] })
    expect(wrapper.text()).toContain('Start a chat with Claude Code in this workspace')
    await tab(wrapper, 'OpenCode').trigger('click')
    expect(wrapper.text()).toContain('Start a chat with OpenCode in this workspace')
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
    await wrapper.get('.mcp-refresh').trigger('click')
    expect(get).toHaveBeenCalledWith('/api/mcp/status')
    wrapper.unmount()
  })
  it('puts Ask Ciao in the header, before the tabs, and emits ask-ciao', async () => {
    const { wrapper } = panel()
    expect(wrapper.get('.settings-card-header-actions').text()).toContain('Ask Ciao to add one')
    expect(wrapper.get('.mcp-ask-button').element.compareDocumentPosition(wrapper.get('[role="tablist"]').element) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
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
  it('signs in through the open chat that needs it, and in the terminal otherwise', async () => {
    runMock.mockReset().mockResolvedValue('typed')
    const { wrapper } = panel(status => { status.providers[0]!.servers[0]!.connections = [{ chat_id: 'c', state: 'failed', error: 'boom' }] })
    const quiet = button(wrapper, 'Sign in in terminal')
    expect(quiet.classes()).toContain('btn-small')
    expect(quiet.classes()).not.toContain('btn-primary')
    await quiet.trigger('click')
    expect(runMock).toHaveBeenLastCalledWith(`${PREFIX}claude mcp login notion`, undefined)
    await tab(wrapper, 'OpenCode').trigger('click')
    expect(wrapper.findAll('.mcp-signin').map(b => b.text())).toEqual(['Sign in'])
    expect(button(wrapper, 'Sign in')!.classes()).toEqual(expect.arrayContaining(['btn-primary', 'btn-small']))
    expect(wrapper.text()).toContain('Sign-in required')
    expect(wrapper.text()).toContain('only reaches chats started after it')
    apiPost
      .mockResolvedValueOnce({ action: { id: 'a1', kind: 'mcp_auth', origin: 'engine', state: 'open', params: { server: 'notion', provider: 'opencode' }, error: '', created_at: 1 } })
      .mockResolvedValueOnce({ action: { id: 'a1', kind: 'mcp_auth', origin: 'engine', state: 'waiting', params: { server: 'notion', provider: 'opencode', url: 'https://auth.example/authorize' }, error: '', created_at: 1 } })
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    runMock.mockClear()
    await button(wrapper, 'Sign in')!.trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenNthCalledWith(1, '/api/chats/chat-two/actions/mcp-auth', { server: 'notion' })
    expect(apiPost).toHaveBeenNthCalledWith(2, '/api/chats/chat-two/actions/a1/start', {})
    expect(open).toHaveBeenCalledWith('https://auth.example/authorize', '_blank', 'noopener,noreferrer')
    expect(routerPush).toHaveBeenCalledWith('/chat/chat-two')
    expect(runMock).not.toHaveBeenCalled()
    open.mockRestore()
    wrapper.unmount()
  })
  it('does not restart a sign-in that is already waiting', async () => {
    apiPost.mockResolvedValueOnce({ action: { id: 'a1', kind: 'mcp_auth', origin: 'engine', state: 'waiting', params: { server: 'notion', provider: 'opencode', url: 'https://auth.example/authorize' }, error: '', created_at: 1 } })
    const { wrapper } = panel()
    await tab(wrapper, 'OpenCode').trigger('click')
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    await button(wrapper, 'Sign in')!.trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledTimes(1)
    expect(open).toHaveBeenCalledWith('https://auth.example/authorize', '_blank', 'noopener,noreferrer')
    expect(routerPush).toHaveBeenCalledWith('/chat/chat-two')
    open.mockRestore()
    wrapper.unmount()
  })
  it('still goes to the chat when the start is refused', async () => {
    apiPost
      .mockResolvedValueOnce({ action: { id: 'a1', kind: 'mcp_auth', origin: 'engine', state: 'open', params: { server: 'notion', provider: 'opencode' }, error: '', created_at: 1 } })
      .mockRejectedValueOnce(Object.assign(new Error('invalid_state'), { status: 400, payload: { error: 'invalid_state', action: null } }))
    const { wrapper } = panel()
    await tab(wrapper, 'OpenCode').trigger('click')
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    await button(wrapper, 'Sign in')!.trigger('click')
    await flushPromises()
    expect(open).not.toHaveBeenCalled()
    expect(routerPush).toHaveBeenCalledWith('/chat/chat-two')
    open.mockRestore()
    wrapper.unmount()
  })
  it('says so when the sign-in card cannot be raised', async () => {
    apiPost.mockRejectedValueOnce(Object.assign(new Error('not found'), { status: 404 }))
    const { wrapper } = panel()
    await tab(wrapper, 'OpenCode').trigger('click')
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    await button(wrapper, 'Sign in')!.trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toBe('The sign-in could not be started. Open the chat and try again.')
    expect(routerPush).not.toHaveBeenCalled()
    expect(button(wrapper, 'Sign in')).toBeDefined()
    open.mockRestore()
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
    await wrapper.get('[aria-label="More actions for notion"]').trigger('click')
    expect(wrapper.findAll('[role="menuitem"]')[0]!.text()).toBe('Sign in in terminal')
    wrapper.unmount()
  })
  it('explains servers whose names cannot be used in a command', () => {
    const { wrapper } = panel(status => {
      status.providers[0]!.servers = [{ name: 'odd name', scope: 'account', source: 'provider', allowed: null, connections: [] }]
    })
    expect(wrapper.text()).toContain('This server can’t be managed with a command here; use Claude Code directly.')
    expect(wrapper.findAll('.mcp-signin')).toHaveLength(0)
    expect(wrapper.find('[aria-label="More actions for odd name"]').exists()).toBe(false)
    wrapper.unmount()
  })
  it('shows a claude.ai connector as not connected, managed on claude.ai', () => {
    const { wrapper } = panel(status => {
      status.providers[0]!.servers = [{ name: 'claude.ai Salesloft', scope: 'claudeai', source: 'provider', allowed: null, connections: ['a', 'b'].map(id => ({ chat_id: `chat-${id}`, state: 'needs_auth' as const, error: 'MCP server needs authentication.' })) }]
    })
    const server = wrapper.get('.mcp-server')
    expect(server.get('.mcp-server-heading .set-tag').text()).toBe('claude.ai')
    expect(server.get('.mcp-connection .badge').text()).toBe('not connected')
    expect(server.get('.mcp-connection .badge').classes()).toContain('badge--muted')
    expect(server.text()).not.toContain('sign-in required')
    expect(server.text()).not.toContain('MCP server needs authentication.')
    expect(server.text()).not.toContain('use Claude Code directly')
    // Said once above the list, not under every connector.
    const notes = wrapper.get('.mcp-list-notes')
    expect(notes.get('a[href="https://claude.ai/settings/connectors"]').attributes('rel')).toContain('noopener')
    expect(notes.getComponent(RouterLinkStub).props('to')).toBe('/settings/workspace')
    expect(wrapper.findAll('a[href="https://claude.ai/settings/connectors"]')).toHaveLength(1)
    expect(server.findAll('.mcp-signin')).toHaveLength(0)
    wrapper.unmount()
  })
  it('shows a connected or failed claude.ai connector with its real state, still without a sign-in', () => {
    const { wrapper } = panel(status => {
      status.providers[0]!.servers = [{ name: 'claude.ai Slack', scope: 'claudeai', source: 'provider', allowed: null, connections: [
        { chat_id: 'chat-a', state: 'connected' as const },
        { chat_id: 'chat-b', state: 'failed' as const, error: 'Connection refused' },
      ] }]
    })
    const server = wrapper.get('.mcp-server')
    expect(server.findAll('.mcp-connection .badge').map(b => b.text())).toEqual(['connected', 'connection failed'])
    expect(server.find('.badge--success').exists()).toBe(true)
    expect(server.find('.badge--error').exists()).toBe(true)
    expect(server.text()).toContain('Connection refused')
    expect(wrapper.get('.mcp-list-notes').text()).toContain('claude.ai, Settings, Connectors')
    expect(server.findAll('.mcp-signin')).toHaveLength(0)
    expect(server.find('[aria-label="More actions for claude.ai Slack"]').exists()).toBe(false)
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
  it('turns a server off and back on from its row', async () => {
    const post = vi.fn()
      .mockResolvedValueOnce({ workspace: 'personal', provider: 'claude', server: 'notion', enabled: false, changed: true, applies_to: '/workspace', warning: null, reload: { reloaded: [], pending: ['c1', 'c2'] } })
      .mockResolvedValueOnce({ workspace: 'personal', provider: 'claude', server: 'notion', enabled: true, changed: true, applies_to: '/workspace', warning: null, reload: { reloaded: [], pending: [] } })
    const { wrapper } = panel(undefined, {}, post)
    const toggle = wrapper.get('[role="switch"][aria-label="notion for Claude Code"]')
    expect(toggle.attributes('aria-checked')).toBe('true')
    ;(toggle.element as HTMLElement).focus()
    await toggle.trigger('click')
    await flushPromises()
    expect(toggle.attributes('aria-checked')).toBe('false')
    expect(toggle.text()).toBe('Off')
    expect(wrapper.get('.mcp-server').classes()).toContain('mcp-server--off')
    expect(document.activeElement).toBe(toggle.element)
    expect(wrapper.get('.mcp-toggle-note').text()).toBe('notion is off for Claude Code chats in /workspace. 2 running chats keep their current connection until reloaded. Claude Code sessions running outside Ciaobot keep their current state until they restart.')
    await toggle.trigger('click')
    await flushPromises()
    expect(post).toHaveBeenLastCalledWith('/api/mcp/servers/enabled', { workspace: 'personal', provider: 'claude', server: 'notion', enabled: true })
    expect(toggle.attributes('aria-checked')).toBe('true')
    expect(toggle.text()).toBe('On')
    expect(wrapper.get('.mcp-server').classes()).not.toContain('mcp-server--off')
    expect(wrapper.get('.mcp-toggle-note').text()).toContain('notion is on for Claude Code chats in /workspace.')
    wrapper.unmount()
  })
  it('says what every row shares once, and names a differing target per row', async () => {
    const { wrapper } = panel(status => {
      status.providers[0]!.servers = ['a', 'b', 'c'].map(name => ({ name, scope: 'local', source: '.claude.json', allowed: null, applies_to: '/workspace', connections: [] }))
    })
    expect(wrapper.findAll('.mcp-applies-to')).toHaveLength(1)
    expect(wrapper.text().split('Connection status comes from open chats.')).toHaveLength(2)
    expect(wrapper.findAll('[role="switch"]')).toHaveLength(3)
    expect(wrapper.findAll('.mcp-signin')).toHaveLength(0)
    expect(wrapper.get('details.mcp-manual > summary').text()).toBe('Add a server yourself')
    expect((wrapper.get('details.mcp-manual').element as HTMLDetailsElement).open).toBe(false)
    wrapper.unmount()
    const mixed = panel(status => {
      status.providers[0]!.servers = [
        { name: 'a', scope: 'local', source: '.claude.json', allowed: null, applies_to: '/workspace', connections: [] },
        { name: 'b', scope: 'user', source: '~/.claude.json', allowed: null, applies_to: '/home', connections: [] },
      ]
    }).wrapper
    expect(mixed.findAll('.mcp-applies-to').map(n => n.text())).toEqual(['Applies to: /workspace', 'Applies to: /home'])
    mixed.unmount()
  })
  it('shows where a switch applies before writing', () => {
    const post = vi.fn()
    const { wrapper } = panel(undefined, {}, post)
    expect(wrapper.get('.mcp-applies-to').text()).toContain('/workspace')
    expect(post).not.toHaveBeenCalled()
    wrapper.unmount()
  })
  it('a turned-off row offers no sign-in anywhere', async () => {
    const { wrapper } = panel(status => { for (const p of status.providers) p.servers[0]!.enabled = false })
    await tab(wrapper, 'OpenCode').trigger('click')
    expect(button(wrapper, 'Sign in')).toBeUndefined()
    expect(button(wrapper, 'Sign in in terminal')).toBeUndefined()
    expect(wrapper.find('.mcp-signin').exists()).toBe(false)
    await wrapper.get('[aria-label="More actions for notion"]').trigger('click')
    expect(wrapper.findAll('[role="menuitem"]').map(i => i.text())).toEqual(['Copy sign-out command'])
    expect(wrapper.get('.mcp-applies-to').text()).toContain('Switches apply to new OpenCode chats')
    expect(wrapper.text()).not.toContain('Connection status comes from open chats.')
    wrapper.unmount()
  })
  it('offers no switch when the engine gives no applies_to', async () => {
    const { wrapper } = panel(status => {
      status.providers[0]!.servers = [{ name: 'corp', scope: 'managed', source: 'managed', allowed: null, applies_to: null, connections: [] }]
      status.providers[1]!.servers = [{ name: 'live', scope: 'runtime', source: 'provider', allowed: null, applies_to: null, connections: [] }]
    })
    expect(wrapper.find('.mcp-toggle').exists()).toBe(false)
    expect(wrapper.text()).toContain('Managed by your organization')
    await tab(wrapper, 'OpenCode').trigger('click')
    expect(wrapper.find('.mcp-toggle').exists()).toBe(false)
    wrapper.unmount()
  })
  it('shows the engine warning in the result', async () => {
    const post = vi.fn().mockResolvedValue({ workspace: 'personal', provider: 'opencode', server: 'notion', enabled: false, changed: true, applies_to: '/workspace/personal/opencode.json', warning: 'Another OpenCode config file still turns notion on.', reload: { reloaded: [], pending: [] } })
    const { wrapper } = panel(undefined, {}, post)
    await tab(wrapper, 'OpenCode').trigger('click')
    await wrapper.get('[role="switch"][aria-label="notion for OpenCode"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('.mcp-toggle-note').text()).toContain('Another OpenCode config file still turns notion on.')
    wrapper.unmount()
  })
  it('shows the engine refusal as an alert', async () => {
    const message = 'opencode.jsonc has comments or trailing commas, so Ciaobot leaves it alone.'
    const post = vi.fn().mockRejectedValue(Object.assign(new Error(message), { status: 409 }))
    const { wrapper } = panel(undefined, {}, post)
    await tab(wrapper, 'OpenCode').trigger('click')
    const toggle = wrapper.get('[role="switch"][aria-label="notion for OpenCode"]')
    await toggle.trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toBe(message)
    expect(toggle.attributes('aria-checked')).toBe('true')
    wrapper.unmount()
  })
  it('keeps a turned-off row live connection muted, with a reload hint and no sign-in', async () => {
    const { wrapper } = panel(status => { status.providers[1]!.servers[0]!.enabled = false })
    await tab(wrapper, 'OpenCode').trigger('click')
    const badges = wrapper.findAll('.mcp-connection .badge')
    expect(badges.map(b => b.text())).toEqual(['connection failed', 'sign-in required'])
    expect(badges.every(b => b.classes().includes('badge--muted'))).toBe(true)
    expect(wrapper.text()).toContain('Running chats keep their connection until they reload.')
    expect(button(wrapper, 'Sign in')).toBeUndefined()
    expect(wrapper.find('.mcp-signin').exists()).toBe(false)
    wrapper.unmount()
  })
  it('shows a toggle result only on the provider it was made for', async () => {
    let finish!: (value: unknown) => void
    const post = vi.fn().mockImplementation(() => new Promise(resolve => { finish = resolve }))
    const { wrapper } = panel(undefined, {}, post)
    await tab(wrapper, 'OpenCode').trigger('click')
    await wrapper.get('[role="switch"][aria-label="notion for OpenCode"]').trigger('click')
    await tab(wrapper, 'Claude Code').trigger('click')
    finish({ workspace: 'personal', provider: 'opencode', server: 'notion', enabled: false, changed: true, applies_to: '/workspace/personal/opencode.json', warning: null, reload: { reloaded: [], pending: [] } })
    await flushPromises()
    expect(wrapper.find('.mcp-toggle-note').exists()).toBe(false)
    await tab(wrapper, 'OpenCode').trigger('click')
    expect(wrapper.get('.mcp-toggle-note').text()).toContain('notion is off in /workspace/personal/opencode.json.')
    wrapper.unmount()
  })
  it('disables Refresh while a toggle is pending', async () => {
    let finish!: (value: unknown) => void
    const post = vi.fn().mockImplementation(() => new Promise(resolve => { finish = resolve }))
    const { wrapper } = panel(undefined, {}, post)
    const refresh = wrapper.get('.mcp-refresh')
    expect(refresh.attributes('disabled')).toBeUndefined()
    await wrapper.get('[role="switch"][aria-label="notion for Claude Code"]').trigger('click')
    expect(refresh.attributes('disabled')).toBeDefined()
    finish({ workspace: 'personal', provider: 'claude', server: 'notion', enabled: false, changed: true, applies_to: '/workspace', warning: null, reload: { reloaded: [], pending: [] } })
    await flushPromises()
    expect(refresh.attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })
})
