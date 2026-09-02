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

export type ArtifactCommentEvent =
  | { frame: 'ciao-artifact'; type: 'ciao:artifact-comment'; action: 'ready' }
  | {
      frame: 'ciao-artifact'
      type: 'ciao:artifact-comment'
      action: 'compose'
      selector: string
      quote: string
      startOffset: number
      endOffset: number
      elementTag?: string
      wholeElement?: boolean
      x: number
      y: number
    }
  | {
      frame: 'ciao-artifact'
      type: 'ciao:artifact-comment'
      action: 'open'
      id: string
      x: number
      y: number
    }

export function isArtifactCommentEvent(data: unknown): data is ArtifactCommentEvent {
  if (!data || typeof data !== 'object') return false
  const d = data as Record<string, unknown>
  return d.frame === 'ciao-artifact' && d.type === 'ciao:artifact-comment'
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