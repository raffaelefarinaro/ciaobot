import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../lib/api'
import { resetWelcomeForTests, useWelcome } from '../useWelcome'

vi.mock('../../lib/api', () => ({ api: { get: vi.fn(), post: vi.fn() } }))

const get = vi.mocked(api.get)

beforeEach(() => {
  vi.clearAllMocks()
  resetWelcomeForTests()
})

describe('useWelcome', () => {
  it('dedupes concurrent ensureStatus calls into one request', async () => {
    get.mockResolvedValue({ pending: true, workspace_root: '/w', provider: 'claude', provider_label: 'Claude Code', on_host: true, platform: 'darwin' })
    const { ensureStatus, status } = useWelcome()
    await Promise.all([ensureStatus(), ensureStatus()])
    expect(get).toHaveBeenCalledTimes(1)
    expect(status.value?.pending).toBe(true)
  })

  it('treats a request failure as not pending', async () => {
    get.mockRejectedValue(new Error('boom'))
    const { ensureStatus, status } = useWelcome()
    await ensureStatus()
    expect(status.value?.pending).toBe(false)
  })
})
