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

export type ProbeResult = 'ready' | 'booting' | 'auth_required' | 'failure' | 'timeout'
export type EngineState = 'ready' | 'booting' | 'auth_required' | 'updating' | 'unreachable'

// How long one probe may take before we give up on it.
//
// The endpoint answers in a few milliseconds on the engine's own machine, but
// the PWA is also served to a phone or a laptop reaching the engine over a
// VPN or a relay, where the same answer can take seconds. A deadline tuned for
// a loopback origin turns "slow" into "gone" on a remote one, so the budget is
// generous: a probe that has not answered in ten seconds is a path problem,
// and it is judged as one (see OUTAGE_GRACE_MS) rather than as a dead engine.
export const PROBE_TIMEOUT_MS = 10000
export const FAILURE_THRESHOLD = 2
export const HEALTHY_INTERVAL_MS = 10000
export const RECOVERY_INTERVAL_MS = 2000
// How long the engine must go without answering before the outage curtain may
// appear. A probe that merely ran out of time proves the path is slow, not
// that the computer is off, so on its own it only counts after this long.
export const OUTAGE_GRACE_MS = 20000

export interface StartupStatus {
  /** HTTP status of the answer, or 0 when no answer arrived at all. */
  status: number
  body: unknown
  /**
   * True when the request was still open when our own budget ran out.
   *
   * A timeout and a failure are different evidence: a timeout means nobody
   * answered in time (the path is slow or stalled), a failure means the
   * request never got an answer at all (refused, reset, DNS, TLS).
   */
  timedOut: boolean
}

let sharedStartupRequest: Promise<StartupStatus> | null = null

/** One bounded read shared by the startup curtain and the engine monitor. */
export function requestStartupStatus(
  fetchImpl: typeof fetch = fetch,
  timeoutMs = PROBE_TIMEOUT_MS,
): Promise<StartupStatus> {
  const share = fetchImpl === fetch && timeoutMs === PROBE_TIMEOUT_MS
  if (share && sharedStartupRequest) return sharedStartupRequest

  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  const request = (async (): Promise<StartupStatus> => {
    try {
      const res = await fetchImpl('/api/startup-status', {
        cache: 'no-store', redirect: 'manual', signal: controller.signal,
      })
      let body: unknown = null
      try { body = await res.json() } catch { body = null }
      return { status: res.status, body, timedOut: false }
    } catch (err) {
      // Our own abort is the one failure that says "nobody answered in
      // time". Anything else is the request failing, which is much stronger
      // evidence that the engine is not there.
      const timedOut = (err as { name?: string } | null)?.name === 'AbortError'
      return { status: 0, body: null, timedOut }
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
export function classifyResponse(status: number, body: unknown, timedOut = false): ProbeResult {
  // Status 0 is not an HTTP answer: the request never got one.
  if (status === 0) return timedOut ? 'timeout' : 'failure'
  if (status === 401 || status === 403) return 'auth_required'
  if (status < 200 || status >= 300) return 'failure'
  const ready = (body as { overall_ready?: unknown } | null)?.overall_ready
  return ready === false ? 'booting' : 'ready'
}

export async function probeEngine(fetchImpl: typeof fetch = fetch, timeoutMs = PROBE_TIMEOUT_MS): Promise<ProbeResult> {
  const response = await requestStartupStatus(fetchImpl, timeoutMs)
  return classifyResponse(response.status, response.body, response.timedOut)
}

export interface EngineMonitorOptions {
  probe?: () => Promise<ProbeResult>
  isUpdating?: () => boolean
  /**
   * Did the engine prove it is alive on another channel?
   *
   * The awareness WebSocket carries a keepalive every few seconds, so a
   * recent frame is proof the engine is up and serving. An HTTP probe that
   * failed against a live engine says the path is slow, not that the
   * computer is off, so it must not raise the outage curtain.
   */
  isAlive?: () => boolean
  onChange: (state: EngineState) => void
  failureThreshold?: number
  /** How long the engine must go unanswered before the curtain appears. */
  outageGraceMs?: number
}

/** Polls the engine; reports `unreachable` only after a sustained outage. */
export function createEngineMonitor(opts: EngineMonitorOptions) {
  const probe = opts.probe ?? (() => probeEngine())
  const threshold = opts.failureThreshold ?? FAILURE_THRESHOLD
  const grace = opts.outageGraceMs ?? OUTAGE_GRACE_MS
  let state: EngineState = 'ready'
  let failures = 0
  // A refused or errored request is the engine saying it is not there; a
  // request that merely ran out of time is the path being slow. Only the
  // first may raise the curtain on its own.
  let decisiveFailures = 0
  // When the engine last answered. The page was served by the engine, so it
  // was alive when the monitor started looking.
  let lastSeenAt = Date.now()
  let timer: ReturnType<typeof setTimeout> | null = null
  let stopped = true
  // A probe outlives the timeout that started it: a dead engine hangs until
  // the 10s abort, so by then `timer` is already null and `clearTimeout` in
  // `retry()` has nothing to cancel. One shared promise is what makes a click
  // during an in-flight probe join that probe instead of adding a second loop.
  let inflight: Promise<void> | null = null

  function set(next: EngineState) {
    if (next !== state) { state = next; opts.onChange(next) }
  }
  function clearOutage() {
    failures = 0
    decisiveFailures = 0
  }
  async function runProbe(): Promise<void> {
    const result = await probe()
    if (result === 'ready' || result === 'booting' || result === 'auth_required') {
      clearOutage()
      lastSeenAt = Date.now()
      set(result)
      return
    }
    // The probe failed. The engine may still be up and serving: the awareness
    // socket proves liveness on its own, and a live engine means the path is
    // the problem, not the computer.
    if (opts.isAlive?.()) {
      clearOutage()
      lastSeenAt = Date.now()
      // Lift the curtain if it was up.
      if (state === 'unreachable' || state === 'updating') set('ready')
      return
    }
    failures += 1
    if (result === 'failure') decisiveFailures += 1
    // A timeout on its own is weak evidence, so it only counts once the
    // engine has gone unanswered for the whole grace window. A refused or
    // errored request is decisive: the engine is not there.
    const unanswered = Date.now() - lastSeenAt
    const decisive = decisiveFailures > 0 || unanswered >= grace
    if (failures >= threshold && decisive) {
      set(opts.isUpdating?.() ? 'updating' : 'unreachable')
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
