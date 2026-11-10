// postMessage protocol shared between the PWA and the artifact bridge script
// (ciao/web/artifact_bridge.py) that runs inside the sandboxed artifact frame.
//
// The frame is an opaque origin: the parent cannot read its DOM and the frame
// cannot touch the session — postMessage in both directions is the only
// channel. Messages from the frame are untrusted (model-authored script could
// forge them); this module only ever turns them into pending comments the
// user reviews before sending, so forgery adds a note to their own composer
// and nothing more.

export interface ArtifactCommentAnchor {
  /** CSS selector path into the artifact document (bridge-generated). */
  selector: string
  /** Verbatim text of the selection (or element), whitespace-collapsed. */
  quote: string
  startOffset: number
  endOffset: number
  elementTag?: string
  wholeElement?: boolean
}

/** A box in the artifact frame's own viewport coordinates. */
export interface ArtifactRect {
  top: number
  left: number
  bottom: number
  right: number
}

/** A live text selection in the frame, as the bridge reports it. `rect` is the
 *  box of the selection's end, where the page floats its Comment button. */
export interface ArtifactSelection extends ArtifactCommentAnchor {
  rect: ArtifactRect
}

type FromFrame = { frame: 'ciao-artifact'; type: 'ciao:artifact-comment' }

// The frame is a thin agent (the model is Ghostwriter's frame.js): it reports
// selections, keystrokes and clicks, and paints highlights. The page owns the
// Comment button, the composer and the keyboard.
export type ArtifactCommentEvent =
  | (FromFrame & { action: 'ready' })
  // Alt+Click on an element: compose on the element straight away.
  | (FromFrame & {
      action: 'compose'
      selector: string
      quote: string
      startOffset: number
      endOffset: number
      elementTag?: string
      wholeElement?: boolean
      x: number
      y: number
    })
  // A new non-empty selection, or the same one re-sent after a scroll.
  | (FromFrame & {
      action: 'selection'
      selector: string
      quote: string
      startOffset: number
      endOffset: number
      elementTag?: string
      rect: ArtifactRect
    })
  | (FromFrame & { action: 'selection-cleared' })
  // A printable key or a paste inside the frame while a selection is reported.
  // The page's type-to-comment listener cannot hear keys typed into the frame.
  | (FromFrame & { action: 'type'; text: string; files?: File[] })
  | (FromFrame & { action: 'open'; id: string; x: number; y: number })

export function isArtifactCommentEvent(data: unknown): data is ArtifactCommentEvent {
  if (!data || typeof data !== 'object') return false
  const d = data as Record<string, unknown>
  return d.frame === 'ciao-artifact' && d.type === 'ciao:artifact-comment'
}

function isFiniteNumber(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v)
}

/** The rect of a `selection` message, or null when it is malformed (frame
 *  messages are untrusted). */
export function parseArtifactRect(raw: unknown): ArtifactRect | null {
  if (!raw || typeof raw !== 'object') return null
  const { top, left, bottom, right } = raw as Record<string, unknown>
  if (!isFiniteNumber(top) || !isFiniteNumber(left) || !isFiniteNumber(bottom) || !isFiniteNumber(right)) return null
  return { top, left, bottom, right }
}

/** Image files forwarded with a `type` message; anything else is dropped. */
export function imageFilesOf(raw: unknown): File[] {
  if (!Array.isArray(raw) || typeof File === 'undefined') return []
  return raw.filter((f): f is File => f instanceof File && f.type.startsWith('image/'))
}

/** Durable comment shape the bridge needs to re-highlight after a reload. */
export type ArtifactHighlight = {
  id: string
  selector: string
  quote: string
  startOffset?: number | null
  endOffset?: number | null
  wholeElement?: boolean
}

/** Compact UI label for an artifact comment chip ("h2 · 'Quote…'"). */
export function formatArtifactCommentLocation(a: {
  selector?: string | null
  elementTag?: string | null
  quote?: string | null
}): string {
  const tag = (a.elementTag || '').trim()
  if (tag) return tag
  const selector = (a.selector || '').trim()
  if (!selector) return ''
  // Last simple selector in the path, stripped of :nth-of-type(...) — close
  // enough for a chip label; the quote carries the real anchor.
  const last = selector.split('>').pop()?.trim() ?? ''
  return last.replace(/:nth-of-type\(\d+\)/g, '').trim()
}