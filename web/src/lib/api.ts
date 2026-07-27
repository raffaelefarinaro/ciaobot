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
  if (!res.ok) {
    const err: any = await res.json().catch(() => ({}))
    const stepDetail = Array.isArray(err?.steps)
      ? err.steps.filter((s: any) => s && !s.ok).map((s: any) =>
          s.output ? `${s.step}: ${s.output}` : s.step).join('; ')
      : ''
    const msg = err?.error || stepDetail || res.statusText || `HTTP ${res.status}`
    throw new ApiError(msg, { status: res.status, payload: err })
  }
  return res.json()
}

export const api = {
  get: <T>(path: string) => request<T>('GET', path),
  post: <T>(path: string, body?: unknown) => request<T>('POST', path, body),
  patch: <T>(path: string, body?: unknown) => request<T>('PATCH', path, body),
  del: <T>(path: string) => request<T>('DELETE', path),
}
