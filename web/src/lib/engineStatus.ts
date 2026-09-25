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

/** Pure classification of one /api/startup-status response. */
export function classifyResponse(status: number, body: unknown): ProbeResult {
  if (status === 401 || status === 403) return 'auth_required'
  if (status < 200 || status >= 300) return 'failure'
  const ready = (body as { overall_ready?: unknown } | null)?.overall_ready
  return ready === false ? 'booting' : 'ready'
}

export async function probeEngine(fetchImpl: typeof fetch = fetch, timeoutMs = PROBE_TIMEOUT_MS): Promise<ProbeResult> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try {
    const res = await fetchImpl('/api/startup-status', { cache: 'no-store', redirect: 'manual', signal: controller.signal })
    let body: unknown = null
    try { body = await res.json() } catch { body = null }
    return classifyResponse(res.status, body)
  } catch {
    return 'failure'
  } finally {
    clearTimeout(timer)
  }
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

  function set(next: EngineState) {
    if (next !== state) { state = next; opts.onChange(next) }
  }
  async function tick(): Promise<void> {
    const result = await probe()
    if (result === 'failure') {
      failures += 1
      if (failures >= threshold) set(opts.isUpdating?.() ? 'updating' : 'unreachable')
    } else {
      failures = 0
      set(result)
    }
  }
  function schedule() {
    if (stopped) return
    const delay = state === 'ready' && failures === 0 ? HEALTHY_INTERVAL_MS : RECOVERY_INTERVAL_MS
    timer = setTimeout(async () => { await tick(); schedule() }, delay)
  }
  return {
    start() { if (!stopped) return; stopped = false; schedule() },
    stop() { stopped = true; if (timer) clearTimeout(timer); timer = null },
    /** Probe now (Retry button); resolves after the state is updated. */
    async retry() { if (timer) clearTimeout(timer); timer = null; await tick(); schedule() },
    get state() { return state },
  }
}
