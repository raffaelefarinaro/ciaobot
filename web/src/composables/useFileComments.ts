import { computed, nextTick, ref, type ComputedRef, type Ref } from 'vue'
import { useProjectStore } from '../stores/projects'
import { formatCommentLocation } from '../lib/commentContext'
import type { CsvCellComment, CsvCellRef } from '../components/CsvViewer.vue'

// Shared file-comment subsystem for FileViewerModal and PinnedFilePanel.
// Both surfaces show durable comments as text/cell highlights and let the
// user select a range, open a compose popover, and attach a note that rides
// along on the next chat message. The anchor math, selection tracking, and
// comment CRUD were previously copy-pasted between the two components; the
// only real differences are which DOM node anchors are measured against
// (the modal frame vs the panel main) and the surface-specific highlight
// guards, so those are injected.
//
// The highlight application itself stays in the components: the two
// `applyHighlights` implementations have different guard sets (commentable
// state, image/pdf/CSV exclusions) and are wired in via `setApplyHighlights`
// so the shared save/cancel paths re-render highlights per surface.

export type Anchor = { top: number; left: number }
export type LineRange = { start: number; end: number } | null
export type CellRef = { row: number; colIndex: number; colHeader: string } | null

/** Durable comment as stored by the projects store (structural subset). */
export type FileComment = {
  id: string
  path: string
  selection: string
  comment: string
  lineStart?: number | null
  lineEnd?: number | null
  colIndex?: number | null
  colHeader?: string | null
  images?: string[]
  createdAt: string
}

export type CommentDraft = {
  selection: string
  text: string
  lines: LineRange
  cell: CellRef
}

export type UseFileCommentsOptions = {
  /** Clean workspace path of the current file (no `:line` suffix). */
  path: () => string
  /** Full source content of the current file, for selection→line mapping. */
  content: () => string
  /** Durable comments for the current file (store-backed). */
  commentsForFile: ComputedRef<FileComment[]>
  /** True when selection should be tracked for commenting. */
  isCommentable: ComputedRef<boolean>
  /** The element anchors are positioned against (modal frame or panel main). */
  containerEl: Ref<HTMLElement | undefined>
  bodyEl: Ref<HTMLElement | undefined>
  mdEl: Ref<HTMLElement | undefined>
  preEl: Ref<HTMLElement | undefined>
  preCodeEl: Ref<HTMLElement | undefined>
  /** Close a pinned read popover when a compose popover opens (PinnedFilePanel). */
  closeReadPopover?: () => void
}

export function useFileComments(options: UseFileCommentsOptions) {
  const projectsStore = useProjectStore()
  const { commentsForFile, containerEl, bodyEl } = options

  // ── Line & cell highlight state ────────────────────────────────────
  const DRAFT_COMMENT_ID = '__draft__'

  const lineCommentMap = computed(() => {
    const map = new Map<number, string>()
    const draft = commentDraft.value
    if (draft?.lines) {
      const end = draft.lines.end || draft.lines.start
      for (let l = draft.lines.start; l <= end; l++) {
        map.set(l, DRAFT_COMMENT_ID)
      }
    }
    for (const c of commentsForFile.value) {
      if (!c.lineStart) continue
      const end = c.lineEnd || c.lineStart
      for (let l = c.lineStart; l <= end; l++) {
        if (!map.has(l)) map.set(l, c.id)
      }
    }
    return map
  })

  function isHighlightedLine(line: number): boolean {
    return lineCommentMap.value.has(line)
  }

  function commentIdForLine(line: number): string | undefined {
    return lineCommentMap.value.get(line)
  }

  const csvCellComments = computed<CsvCellComment[]>(() =>
    commentsForFile.value
      .filter(c => c.lineStart != null && c.colIndex != null)
      .map(c => ({
        id: c.id,
        row: c.lineStart as number,
        colIndex: c.colIndex as number,
      })),
  )

  // ── Selection → comment draft state ────────────────────────────────
  const selectionAnchor = ref<Anchor | null>(null)
  const draftAnchor = ref<Anchor | null>(null)
  const commentDraft = ref<CommentDraft | null>(null)
  const composeText = computed({
    get: () => commentDraft.value?.text ?? '',
    set: (v: string) => {
      if (commentDraft.value) commentDraft.value.text = v
    },
  })
  let lastSelectionText = ''
  let lastSelectionLines: LineRange = null
  let lastSelectionRange: Range | null = null
  let lastCsvCell: CsvCellRef | null = null

  // ── Edit-existing-comment state ────────────────────────────────────
  const commentDraftImages = ref<string[]>([])
  const editingCommentId = ref<string | null>(null)
  const editDraftText = ref('')
  const editingCommentImages = ref<string[]>([])
  const editAnchor = ref<Anchor | null>(null)

  // The components keep their own (divergent) highlight application so its
  // per-surface guards (commentable state, image/PDF/CSV exclusions) stay
  // exact; this just routes the shared save/cancel paths back through it.
  let applyHighlights: (() => void) | null = null
  function setApplyHighlights(fn: () => void): void { applyHighlights = fn }
  function reapplyHighlights(): void {
    nextTick(() => { applyHighlights?.() })
  }

  // ── Anchor math ────────────────────────────────────────────────────
  // All anchors are positioned against `containerEl` (the modal frame in
  // FileViewerModal, the panel main in PinnedFilePanel). CommentComposePopover
  // clamps itself to the viewport afterwards, so here we only translate.
  function toViewportAnchor(local: Anchor): Anchor {
    const container = containerEl.value
    if (!container) return local
    const r = container.getBoundingClientRect()
    return { top: r.top + local.top, left: r.left + local.left }
  }

  function anchorFromElement(el: HTMLElement): Anchor | null {
    const container = containerEl.value
    const body = bodyEl.value
    if (!container || !body) return null
    const rect = el.getBoundingClientRect()
    const containerRect = container.getBoundingClientRect()
    const popWidth = 280
    const pad = 8
    const top = Math.min(Math.max(rect.bottom - containerRect.top + 6, pad), Math.max(pad, container.clientHeight - 60))
    const left = Math.min(Math.max(rect.left - containerRect.left, pad), Math.max(pad, container.clientWidth - popWidth - pad))
    return { top, left }
  }

  function anchorFromCellRect(rect: DOMRect): Anchor | null {
    const container = containerEl.value
    const body = bodyEl.value
    if (!container || !body) return null
    const containerRect = container.getBoundingClientRect()
    const bodyRect = body.getBoundingClientRect()
    const visible = rect.bottom > bodyRect.top
      && rect.top < bodyRect.bottom
      && rect.right > bodyRect.left
      && rect.left < bodyRect.right
    if (!visible) return null
    const top = Math.min(Math.max(rect.bottom - containerRect.top + 6, 8), containerRect.height - 48)
    const left = Math.min(Math.max(rect.right - containerRect.left - 96, 8), containerRect.width - 110)
    return { top, left }
  }

  // ── Text-selection anchoring ───────────────────────────────────────
  // Convert a (container, offset) Range endpoint into a character offset
  // relative to the start of `root.textContent`. Walks via a fresh Range +
  // `toString().length`, which handles nested elements (links, em, strong)
  // transparently. Returns null when the endpoint isn't inside `root`.
  function charOffsetFrom(root: Element, container: Node, offset: number): number | null {
    if (!root.contains(container) && root !== container) return null
    const r = document.createRange()
    r.selectNodeContents(root)
    try {
      r.setEnd(container, offset)
    } catch {
      return null
    }
    return r.toString().length
  }

  // Count 1-indexed line number of `idx` within `text` (idx points at the
  // character whose line we want — newlines before it are counted, the char
  // at idx itself is not).
  function lineAt(text: string, idx: number): number {
    let line = 1
    const limit = Math.min(idx, text.length)
    for (let i = 0; i < limit; i++) {
      if (text.charCodeAt(i) === 10) line++
    }
    return line
  }

  // ── Markdown text highlighting ─────────────────────────────────────
  function clearHighlights(root: HTMLElement): void {
    const existing = root.querySelectorAll('.comment-highlight')
    for (const el of Array.from(existing)) {
      const parent = el.parentNode
      if (!parent) continue
      parent.replaceChild(document.createTextNode(el.textContent || ''), el)
      parent.normalize()
    }
  }

  function highlightInMarkdown(root: HTMLElement, selection: string, commentId: string): boolean {
    const text = selection.trim()
    if (!text) return false
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
    const nodes: Text[] = []
    let node: Node | null
    while ((node = walker.nextNode())) nodes.push(node as Text)
    if (!nodes.length) return false

    let fullText = ''
    const offsets: { node: Text; start: number; end: number }[] = []
    for (const n of nodes) {
      const start = fullText.length
      fullText += n.textContent || ''
      offsets.push({ node: n, start, end: fullText.length })
    }

    const idx = fullText.indexOf(text)
    if (idx === -1) return false

    // Wrap the matching slice of each overlapping text node in its own span.
    // Using one Range across multiple nodes fails with range.surroundContents
    // when the range spans structural boundaries (table cells, paragraphs,
    // list items) or inline element boundaries (<strong>, <a>, etc.).
    //
    // Instead, we split each text node at the match boundaries using
    // splitText(), then replace the middle portion with a highlight span.
    // This avoids the common-ancestor restriction and works across any
    // element boundary because we only ever manipulate text nodes.
    //
    // We also skip whitespace-only text nodes so highlights don't bleed
    // into empty gaps between paragraphs or list items.
    //
    // Iterate in reverse so DOM mutations don't shift the offsets we still
    // need to act on.
    const matchStart = idx
    const matchEnd = idx + text.length
    let success = false
    for (let i = offsets.length - 1; i >= 0; i--) {
      const o = offsets[i]
      if (o.end <= matchStart || o.start >= matchEnd) continue
      const localStart = Math.max(0, matchStart - o.start)
      const localEnd = Math.min(o.end - o.start, matchEnd - o.start)
      if (localStart >= localEnd) continue

      const textNode = o.node
      const slice = textNode.textContent?.slice(localStart, localEnd) || ''
      if (!slice.trim()) continue  // Skip whitespace-only gaps

      try {
        // splitText mutates the tree, which is the point; the tail node itself
        // is not needed here.
        textNode.splitText(localEnd)
        const mid = textNode.splitText(localStart)
        const span = document.createElement('span')
        span.className = 'comment-highlight'
        span.dataset.commentId = commentId
        mid.parentNode?.replaceChild(span, mid)
        span.appendChild(mid)
        success = true
      } catch {
        // Skip this node; the others may still wrap successfully.
      }
    }
    return success
  }

  function commentLineLabel(c: {
    lineStart?: number | null
    lineEnd?: number | null
    colIndex?: number | null
    colHeader?: string | null
  }): string {
    return formatCommentLocation(c)
  }

  // ── Comment CRUD ───────────────────────────────────────────────────
  function onCsvCellSelect(cell: CsvCellRef, rect: DOMRect): void {
    if (commentDraft.value) return
    lastCsvCell = cell
    lastSelectionText = cell.value
    lastSelectionLines = { start: cell.row, end: cell.row }
    lastSelectionRange = null
    selectionAnchor.value = anchorFromCellRect(rect)
  }

  function cancelComment(): void {
    commentDraft.value = null
    draftAnchor.value = null
    commentDraftImages.value = []
    lastSelectionText = ''
    lastSelectionLines = null
    lastSelectionRange = null
    lastCsvCell = null
    reapplyHighlights()
  }

  async function handleDraftImageUpload(e: Event): Promise<void> {
    const input = e.target as HTMLInputElement
    if (!input.files?.length) return
    const chatId = projectsStore.activeChatId
    if (!chatId) return
    try {
      const refs = await projectsStore.uploadImageRefs(chatId, Array.from(input.files))
      commentDraftImages.value.push(...refs)
    } catch (err) {
      console.error('Comment image upload failed:', err)
    }
    input.value = ''
  }

  function removeDraftImage(index: number): void {
    commentDraftImages.value.splice(index, 1)
  }

  // ── Edit existing comment ──────────────────────────────────────────
  function startEditComment(c: { id: string; comment: string; images?: string[] }, anchor?: Anchor | null): void {
    editingCommentId.value = c.id
    editDraftText.value = c.comment
    editingCommentImages.value = c.images ? [...c.images] : []

    if (anchor) {
      editAnchor.value = anchor
    } else {
      const el = bodyEl.value?.querySelector(`[data-comment-id="${c.id}"]`) as HTMLElement | null
      const local = el ? anchorFromElement(el) : null
      editAnchor.value = local ? toViewportAnchor(local) : null
    }
  }

  function cancelEditComment(): void {
    editingCommentId.value = null
    editDraftText.value = ''
    editingCommentImages.value = []
    editAnchor.value = null
  }

  function removeEditImage(index: number): void {
    editingCommentImages.value.splice(index, 1)
  }

  return {
    DRAFT_COMMENT_ID,
    lineCommentMap,
    isHighlightedLine,
    commentIdForLine,
    csvCellComments,
    selectionAnchor,
    draftAnchor,
    commentDraft,
    composeText,
    commentDraftImages,
    editingCommentId,
    editDraftText,
    editingCommentImages,
    editAnchor,
    // Plain (non-reactive) selection trackers, exposed as accessors so the
    // shared functions and the components read/write the same variables.
    get lastSelectionText() { return lastSelectionText },
    set lastSelectionText(v: string) { lastSelectionText = v },
    get lastSelectionLines() { return lastSelectionLines },
    set lastSelectionLines(v: LineRange) { lastSelectionLines = v },
    get lastSelectionRange() { return lastSelectionRange },
    set lastSelectionRange(v: Range | null) { lastSelectionRange = v },
    get lastCsvCell() { return lastCsvCell },
    set lastCsvCell(v: CsvCellRef | null) { lastCsvCell = v },
    setApplyHighlights,
    toViewportAnchor,
    anchorFromElement,
    anchorFromCellRect,
    charOffsetFrom,
    lineAt,
    clearHighlights,
    highlightInMarkdown,
    commentLineLabel,
    onCsvCellSelect,
    cancelComment,
    handleDraftImageUpload,
    removeDraftImage,
    startEditComment,
    cancelEditComment,
    removeEditImage,
  }
}
