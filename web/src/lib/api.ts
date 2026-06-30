const BASE = ''

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
    window.location.href = '/login'
    throw new Error('unauthorized')
  }
  if (!res.ok) {
    const err: any = await res.json().catch(() => ({}))
    const stepDetail = Array.isArray(err?.steps)
      ? err.steps.filter((s: any) => s && !s.ok).map((s: any) =>
          s.output ? `${s.step}: ${s.output}` : s.step).join('; ')
      : ''
    const msg = err?.error || stepDetail || res.statusText || `HTTP ${res.status}`
    const e = new Error(msg) as Error & { payload?: unknown; status?: number }
    e.payload = err
    e.status = res.status
    throw e
  }
  return res.json()
}

export const api = {
  get: <T>(path: string) => request<T>('GET', path),
  post: <T>(path: string, body?: unknown) => request<T>('POST', path, body),
  patch: <T>(path: string, body?: unknown) => request<T>('PATCH', path, body),
  del: <T>(path: string) => request<T>('DELETE', path),
}
