/** The JSON body an error response may carry. Every field is best-effort. */
interface ApiErrorBody {
  error?: string
  steps?: Array<{ step?: string; ok?: boolean; output?: string }>
  [key: string]: unknown
}

class ApiError extends Error {
  status?: number
  payload?: unknown

  constructor(message: string, opts?: { status?: number; payload?: unknown }) {
    super(message)
    this.name = 'ApiError'
    this.status = opts?.status
    this.payload = opts?.payload
  }
}

function onLoginPage(): boolean {
  return window.location.pathname === '/login' || window.location.pathname.startsWith('/login/')
}

function onDevicePage(): boolean {
  // /device is the escape hatch out of client mode. Bouncing it to a login
  // screen — which authenticates against the very host the user is trying to
  // leave — would strand a client whose host is gone.
  return window.location.pathname === '/device' || window.location.pathname.startsWith('/device/')
}

// Paths handed to the api wrapper are built from server state (chat ids, file
// names). Every request resolves against the page origin and refuses anything
// that would land on another host or scheme, so a crafted value can never
// turn a same-origin API call into a cross-origin one.

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  // The barrier sits here, next to the sink: resolve the caller's path against this page's origin and refuse anything
  // that would land on another host or scheme before a request can exist.
  const target = new URL(path, window.location.origin)
  if (!/^https?:$/.test(target.protocol) || target.origin !== window.location.origin) {
    throw new ApiError(`Blocked non-same-origin API path: ${path}`)
  }
  const opts: RequestInit = {
    method,
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
  }
  if (body !== undefined) {
    opts.body = JSON.stringify(body)
  }
  const res = await fetch(`${target.pathname}${target.search}${target.hash}`, opts)
  if (res.status === 401) {
    // Never hard-reload while already on /login — that caused a refresh loop
    // when client mode's /api/auth/check returns 401 (host password needed).
    const isAuthProbe = path === '/api/auth/check' || path === '/api/auth'
    if (!onLoginPage() && !onDevicePage() && !isAuthProbe) {
      window.location.href = '/login'
    }
    const payload = await res.json().catch(() => ({}))
    throw new ApiError(
      (payload as { error?: string })?.error || 'unauthorized',
      { status: 401, payload },
    )
  }
  const contentType = (res.headers.get('content-type') || '').toLowerCase()
  const raw = await res.text()
  const looksLikeHtml = raw.trimStart().startsWith('<!') || raw.trimStart().startsWith('<html')
  const looksLikeJson = contentType.includes('application/json') || raw.trimStart().startsWith('{') || raw.trimStart().startsWith('[')
  if (!res.ok) {
    // The error body is whatever the server sent, so it is read through a
    // narrow shape rather than trusted: `error` for the message and `steps`
    // for the deploy/preflight routes that report per-step failures.
    let err: ApiErrorBody = {}
    if (looksLikeJson) {
      try {
        const parsed: unknown = JSON.parse(raw)
        if (parsed && typeof parsed === 'object') err = parsed as ApiErrorBody
      } catch { err = {} }
    }
    // A server running older code answers an unknown route with the SPA shell
    // (HTML), or a 404 with no JSON error to explain itself. Only those warrant
    // the redeploy hint: a real `404 {"error": "not found"}` has a reason worth
    // showing, and a plain-text 500 or a proxy 502/503 is a live failure — hide
    // either behind "redeploy" and the user goes and redeploys a healthy build.
    if (looksLikeHtml || (res.status === 404 && !err.error)) {
      throw new ApiError(
        `API route ${path} is not available on the running server yet. Use Settings → Deploy, then restart Ciaobot.`,
        { status: res.status, payload: err },
      )
    }
    const stepDetail = Array.isArray(err.steps)
      ? err.steps.filter((step) => step && !step.ok).map((step) =>
          step.output ? `${step.step}: ${step.output}` : step.step).join('; ')
      : ''
    const bodyDetail = !looksLikeJson ? raw.trim().slice(0, 200) : ''
    const msg = err.error || stepDetail || bodyDetail || res.statusText || `HTTP ${res.status}`
    throw new ApiError(msg, { status: res.status, payload: err })
  }
  if (looksLikeHtml || !looksLikeJson) {
    throw new ApiError(
      `API route ${path} is not available on the running server yet. Use Settings → Deploy, then restart Ciaobot.`,
      { status: res.status },
    )
  }
  try {
    return JSON.parse(raw) as T
  } catch {
    throw new ApiError(`Invalid JSON from ${path}`, { status: res.status })
  }
}

export const api = {
  get: <T>(path: string) => request<T>('GET', path),
  post: <T>(path: string, body?: unknown) => request<T>('POST', path, body),
  patch: <T>(path: string, body?: unknown) => request<T>('PATCH', path, body),
  del: <T>(path: string) => request<T>('DELETE', path),
}
