const BASE = ''

export class ApiError extends Error {
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

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const opts: RequestInit = {
    method,
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
  }
  if (body !== undefined) {
    opts.body = JSON.stringify(body)
  }
  const res = await fetch(`${BASE}${path}`, opts)
  if (res.status === 401) {
    // Never hard-reload while already on /login — that caused a refresh loop
    // when client mode's /api/auth/check returns 401 (host password needed).
    const isAuthProbe = path === '/api/auth/check' || path === '/api/auth'
    if (!onLoginPage() && !isAuthProbe) {
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
    let err: any = {}
    if (looksLikeJson) {
      try { err = JSON.parse(raw) } catch { err = {} }
    }
    // A server running older code answers an unknown route with the SPA shell
    // (HTML) or a 404. Only those warrant the redeploy hint — a plain-text 500
    // from Starlette, or a 502/503 from a proxy, is a real failure and has to
    // surface as itself, or the user redeploys a healthy build chasing it.
    if (looksLikeHtml || res.status === 404) {
      throw new ApiError(
        `API route ${path} is not available on the running server yet. Use Settings → Deploy, then restart Ciaobot.`,
        { status: res.status, payload: err },
      )
    }
    const stepDetail = Array.isArray(err?.steps)
      ? err.steps.filter((s: any) => s && !s.ok).map((s: any) =>
          s.output ? `${s.step}: ${s.output}` : s.step).join('; ')
      : ''
    const bodyDetail = !looksLikeJson ? raw.trim().slice(0, 200) : ''
    const msg = err?.error || stepDetail || bodyDetail || res.statusText || `HTTP ${res.status}`
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
