// The page's half of selecting text inside an HTML artifact.
//
// The artifact frame is an opaque origin, so its selection is not something
// the page can read: the bridge script inside it reports each selection (the
// anchor plus the box of its end, in frame-viewport coordinates). This turns
// that report into the position of the page's own floating "Comment" button —
// the same button a Markdown selection gets, placed by the same
// `placeCommentTrigger` — and holds the anchor until the user commits to it.
//
// The selection itself stays reported while its end is scrolled out of the
// frame; only the button hides, so typing still opens a comment on it.
import { ref, shallowRef, type Ref } from 'vue'
import type { ArtifactSelection } from '../lib/artifactBridge'
import { placeCommentTrigger, type Anchor } from './useFileComments'

export type UseArtifactSelectionOptions = {
  /** The element the trigger is positioned against (modal frame or panel main). */
  containerEl: Ref<HTMLElement | undefined>
  /** The artifact iframe, whose viewport the reported boxes are relative to. */
  frameEl: () => HTMLIFrameElement | null | undefined
}

export function useArtifactSelection(options: UseArtifactSelectionOptions) {
  const selection = shallowRef<ArtifactSelection | null>(null)
  /** Container-relative position of the Comment button, or null to hide it. */
  const trigger = ref<Anchor | null>(null)

  function place(): void {
    const s = selection.value
    const container = options.containerEl.value
    const frame = options.frameEl()
    if (!s || !container || !frame) {
      trigger.value = null
      return
    }
    const f = frame.getBoundingClientRect()
    const end = {
      top: f.top + s.rect.top,
      bottom: f.top + s.rect.bottom,
      left: f.left + s.rect.left,
      right: f.left + s.rect.right,
    }
    const visible = end.bottom > f.top && end.top < f.bottom && end.right > f.left && end.left < f.right
    trigger.value = visible ? placeCommentTrigger(end, container) : null
  }

  function onSelection(s: ArtifactSelection): void {
    selection.value = s
    place()
  }

  function clear(): void {
    selection.value = null
    trigger.value = null
  }

  /** Hand over the reported selection for composing, and forget it. */
  function take(): { selection: ArtifactSelection; trigger: Anchor | null } | null {
    const s = selection.value
    if (!s) return null
    const at = trigger.value
    clear()
    return { selection: s, trigger: at }
  }

  return { selection, trigger, onSelection, clear, take }
}
