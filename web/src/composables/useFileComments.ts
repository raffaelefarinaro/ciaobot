import { computed, nextTick, ref, type ComputedRef, type Ref } from 'vue'
import { useProjectStore } from '../stores/projects'
import { formatCommentLocation } from '../lib/commentContext'
import { cleanCommentSelection, findCommentTextMatch, highlightCommentText } from '../lib/commentHighlight'
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
  /** Scroll to a comment's highlight (kept per-surface: the two differ). */
  scrollToHighlight: (id: string) => void
}

export function useFileComments(options: UseFileCommentsOptions) {
  const projectsStore = useProjectStore()
  const {
    path, content, commentsForFile, isCommentable,
    containerEl, bodyEl, mdEl, preEl, preCodeEl, closeReadPopover, scrollToHighlight,
  } = options

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

  // Compute a {start, end} source line range for the active selection.
  // Two strategies:
  //   • <pre> branch: text-node offsets map 1:1 onto the source, so we
  //     read range.startOffset/endOffset directly via charOffsetFrom and
  //     count newlines. This is exact.
  //   • markdown branch: rendered DOM doesn't map onto source, so we
  //     substring-search the source markdown for the first ~50 chars (start
  //     line) and the last ~50 chars (end line) of the selection. Falls back
  //     to a single line when the second search misses.
  function computeSelectionLines(range: Range, selectionText: string): LineRange {
    const src = content()
    if (!src) return null

    // Plain-text branch: exact mapping via offsets into the <code> root.
    const codeRoot = preCodeEl.value
    if (codeRoot && codeRoot.contains(range.startContainer)) {
      const startOff = charOffsetFrom(codeRoot, range.startContainer, range.startOffset)
      const endOff = charOffsetFrom(codeRoot, range.endContainer, range.endOffset)
      if (startOff != null && endOff != null) {
        const a = Math.min(startOff, endOff)
        const b = Math.max(startOff, endOff)
        const start = lineAt(src, a)
        // For end, look at the last char of the selection (b - 1) so a
        // selection ending at the start of a line doesn't bleed into it.
        const end = b > a ? lineAt(src, b - 1) : start
        return { start, end: Math.max(end, start) }
      }
    }

    // Markdown branch: best-effort substring lookup.
    const trimmed = selectionText.trim()
    if (!trimmed) return null
    const sourceMatch = findCommentTextMatch(src, trimmed)
    if (sourceMatch) {
      const start = lineAt(src, sourceMatch.start)
      const end = lineAt(src, Math.max(sourceMatch.start, sourceMatch.end - 1))
      return { start, end: Math.max(end, start) }
    }
    const head = trimmed.slice(0, 60)
    let startIdx = src.indexOf(head)
    if (startIdx === -1) {
      // Try a shorter prefix from the first line of the rendered selection,
      // which is usually the most stable token to find in source.
      const firstLine = trimmed.split(/\n/, 1)[0].trim().slice(0, 30)
      if (firstLine.length >= 4) startIdx = src.indexOf(firstLine)
    }
    if (startIdx === -1) return null
    const start = lineAt(src, startIdx)

    const tail = trimmed.slice(-60).trim()
    if (tail.length >= 4 && tail !== head) {
      // Search starting after the head match so identical phrases earlier in
      // the doc don't pull the end line backwards.
      const tailIdx = src.indexOf(tail, startIdx)
      if (tailIdx !== -1) {
        const end = lineAt(src, tailIdx + tail.length - 1)
        return { start, end: Math.max(end, start) }
      }
    }
    return { start, end: start }
  }

  // Position the floating comment trigger at the END of the selection
  // (where the cursor lands after a drag-select), not at the bounding box of
  // the whole range. Left is clamped so the pill never spills past the
  // container's right edge (a no-op in the wide modal, required in the
  // narrow pinned panel).
  function updateSelectionAnchorFromRange(range: Range): void {
    const container = containerEl.value
    const body = bodyEl.value
    if (!container || !body) {
      selectionAnchor.value = null
      return
    }

    const rects = range.getClientRects()
    const endRect = rects.length ? rects[rects.length - 1] : range.getBoundingClientRect()
    const bodyRect = body.getBoundingClientRect()
    const visible = endRect.bottom > bodyRect.top
      && endRect.top < bodyRect.bottom
      && endRect.right > bodyRect.left
      && endRect.left < bodyRect.right
    if (!visible) {
      selectionAnchor.value = null
      return
    }

    const containerRect = container.getBoundingClientRect()
    const triggerWidth = 110  // approximate; matches the rendered "💬 Comment" pill
    const panelPad = 8
    const top = endRect.bottom - containerRect.top + 2
    const idealLeft = endRect.right - containerRect.left + 6
    const maxLeft = container.clientWidth - triggerWidth - panelPad
    const left = Math.max(panelPad, Math.min(idealLeft, maxLeft))
    selectionAnchor.value = { top, left }
  }

  // Track the live selection for the floating trigger. Only reacts to
  // selections inside the rendered file view — selecting text in chrome
  // (path subtitle, headers) shouldn't trigger the comment UI. While the
  // composer is open the selection has been "captured" — don't keep
  // retracking it (the textarea steals focus and would clear it).
  function onSelectionChange(): void {
    if (!isCommentable.value) {
      lastSelectionRange = null
      selectionAnchor.value = null
      return
    }
    if (commentDraft.value) return
    const sel = window.getSelection()
    if (!sel || sel.isCollapsed || sel.rangeCount === 0) {
      lastSelectionRange = null
      selectionAnchor.value = null
      return
    }
    const range = sel.getRangeAt(0)
    const targets: (HTMLElement | undefined)[] = [mdEl.value, preEl.value, preCodeEl.value]
    const inside = targets.some(
      el => el && el.contains(range.startContainer) && el.contains(range.endContainer)
    )
    if (!inside) {
      lastSelectionRange = null
      selectionAnchor.value = null
      return
    }
    const text = cleanCommentSelection(sel.toString().trim())
    if (!text) {
      lastSelectionRange = null
      selectionAnchor.value = null
      return
    }
    lastSelectionText = text
    lastSelectionLines = computeSelectionLines(range, text)
    lastSelectionRange = range.cloneRange()
    updateSelectionAnchorFromRange(range)
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

  function deleteFileComment(id: string): void {
    projectsStore.removeFileComment(path(), id)
    reapplyHighlights()
  }

  function onCsvCellActivate(cell: CsvCellRef): void {
    const match = commentsForFile.value.find(
      c => c.lineStart === cell.row && c.colIndex === cell.colIndex,
    )
    if (match) scrollToHighlight(match.id)
  }

  function openCommentForSelection(): void {
    if (!selectionAnchor.value || !lastSelectionText) return
    closeReadPopover?.()
    draftAnchor.value = toViewportAnchor(selectionAnchor.value)
    commentDraft.value = {
      selection: lastSelectionText,
      text: '',
      lines: lastSelectionLines,
      cell: null,
    }
    commentDraftImages.value = []
    selectionAnchor.value = null
    lastSelectionRange = null
    lastCsvCell = null
    window.getSelection()?.removeAllRanges()
    reapplyHighlights()
  }

  function openCommentForCsvCell(): void {
    if (!selectionAnchor.value || !lastCsvCell) return
    closeReadPopover?.()
    draftAnchor.value = toViewportAnchor(selectionAnchor.value)
    commentDraft.value = {
      selection: lastCsvCell.value,
      text: '',
      lines: { start: lastCsvCell.row, end: lastCsvCell.row },
      cell: {
        row: lastCsvCell.row,
        colIndex: lastCsvCell.colIndex,
        colHeader: lastCsvCell.colHeader,
      },
    }
    commentDraftImages.value = []
    selectionAnchor.value = null
    lastSelectionRange = null
    reapplyHighlights()
  }

  function saveComment(): void {
    const draft = commentDraft.value
    if (!draft) return
    const note = draft.text.trim()
    if (!note) return
    projectsStore.addPendingComment({
      path: path(),
      selection: draft.selection,
      comment: note,
      lineStart: draft.lines?.start ?? null,
      lineEnd: draft.lines?.end ?? null,
      colIndex: draft.cell?.colIndex ?? null,
      colHeader: draft.cell?.colHeader ?? null,
      images: commentDraftImages.value.length ? commentDraftImages.value : undefined,
    })
    commentDraft.value = null
    draftAnchor.value = null
    commentDraftImages.value = []
    lastSelectionText = ''
    lastSelectionLines = null
    lastSelectionRange = null
    lastCsvCell = null
    reapplyHighlights()
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

  function saveEditComment(id: string | null): void {
    if (!id) return
    const note = editDraftText.value.trim()
    if (!note) return
    const p = path()
    projectsStore.updateFileComment(p, id, note)
    // Sync images: remove existing ones that are gone, add new ones
    const existing = projectsStore.fileCommentsFor(p).find(c => c.id === id)
    const existingImages = existing?.images || []
    const nextImages = editingCommentImages.value
    for (const img of existingImages) {
      if (!nextImages.includes(img)) projectsStore.removeFileCommentImage(p, id, img)
    }
    for (const img of nextImages) {
      if (!existingImages.includes(img)) projectsStore.addFileCommentImage(p, id, img)
    }
    cancelEditComment()
    reapplyHighlights()
  }

  async function handleEditImageUpload(e: Event, id: string | null): Promise<void> {
    if (!id) return
    const input = e.target as HTMLInputElement
    if (!input.files?.length) return
    const chatId = projectsStore.activeChatId
    if (!chatId) return
    try {
      const refs = await projectsStore.uploadImageRefs(chatId, Array.from(input.files))
      const p = path()
      for (const ref of refs) {
        projectsStore.addFileCommentImage(p, id, ref)
      }
      // Refresh local edit state from store
      const c = projectsStore.fileCommentsFor(p).find(x => x.id === id)
      if (c?.images) editingCommentImages.value = [...c.images]
    } catch (err) {
      console.error('Comment image upload failed:', err)
    }
    input.value = ''
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
    highlightInMarkdown: highlightCommentText,
    commentLineLabel,
    computeSelectionLines,
    updateSelectionAnchorFromRange,
    onSelectionChange,
    onCsvCellSelect,
    onCsvCellActivate,
    openCommentForSelection,
    openCommentForCsvCell,
    deleteFileComment,
    cancelComment,
    saveComment,
    handleDraftImageUpload,
    removeDraftImage,
    startEditComment,
    cancelEditComment,
    saveEditComment,
    handleEditImageUpload,
    removeEditImage,
  }
}
