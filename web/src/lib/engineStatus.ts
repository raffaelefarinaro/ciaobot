// How the browser tells "the engine is fine" from "the engine is gone".
//
// `StartupView` only covers the first boot and `RestartNotice` only appears
// when the server announced a restart, so a crash, `ciao service stop` or a
// reboot left an already-loaded tab with no honest state: API calls failed one
// by one and the WebSocket backed off silently. The public startup-status
// endpoint answers while the engine is starting but not while it is down, so it
// is the one honest probe, and the classification below keeps its answer plain:
// `auth_required` is not an outage (the login flow owns it) and only a repeated
// `failure` becomes an outage.

export type ProbeResult = 'ready' | 'booting' | 'auth_required' | 'failure'
export type EngineState = 'ready' | 'booting' | 'auth_required' | 'updating' | 'unreachable'

export const PROBE_TIMEOUT_MS = 4000
export const FAILURE_THRESHOLD = 2
export const HEALTHY_INTERVAL_MS = 10000
export const RECOVERY_INTERVAL_MS = 2000

export interface StartupStatus {
  status: number
  body: unknown
}

let sharedStartupRequest: Promise<StartupStatus | null> | null = null

/** One bounded read shared by the startup curtain and the engine monitor. */
export function requestStartupStatus(
  fetchImpl: typeof fetch = fetch,
  timeoutMs = PROBE_TIMEOUT_MS,
): Promise<StartupStatus | null> {
  const share = fetchImpl === fetch && timeoutMs === PROBE_TIMEOUT_MS
  if (share && sharedStartupRequest) return sharedStartupRequest

  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  const request = (async (): Promise<StartupStatus | null> => {
    try {
      const res = await fetchImpl('/api/startup-status', {
        cache: 'no-store', redirect: 'manual', signal: controller.signal,
      })
      let body: unknown = null
      try { body = await res.json() } catch { body = null }
      return { status: res.status, body }
    } catch {
      return null
    } finally {
      clearTimeout(timer)
    }
  })()

  if (share) {
    sharedStartupRequest = request
    void request.finally(() => {
      if (sharedStartupRequest === request) sharedStartupRequest = null
    })
  }
  return request
}

/** Pure classification of one /api/startup-status response. */
export function classifyResponse(status: number, body: unknown): ProbeResult {
  if (status === 401 || status === 403) return 'auth_required'
  if (status < 200 || status >= 300) return 'failure'
  const ready = (body as { overall_ready?: unknown } | null)?.overall_ready
  return ready === false ? 'booting' : 'ready'
}

export async function probeEngine(fetchImpl: typeof fetch = fetch, timeoutMs = PROBE_TIMEOUT_MS): Promise<ProbeResult> {
  const response = await requestStartupStatus(fetchImpl, timeoutMs)
  return response ? classifyResponse(response.status, response.body) : 'failure'
}

export interface EngineMonitorOptions {
  probe?: () => Promise<ProbeResult>
  isUpdating?: () => boolean
  onChange: (state: EngineState) => void
  failureThreshold?: number
}

/** Polls the engine; reports `unreachable` only after consecutive failures. */
export function createEngineMonitor(opts: EngineMonitorOptions) {
  const probe = opts.probe ?? (() => probeEngine())
  const threshold = opts.failureThreshold ?? FAILURE_THRESHOLD
  let state: EngineState = 'ready'
  let failures = 0
  let timer: ReturnType<typeof setTimeout> | null = null
  let stopped = true
  // A probe outlives the timeout that started it: a dead engine hangs until the
  // 4s abort, so by then `timer` is already null and `clearTimeout` in
  // `retry()` has nothing to cancel. One shared promise is what makes a click
  // during an in-flight probe join that probe instead of adding a second loop.
  let inflight: Promise<void> | null = null

  function set(next: EngineState) {
    if (next !== state) { state = next; opts.onChange(next) }
  }
  async function runProbe(): Promise<void> {
    const result = await probe()
    if (result === 'failure') {
      failures += 1
      if (failures >= threshold) set(opts.isUpdating?.() ? 'updating' : 'unreachable')
    } else {
      failures = 0
      set(result)
    }
  }
  function tick(): Promise<void> {
    if (!inflight) inflight = runProbe().finally(() => { inflight = null })
    return inflight
  }
  function schedule() {
    if (stopped) return
    // Both the scheduled tick and a concurrent `retry()` schedule when their
    // probe settles; whichever is last owns the one live timer.
    if (timer) clearTimeout(timer)
    const delay = state === 'ready' && failures === 0 ? HEALTHY_INTERVAL_MS : RECOVERY_INTERVAL_MS
    // `catch` + `finally`, not a bare await: a probe that rejects (a stubbed
    // fetch, a browser that throws on the abort) would leave the loop dead and
    // the curtain up forever with nothing left to retry from, and the rejection
    // would surface as an unhandled error rather than as a failed probe.
    timer = setTimeout(async () => {
      timer = null
      try { await tick() } catch { /* the next round re-asks the engine */ } finally { schedule() }
    }, delay)
  }
  return {
    start() { if (!stopped) return; stopped = false; schedule() },
    stop() { stopped = true; if (timer) clearTimeout(timer); timer = null },
    /** Probe now (Retry button); resolves after the state is updated. */
    async retry() { if (timer) clearTimeout(timer); timer = null; await tick(); schedule() },
    get state() { return state },
  }
}
