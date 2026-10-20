import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../lib/api', () => ({ api: { get: vi.fn(), post: vi.fn() } }))
vi.mock('../lib/loopback', () => ({ isLoopbackPage: vi.fn() }))

import { api } from '../lib/api'
import { isLoopbackPage } from '../lib/loopback'
import { resetHostTerminalForTests, useHostTerminal } from './useHostTerminal'

const caps = {
  available: true,
  platform: 'macos',
  reason: '',
  terminal: 'Terminal',
  actions: ['claude-install', 'skills-update'],
}

describe('useHostTerminal', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    resetHostTerminalForTests()
    vi.mocked(isLoopbackPage).mockReturnValue(true)
  })

  it('fetches capabilities once for every caller', async () => {
    vi.mocked(api.get).mockResolvedValue(caps)
    await Promise.all([useHostTerminal().ensureCapabilities(), useHostTerminal().ensureCapabilities()])
    await useHostTerminal().ensureCapabilities()
    expect(api.get).toHaveBeenCalledTimes(1)
    expect(api.get).toHaveBeenCalledWith('/api/host/terminal')
    expect(useHostTerminal().available.value).toBe(true)
  })

  it('never asks the engine from a non-loopback page', async () => {
    vi.mocked(isLoopbackPage).mockReturnValue(false)
    const host = useHostTerminal()
    await host.ensureCapabilities()
    expect(api.get).not.toHaveBeenCalled()
    expect(host.available.value).toBe(false)
    expect(host.capabilities.value?.reason).toBe('not_on_host')
  })

  it('treats a failed or malformed capability read as unavailable', async () => {
    vi.mocked(api.get).mockRejectedValueOnce(new Error('offline'))
    await useHostTerminal().ensureCapabilities()
    expect(useHostTerminal().available.value).toBe(false)

    resetHostTerminalForTests()
    vi.mocked(api.get).mockResolvedValueOnce('nope')
    await useHostTerminal().ensureCapabilities()
    expect(useHostTerminal().available.value).toBe(false)

    resetHostTerminalForTests()
    vi.mocked(api.get).mockResolvedValueOnce({ ...caps, available: 'yes' })
    await useHostTerminal().ensureCapabilities()
    expect(useHostTerminal().available.value).toBe(false)

    resetHostTerminalForTests()
    vi.mocked(api.get).mockResolvedValueOnce({ available: false, reason: 'unavailable_headless' })
    await useHostTerminal().ensureCapabilities()
    expect(useHostTerminal().capabilities.value?.reason).toBe('unavailable_headless')
  })

  it('only offers actions the engine lists', async () => {
    vi.mocked(api.get).mockResolvedValue(caps)
    const host = useHostTerminal()
    await host.ensureCapabilities()
    expect(host.canRun('claude-install')).toBe(true)
    expect(host.canRun('gws-install')).toBe(false)
  })

  it('posts an action id, never a command', async () => {
    vi.mocked(api.post).mockResolvedValue({ ok: true, platform: 'macos', launched: 'Terminal' })
    await useHostTerminal().runAction('skills-update')
    expect(api.post).toHaveBeenCalledWith('/api/host/terminal', { action: 'skills-update' })
  })

  it('posts a folder request with only the ids it was given', async () => {
    vi.mocked(api.post).mockResolvedValue({ ok: true, platform: 'macos', launched: 'Terminal' })
    const host = useHostTerminal()
    await host.openFolder({ chatId: 'c1' })
    await host.openFolder({ projectId: 'p1' })
    expect(vi.mocked(api.post).mock.calls).toEqual([
      ['/api/host/terminal', { open: 'folder', chat_id: 'c1' }],
      ['/api/host/terminal', { open: 'folder', project_id: 'p1' }],
    ])
  })
})
