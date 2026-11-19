// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import ProviderInventory from '../settings/ProviderInventory.vue'
import ProviderSignInGuidance from '../settings/ProviderSignInGuidance.vue'

const state = vi.hoisted(() => ({ open: vi.fn(), copy: vi.fn() }))
vi.mock('../../stores/terminal', () => ({ useTerminalStore: () => ({ openWorkspaceForSettings: state.open }) }))
vi.mock('../../lib/codeCopy', () => ({ writeClipboard: state.copy }))

beforeEach(() => { state.open.mockReset(); state.copy.mockReset() })

describe('provider inventory preparation', () => {
  it('renders only reported metadata as text and generic manual guidance', async () => {
    const view = mount(ProviderInventory, { props: {
      command: 'opencode auth login',
      workspace: 'work',
      inventory: { status: 'partial', providers: [
        { id: 'one', name: '<img src=x>', auth_types: ['oauth', 'api_key'], configured: true, verified: null, ...{ secret: 'hidden' } },
        { id: 'two', name: '<img src=x>', auth_types: ['environment'], configured: true, verified: null },
      ] },
    } })
    expect(view.find('img').exists()).toBe(false)
    expect(view.text()).toContain('<img src=x>')
    expect(view.text()).not.toContain('hidden')
    expect(view.text()).toContain('Some providers may be missing')
    expect(view.text()).toContain('Configured · verification unknown')
    expect(view.findAll('summary').map(x => x.text())).toContain('Reconnect <img src=x> (one)')
    expect(view.findAll('button').map(button => button.attributes('aria-label'))).toEqual([
      'Copy sign-in command for <img src=x> (one)',
      'Open browser terminal for <img src=x> (one)',
      'Copy sign-in command for <img src=x> (two)',
      'Open browser terminal for <img src=x> (two)',
      'Copy sign-in command for a provider',
      'Open browser terminal for a provider',
    ])
    expect(state.copy).not.toHaveBeenCalled()
    await view.find('button').trigger('click')
    expect(state.copy).toHaveBeenCalledWith('opencode auth login')
    view.unmount()
  })

  it('distinguishes empty and unavailable, and never takes over a busy terminal', async () => {
    state.open.mockResolvedValue('busy')
    const view = mount(ProviderInventory, { props: { command: 'opencode auth login', workspace: 'work', inventory: { status: 'complete', providers: [] } } })
    expect(view.text()).toContain('No configured providers reported')
    await view.findAll('button')[1]!.trigger('click')
    await flushPromises()
    expect(view.text()).toContain('Another terminal is open')
    expect(state.open).toHaveBeenCalledWith('work')
    await view.setProps({ inventory: undefined, workspace: '' })
    expect(view.text()).toContain('Provider inventory unavailable')
    await view.setProps({ inventory: { status: 'unknown', providers: [] } })
    expect(view.text()).toContain('OpenCode could not report its configured providers')
    await view.findAll('button')[1]!.trigger('click')
    expect(view.text()).toContain('Select a workspace')
    view.unmount()
  })

  it('does not publish terminal results after a workspace round trip', async () => {
    let finish!: (result: string) => void
    state.open.mockReturnValue(new Promise(resolve => { finish = resolve }))
    const view = mount(ProviderInventory, { props: { command: 'opencode auth login', workspace: 'A' } })
    await view.findAll('button')[1]!.trigger('click')
    await view.setProps({ workspace: 'B' })
    await view.setProps({ workspace: 'A' })
    finish('busy')
    await flushPromises()
    expect(view.text()).not.toContain('Another terminal is open')
    view.unmount()
  })

  it('reports clipboard and terminal failures without exposing raw errors', async () => {
    state.copy.mockRejectedValue(new Error('sensitive clipboard error'))
    state.open.mockRejectedValue(new Error('sensitive terminal error'))
    const view = mount(ProviderInventory, { props: { command: 'opencode auth login', workspace: 'work' } })
    await view.findAll('button')[0]!.trigger('click')
    expect(view.text()).toContain('Could not copy')
    await view.findAll('button')[1]!.trigger('click')
    expect(view.text()).toContain('Could not open the terminal')
    expect(view.text()).not.toContain('sensitive')
    view.unmount()
  })

  it.each([
    ['partial', 'Some providers may be missing'],
    ['unknown', 'OpenCode could not report its configured providers'],
  ] as const)('reports %s rather than an empty inventory', (status, message) => {
    const view = mount(ProviderInventory, { props: { command: 'opencode auth login', workspace: 'work', inventory: { status, providers: [] } } })
    expect(view.text()).toContain(message)
    expect(view.text()).not.toContain('No configured providers reported')
    expect(view.find('summary').text()).toBe('Connect provider')
    view.unmount()
  })

  it('reports an invalid workspace and a refused clipboard without displaying raw errors', async () => {
    state.open.mockResolvedValue('invalid')
    state.copy.mockResolvedValue(false)
    const view = mount(ProviderInventory, { props: { command: 'opencode auth login', workspace: 'invalid' } })
    await view.findAll('button')[0]!.trigger('click')
    expect(view.text()).toContain('Could not copy. Select the command')
    await view.findAll('button')[1]!.trigger('click')
    expect(view.text()).toContain('The workspace is unavailable')
    expect(state.open).toHaveBeenCalledWith('invalid')
    expect(view.text()).toContain('opencode auth login')
    view.unmount()
  })

  it.each(['remote policy refusal', 'terminal capacity failure'])( 'does not claim a usable shell when the pane handles %s', async (paneFailure) => {
    // The terminal store reports an opening attempt; policy/capacity errors
    // belong to the pane, not a rejected promise or a successful shell.
    state.open.mockResolvedValue('opened')
    const view = mount(ProviderInventory, { props: { command: 'opencode auth login', workspace: 'work' } })
    await view.findAll('button')[1]!.trigger('click')
    expect(view.text()).toContain('Terminal opening requested. Check the terminal pane for availability.')
    expect(view.text()).not.toContain(paneFailure)
    expect(view.text()).not.toContain('shell is ready')
    view.unmount()
  })

  it('keeps provider metadata out of copied commands and terminal arguments', async () => {
    state.copy.mockResolvedValue(true)
    state.open.mockResolvedValue('opened')
    const view = mount(ProviderInventory, { props: { command: 'opencode auth login', workspace: 'workspace', inventory: {
      status: 'complete', providers: [{ id: 'x; secret', name: 'Provider $(danger)', auth_types: ['unknown'], configured: true, verified: null }],
    } } })
    expect(view.text()).toContain('Unknown · Configured · verification unknown')
    expect(view.findAll('code').every(code => code.text() === 'opencode auth login')).toBe(true)
    for (const button of view.findAll('button').filter(button => button.text() === 'Copy')) await button.trigger('click')
    expect(state.copy).toHaveBeenCalledTimes(2)
    expect(state.copy.mock.calls).toEqual([['opencode auth login'], ['opencode auth login']])
    await view.findAll('button').find(button => button.text() === 'Open browser terminal')!.trigger('click')
    expect(state.open).toHaveBeenCalledExactlyOnceWith('workspace')
    view.unmount()
  })

  it('shows the command it is given and never another provider\'s', async () => {
    state.copy.mockResolvedValue(true)
    const view = mount(ProviderSignInGuidance, { props: { workspace: 'work', context: 'Claude Code', command: 'claude auth login' } })
    expect(view.get('code').text()).toBe('claude auth login')
    expect(view.text()).toContain('available on your PATH')
    await view.get('button').trigger('click')
    expect(state.copy).toHaveBeenCalledExactlyOnceWith('claude auth login')
    expect(state.open).not.toHaveBeenCalled()
    view.unmount()
  })

  it('offers no sign-in guidance when the OpenCode row reported no command', async () => {
    const view = mount(ProviderInventory, { props: { workspace: 'work', inventory: { status: 'complete', providers: [] } } })
    expect(view.text()).not.toContain('Connect provider')
    expect(view.find('code').exists()).toBe(false)
    view.unmount()
  })

  it('shows the Codex sign-in command for Codex, not the OpenCode one', async () => {
    state.copy.mockResolvedValue(true)
    const view = mount(ProviderSignInGuidance, { props: { workspace: 'work', context: 'Codex', command: 'codex login' } })
    expect(view.get('code').text()).toBe('codex login')
    expect(view.text()).not.toContain('opencode auth login')
    await view.get('button').trigger('click')
    expect(state.copy).toHaveBeenCalledExactlyOnceWith('codex login')
    view.unmount()
  })
})
