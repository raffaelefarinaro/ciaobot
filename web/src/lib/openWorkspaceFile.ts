export type OpenWorkspaceFileResult =
  | { ok: true }
  | { ok: false; error: string }

/**
 * A dead markdown link whose target is a local file path is rendered as a
 * real button on the engine's own machine (see `lib/safeMarkdown.ts`), so
 * that the click has something to hit.
 *
 * The button lives inside markdown injected with `v-html`, so it cannot be a
 * Vue component: the click is handled by delegation, the same way the
 * code-block copy button is (`lib/codeCopy.ts`). Delegation is also what keeps
 * the button alive during streaming — a bubble's innerHTML is replaced on
 * every token, so a listener bound to an individual button node would die
 * with the node it was attached to.
 */
export const DEAD_LINK_OPENABLE_CLASS = 'markdown-dead-link--openable'

/** The attribute carrying the local path the button asks the engine to open. */
export const DEAD_LINK_PATH_ATTR = 'data-local-path'

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

/**
 * Ask the engine to open the path with the OS default application and report
 * a refusal.
 *
 * The button only exists on the engine's own machine — the renderer gates it
 * on `isLoopbackPage()` — so this handler needs no loopback check of its own:
 * `POST /api/workspace-open` refuses a remote caller on its own.
 */
async function openLocalLink(path: string, onFail: (message: string) => void): Promise<void> {
  const result = await openWorkspaceFileExternally(path)
  if (!result.ok) onFail(result.error)
}

/**
 * Delegated click handler for the openable dead link. Returns true when the
 * event hit the button, so the caller can skip its other click handling for
 * that event.
 */
export function handleOpenLocalFileClick(event: Event, onFail: (message: string) => void): boolean {
  const target = event.target
  if (!(target instanceof Element)) return false
  const button = target.closest(`button.${DEAD_LINK_OPENABLE_CLASS}`)
  if (!(button instanceof HTMLButtonElement)) return false
  event.preventDefault()
  event.stopPropagation()
  void openLocalLink(button.getAttribute(DEAD_LINK_PATH_ATTR) || '', onFail)
  return true
}
