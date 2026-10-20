import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  FAILURE_THRESHOLD,
  HEALTHY_INTERVAL_MS,
  OUTAGE_GRACE_MS,
  PROBE_TIMEOUT_MS,
  RECOVERY_INTERVAL_MS,
  classifyResponse,
  createEngineMonitor,
  probeEngine,
  requestStartupStatus,
  type ProbeResult,
} from './engineStatus'

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
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

  it('tells a request that ran out of time from one that never got an answer', () => {
    // No answer at all: the request was refused, reset, or never left.
    expect(classifyResponse(0, null)).toBe('failure')
    // Our own budget ran out: nobody answered in time, which is a slow path
    // far more often than a dead engine.
    expect(classifyResponse(0, null, true)).toBe('timeout')
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
      init.signal?.addEventListener('abort', () => reject(abortError()))
    }))
    const pending = probeEngine(hanging as unknown as typeof fetch)
    await vi.advanceTimersByTimeAsync(PROBE_TIMEOUT_MS)
    // A timeout is not the same evidence as a failure: it says the path did
    // not answer in time, not that the engine is gone.
    await expect(pending).resolves.toBe('timeout')
  })
})

describe('requestStartupStatus', () => {
  it('shares an in-flight default request between startup and engine consumers', async () => {
    const fetchMock = vi.fn(async () => ({
      status: 200,
      json: async () => ({ overall_ready: true }),
    }))
    vi.stubGlobal('fetch', fetchMock)

    const [startup, monitor] = await Promise.all([requestStartupStatus(), requestStartupStatus()])

    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(startup).toEqual({ status: 200, body: { overall_ready: true }, timedOut: false })
    expect(monitor).toEqual(startup)
  })

  it('aborts a shared request that never responds', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn((_url: string, init: RequestInit) => new Promise<Response>((_resolve, reject) => {
      init.signal?.addEventListener('abort', () => reject(abortError()))
    }))
    vi.stubGlobal('fetch', fetchMock)
    const first = requestStartupStatus()
    const second = requestStartupStatus()

    await vi.advanceTimersByTimeAsync(PROBE_TIMEOUT_MS)

    await expect(first).resolves.toEqual({ status: 0, body: null, timedOut: true })
    await expect(second).resolves.toEqual({ status: 0, body: null, timedOut: true })
    expect(fetchMock).toHaveBeenCalledTimes(1)
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

  it('keeps polling after a probe throws', async () => {
    vi.useFakeTimers()
    const onChange = vi.fn()
    // A throwing probe (a stubbed fetch that rejects, a browser that throws on
    // the abort) must not end the loop: nothing else can raise the curtain
    // again, and no Retry button is reachable while it is up.
    const probe = vi.fn<() => Promise<ProbeResult>>()
    probe.mockRejectedValueOnce(new Error('probe exploded'))
    probe.mockResolvedValue('ready')
    const monitor = createEngineMonitor({ probe, onChange })
    monitor.start()
    await vi.advanceTimersByTimeAsync(HEALTHY_INTERVAL_MS * 2)
    // The rejection reported no state (the state stays `ready`, failures 0), so
    // the next round is a healthy one.
    expect(probe).toHaveBeenCalledTimes(2)
    expect(monitor.state).toBe('ready')
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

  it('retry during an in-flight probe does not start a second loop', async () => {
    vi.useFakeTimers()
    const onChange = vi.fn()
    // A remote host that is off hangs until the abort, so the scheduled probe
    // is still in flight when the user hits Retry.
    const probe = vi.fn<() => Promise<ProbeResult>>(() => new Promise((resolve) => {
      setTimeout(() => resolve('failure'), 3000)
    }))
    const monitor = createEngineMonitor({ probe, onChange })
    monitor.start()
    await vi.advanceTimersByTimeAsync(HEALTHY_INTERVAL_MS)
    // The scheduled tick has fired and its probe is still pending.
    void monitor.retry()
    await vi.advanceTimersByTimeAsync(3000)
    probe.mockClear()
    // Each remaining round is one probe plus one recovery interval. Two live
    // loops would double that.
    await vi.advanceTimersByTimeAsync((3000 + RECOVERY_INTERVAL_MS) * 4)
    expect(probe.mock.calls.length).toBe(4)
    monitor.stop()
  })

  it('does not raise the curtain for unanswered probes before the grace window', async () => {
    vi.useFakeTimers()
    const onChange = vi.fn()
    // A remote origin on a slow path: the probe runs out of time every round
    // while the engine is perfectly healthy. Two of those must not be read as
    // "the computer is off".
    const probe = queue(new Array(12).fill('timeout'))
    const monitor = createEngineMonitor({ probe, onChange })
    monitor.start()
    await vi.advanceTimersByTimeAsync(HEALTHY_INTERVAL_MS + RECOVERY_INTERVAL_MS * 4)
    expect(monitor.state).toBe('ready')
    expect(onChange).not.toHaveBeenCalled()
    monitor.stop()
  })

  it('raises the curtain for unanswered probes once the grace window passes', async () => {
    vi.useFakeTimers()
    const onChange = vi.fn()
    const probe = queue(new Array(12).fill('timeout'))
    const monitor = createEngineMonitor({ probe, onChange })
    monitor.start()
    await vi.advanceTimersByTimeAsync(HEALTHY_INTERVAL_MS + RECOVERY_INTERVAL_MS * 5)
    expect(monitor.state).toBe('unreachable')
    monitor.stop()
  })

  it('trusts the socket\'s proof of life over a failed probe', async () => {
    vi.useFakeTimers()
    const onChange = vi.fn()
    // The awareness socket carries a keepalive every few seconds, so the
    // engine is provably serving. A probe that times out against it is the
    // path being slow, and must never raise the "computer is off" curtain.
    const probe = queue(new Array(12).fill('timeout'))
    const monitor = createEngineMonitor({ probe, isAlive: () => true, onChange })
    monitor.start()
    await vi.advanceTimersByTimeAsync(HEALTHY_INTERVAL_MS + RECOVERY_INTERVAL_MS * 8)
    expect(monitor.state).toBe('ready')
    expect(onChange).not.toHaveBeenCalled()
    monitor.stop()
  })

  it('lifts the curtain when the socket proves the engine is alive', async () => {
    vi.useFakeTimers()
    const onChange = vi.fn()
    let alive = false
    const probe = queue(new Array(12).fill('failure'))
    const monitor = createEngineMonitor({ probe, isAlive: () => alive, onChange })
    monitor.start()
    await vi.advanceTimersByTimeAsync(HEALTHY_INTERVAL_MS + RECOVERY_INTERVAL_MS)
    expect(monitor.state).toBe('unreachable')
    // The engine comes back: the socket says so, so the curtain lifts even
    // though the next probe has not answered yet.
    alive = true
    await vi.advanceTimersByTimeAsync(RECOVERY_INTERVAL_MS)
    expect(monitor.state).toBe('ready')
    expect(onChange.mock.calls.map(([state]) => state)).toEqual(['unreachable', 'ready'])
    monitor.stop()
  })

  it('uses the grace window it was given', async () => {
    vi.useFakeTimers()
    const onChange = vi.fn()
    const probe = queue(new Array(12).fill('timeout'))
    const monitor = createEngineMonitor({ probe, outageGraceMs: 1000, onChange })
    monitor.start()
    await vi.advanceTimersByTimeAsync(HEALTHY_INTERVAL_MS + RECOVERY_INTERVAL_MS * 5)
    expect(monitor.state).toBe('unreachable')
    monitor.stop()
  })

  it('documents the grace window the monitor uses by default', () => {
    expect(OUTAGE_GRACE_MS).toBeGreaterThan(HEALTHY_INTERVAL_MS)
  })
})

/** The error a browser raises when its own abort fires. */
function abortError(): Error {
  return Object.assign(new Error('aborted'), { name: 'AbortError' })
}
