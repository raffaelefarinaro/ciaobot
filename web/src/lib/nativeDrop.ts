/**
 * "Use in place" for a file dropped on a chat, on the machine that runs the
 * engine. Pure helpers: no Vue, no store.
 *
 * A browser never tells a page where a dropped file lives (`File` carries no
 * path), so a page cannot reference a file by path and the engine never takes
 * a path from one. Only the Ciaobot for Mac app, showing its own local engine,
 * can: it registers the `ciaobotNativeDrop` message handler, reads the file
 * URLs of the drop (or of an open panel) natively, writes them into a
 * single-use grant the engine consumes, and answers with the grant id and the
 * display names only. Every other host attaches a copy.
 */

/** Where this page runs, as far as file drops are concerned. */
export type DropHost =
  /** The Mac app on its own engine: attach a copy, or use in place. */
  | 'native'
  /** A browser tab on the engine's machine: attach a copy; a tab cannot see paths. */
  | 'loopback'
  /** Any other device: attach a copy. */
  | 'remote'

/** The two places a file can be dropped while the zones are shown. */
export type DropZone = 'copy' | 'in-place'

export interface NativeDropGrant {
  grantId: string
  names: string[]
}

interface NativeDropHandler {
  postMessage(body: unknown): Promise<unknown>
}

type HostWindow = {
  location?: { hostname?: string }
  webkit?: { messageHandlers?: Record<string, NativeDropHandler | undefined> }
}

const HANDLER = 'ciaobotNativeDrop'
const LOOPBACK_HOSTS = new Set(['localhost', '127.0.0.1', '[::1]', '::1'])
const GRANT_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/

function handler(win: HostWindow | undefined): NativeDropHandler | null {
  const candidate = win?.webkit?.messageHandlers?.[HANDLER]
  return candidate && typeof candidate.postMessage === 'function' ? candidate : null
}

/** Which drop actions this page can offer. */
export function dropHost(win: HostWindow | undefined = globalThis as unknown as HostWindow): DropHost {
  if (handler(win)) return 'native'
  return LOOPBACK_HOSTS.has(win?.location?.hostname || '') ? 'loopback' : 'remote'
}

/** Read the app's reply: a grant, or null when there was nothing to grant. */
export function parseNativeDropGrant(value: unknown): NativeDropGrant | null {
  if (value === null || value === undefined) return null
  const reply = value as { grantId?: unknown; names?: unknown }
  if (typeof reply.grantId !== 'string' || !GRANT_ID_RE.test(reply.grantId)) {
    throw new Error('The app answered with an invalid file grant.')
  }
  const names = Array.isArray(reply.names)
    ? reply.names.filter((name): name is string => typeof name === 'string')
    : []
  return { grantId: reply.grantId, names }
}

/**
 * Ask the app for a grant: `grant` for the drop the page just received,
 * `pick` to open its panel for files and folders. Null means no drop or a
 * cancelled panel; a refusal or a missing bridge throws.
 */
export async function requestNativeDropGrant(
  op: 'grant' | 'pick',
  win: HostWindow | undefined = globalThis as unknown as HostWindow,
): Promise<NativeDropGrant | null> {
  const bridge = handler(win)
  if (!bridge) throw new Error('Only the Ciaobot app can use a file where it is.')
  let reply: unknown
  try {
    reply = await bridge.postMessage({ op })
  } catch {
    throw new Error('The app would not hand over the files.')
  }
  return parseNativeDropGrant(reply)
}

interface ZoneBox {
  zone: DropZone
  rect: Pick<DOMRect, 'left' | 'right' | 'top' | 'bottom'> | null | undefined
}

/** The zone under a drag point; null outside both (a plain drop attaches a copy). */
export function dropZoneAt(x: number, y: number, zones: ZoneBox[]): DropZone | null {
  for (const { zone, rect } of zones) {
    if (rect && x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom) return zone
  }
  return null
}
