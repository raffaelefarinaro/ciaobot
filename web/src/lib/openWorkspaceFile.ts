export type OpenWorkspaceFileResult =
  | { ok: true }
  | { ok: false; error: string }

/** Ask the local Ciao server to open a file with the OS default application. */
export async function openWorkspaceFileExternally(path: string): Promise<OpenWorkspaceFileResult> {
  const cleaned = path.replace(/:\d+$/, '').trim()
  if (!cleaned) return { ok: false, error: 'No file selected.' }
  try {
    const resp = await fetch('/api/workspace-open', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: cleaned }),
    })
    if (resp.ok) return { ok: true }
    const body = await resp.json().catch(() => ({}))
    const message = typeof body.error === 'string' ? body.error : `Failed (HTTP ${resp.status})`
    return { ok: false, error: message }
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : String(e) }
  }
}
