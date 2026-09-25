import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  FAILURE_THRESHOLD,
  HEALTHY_INTERVAL_MS,
  PROBE_TIMEOUT_MS,
  RECOVERY_INTERVAL_MS,
  classifyResponse,
  createEngineMonitor,
  probeEngine,
  type ProbeResult,
} from './engineStatus'

afterEach(() => {
  vi.useRealTimers()
})

describe('classifyResponse', () => {
  it('reads the readiness flag out of a healthy answer', () => {
    expect(classifyResponse(200, { overall_ready: true })).toBe('ready')
    expect(classifyResponse(200, { overall_ready: false })).toBe('booting')
    // A 200 with no body at all is still a live engine answering.
    expect(classifyResponse(200, null)).toBe('ready')
  })

  it('treats an auth challenge as a login problem, not an outage', () => {
    expect(classifyResponse(401, null)).toBe('auth_required')
    expect(classifyResponse(403, null)).toBe('auth_required')
  })

  it('treats every other non-ok status as a failure', () => {
    expect(classifyResponse(502, {})).toBe('failure')
    expect(classifyResponse(503, {})).toBe('failure')
    expect(classifyResponse(504, {})).toBe('failure')
    expect(classifyResponse(500, {})).toBe('failure')
  })
})

describe('probeEngine', () => {
  it('reports failure when the request cannot be made at all', async () => {
    const boom = vi.fn(async () => {
      throw new TypeError('Failed to fetch')
    })
    await expect(probeEngine(boom as unknown as typeof fetch)).resolves.toBe('failure')
  })

  it('reports failure for a proxy error', async () => {
    const res = { status: 503, json: async () => ({}) }
    await expect(probeEngine((async () => res) as unknown as typeof fetch)).resolves.toBe('failure')
  })

  it('gives up on a hanging engine after the timeout instead of waiting forever', async () => {
    vi.useFakeTimers()
    // A dead engine behind a proxy that never answers is the common case: the
    // request stays open until our own abort fires.
    const hanging = vi.fn((_url: string, init: RequestInit) => new Promise<Response>((_resolve, reject) => {
      init.signal?.addEventListener('abort', () => reject(new Error('aborted')))
    }))
    const pending = probeEngine(hanging as unknown as typeof fetch)
    await vi.advanceTimersByTimeAsync(PROBE_TIMEOUT_MS)
    await expect(pending).resolves.toBe('failure')
  })
})

describe('createEngineMonitor', () => {
  function queue(results: ProbeResult[]) {
    const probe = vi.fn<() => Promise<ProbeResult>>()
    for (const result of results) probe.mockResolvedValueOnce(result)
    return probe
  }

  it('does not report unreachable for a single failed probe', async () => {
    vi.useFakeTimers()
    const onChange = vi.fn()
    const monitor = createEngineMonitor({ probe: queue(['failure']), onChange })
    monitor.start()
    await vi.advanceTimersByTimeAsync(HEALTHY_INTERVAL_MS)
    expect(monitor.state).toBe('ready')
    expect(onChange).not.toHaveBeenCalled()
    monitor.stop()
  })

  it('reports unreachable only after the threshold of consecutive failures', async () => {
    vi.useFakeTimers()
    const onChange = vi.fn()
    const monitor = createEngineMonitor({ probe: queue(['failure', 'failure']), onChange })
    monitor.start()
    await vi.advanceTimersByTimeAsync(HEALTHY_INTERVAL_MS + RECOVERY_INTERVAL_MS)
    expect(monitor.state).toBe('unreachable')
    expect(onChange.mock.calls.filter(([state]) => state === 'unreachable')).toHaveLength(1)
    monitor.stop()
  })

  it('reads a failure during an announced restart as updating', async () => {
    vi.useFakeTimers()
    const onChange = vi.fn()
    const monitor = createEngineMonitor({
      probe: queue(['failure', 'failure']),
      isUpdating: () => true,
      onChange,
    })
    monitor.start()
    await vi.advanceTimersByTimeAsync(HEALTHY_INTERVAL_MS + RECOVERY_INTERVAL_MS)
    expect(monitor.state).toBe('updating')
    expect(onChange).toHaveBeenCalledWith('updating')
    monitor.stop()
  })

  it('never turns an auth challenge into an outage', async () => {
    vi.useFakeTimers()
    const onChange = vi.fn()
    const monitor = createEngineMonitor({
      probe: queue(['auth_required', 'auth_required', 'auth_required', 'auth_required', 'auth_required']),
      onChange,
    })
    monitor.start()
    // Five probes: the first at the healthy interval, the rest while the state
    // is not `ready`.
    await vi.advanceTimersByTimeAsync(HEALTHY_INTERVAL_MS + RECOVERY_INTERVAL_MS * 4)
    // The login flow owns a 401; the outage screen must stay out of it.
    expect(onChange.mock.calls.map(([state]) => state)).toEqual(['auth_required'])
    monitor.stop()
  })

  it('recovers to ready and resets the failure count', async () => {
    vi.useFakeTimers()
    const onChange = vi.fn()
    const monitor = createEngineMonitor({ probe: queue(['failure', 'failure', 'ready', 'failure']), onChange })
    monitor.start()
    // 10s: first failure (1/2). +2s: second failure, unreachable. +2s: ready.
    // +10s: one more failure, back under the threshold. Stopping there.
    await vi.advanceTimersByTimeAsync(HEALTHY_INTERVAL_MS + RECOVERY_INTERVAL_MS * 2 + HEALTHY_INTERVAL_MS)
    expect(monitor.state).toBe('ready')
    expect(onChange.mock.calls.map(([state]) => state)).toEqual(['unreachable', 'ready'])
    // The single failure after recovery is below the threshold again, so the
    // screen does not come back for one blip.
    expect(FAILURE_THRESHOLD).toBe(2)
    monitor.stop()
  })

  it('probes immediately on retry', async () => {
    vi.useFakeTimers()
    const onChange = vi.fn()
    const probe = queue(['failure', 'failure', 'ready'])
    const monitor = createEngineMonitor({ probe, onChange })
    monitor.start()
    await vi.advanceTimersByTimeAsync(HEALTHY_INTERVAL_MS + RECOVERY_INTERVAL_MS)
    expect(monitor.state).toBe('unreachable')
    const callsBeforeRetry = probe.mock.calls.length
    // No timer advance: the button is the probe.
    await monitor.retry()
    expect(probe.mock.calls.length).toBe(callsBeforeRetry + 1)
    expect(monitor.state).toBe('ready')
    expect(onChange).toHaveBeenLastCalledWith('ready')
    monitor.stop()
  })
})
