import { computed, ref, type Ref } from 'vue'
import { getPendingBucket, normalizePendingBuckets, setPendingBucket } from '../lib/pendingBuckets'
import { formatChatComments, formatFileComments, type ChatCommentAnchor } from '../lib/commentContext'
import { setListIndex } from '../lib/safeList'

/**
 * Everything the user stages against the *next* message, plus the durable
 * notes and pins anchored to a file.
 *
 * This is a store module, not a Pinia store: `useProjectStore` calls the
 * factory once inside its own setup and spreads the result into what it
 * returns, so consumers keep reading `store.pendingComments`,
 * `store.addPendingComment(...)` and friends exactly as before. Keeping it a
 * plain factory (rather than a second `defineStore`) means the refs handed to
 * the projects store are the *same* refs this module mutates — no proxy layer,
 * no second `$state`, no change in reactivity or in the store's surface.
 *
 * Owned here: the per-chat pending-image, pending-file-comment and
 * pending-chat-comment buckets, the durable per-file comment store, pinned
 * file paths and auto-pin dismissals — and every `localStorage` key that backs
 * them (`ciao-pending-images`, `ciao-pending-comments`,
 * `ciao-pending-chat-comments`, `ciao-file-comments`, `ciao-pinned-files`,
 * `ciao-dismissed-auto-pins`).
 *
 * Not owned here: which chat is active (injected), message history, sending.
 * `prepareMessage` composes the outgoing text from the staged material and
 * `consumePreparedAttachments` clears it, but the send itself stays in the
 * projects store.
 */

// Pending in-file comments captured from the file viewer. Each entry is
// a (path, selected text, user note) triple plus an optional source line
// range (1-indexed, inclusive). Cleared on send (formatted into the
// outgoing message) or via removePendingComment / clear helpers.
export type PendingComment = {
  id: string
  path: string
  selection: string
  comment: string
  lineStart?: number | null
  lineEnd?: number | null
  colIndex?: number | null
  colHeader?: string | null
  // HTML artifact anchor (CSS selector + text offsets in the rendered page).
  // Null for markdown/CSV comments; checked before lineStart.
  artifactSelector?: string | null
  artifactStartOffset?: number | null
  artifactEndOffset?: number | null
  artifactElementTag?: string | null
  artifactWholeElement?: boolean
  images?: string[]
}

// Durable file comments: persisted per file so they remain visible in the
// document viewer after being sent. Keyed by workspace-relative path.
export type FileComment = PendingComment & { createdAt: string }

// Chat comments: ephemeral references to text selected inside a chat bubble.
// Formatted as XML-tagged reference blocks (see lib/commentContext.ts).
export type PendingChatComment = ChatCommentAnchor & {
  id: string
  selection: string
  comment: string
  images?: string[]
}

export type PreparedMessage = {
  composed: string
  imageRefs?: string[]
  fileComments: PendingComment[]
  chatComments: PendingChatComment[]
}

export function createChatAnnotations(deps: { activeChatId: Ref<string | null> }) {
  const { activeChatId } = deps

  const pendingImagesByChat = ref<Record<string, string[]>>({})
  const pendingImages = computed<string[]>({
    get: () => getPendingBucket(pendingImagesByChat.value, activeChatId.value),
    set: (entries) => {
      if (!activeChatId.value) return
      setPendingBucket(pendingImagesByChat.value, activeChatId.value, entries)
      persistPendingImages()
    },
  })
  const pendingCommentsByChat = ref<Record<string, PendingComment[]>>({})
  const pendingComments = computed<PendingComment[]>({
    get: () => getPendingBucket(pendingCommentsByChat.value, activeChatId.value),
    set: (entries) => {
      if (!activeChatId.value) return
      setPendingBucket(pendingCommentsByChat.value, activeChatId.value, entries)
      persistPendingComments()
    },
  })
  const fileComments = ref<Record<string, FileComment[]>>({})
  const pendingChatCommentsByChat = ref<Record<string, PendingChatComment[]>>({})
  const pendingChatComments = computed<PendingChatComment[]>({
    get: () => getPendingBucket(pendingChatCommentsByChat.value, activeChatId.value),
    set: (entries) => {
      if (!activeChatId.value) return
      setPendingBucket(pendingChatCommentsByChat.value, activeChatId.value, entries)
      persistPendingChatComments()
    },
  })
  // Pinned file paths per chat/project. Dismissals are remembered per *path*,
  // not per chat: a replayed `file_surface` event (WS reconnect replays the
  // in-flight stream's buffer) must not reopen a file the user closed, but a
  // later surface of a *different* file is a new deliverable and must still
  // open. A chat-wide flag conflated the two and silently swallowed every
  // subsequent surface request for the rest of the chat.
  const pinnedFilePaths = ref<Record<string, string>>({})
  const dismissedAutoPins = ref<Record<string, string[]>>({})

  // ── Persistence ────────────────────────────────────────────────────

  function persistFileComments() {
    try {
      localStorage.setItem('ciao-file-comments', JSON.stringify(fileComments.value))
    } catch { /* ignore */ }
  }

  function persistPinnedFiles() {
    try {
      localStorage.setItem('ciao-pinned-files', JSON.stringify(pinnedFilePaths.value))
    } catch { /* ignore */ }
  }

  // Older builds stored `{ [chatId]: true }`, a chat-wide block. Drop those
  // rather than translating them: the flag they encoded ("never surface here
  // again") is the bug this shape replaces, and the file it referred to is not
  // recoverable from it.
  function normalizeDismissedAutoPins(raw: unknown): Record<string, string[]> {
    if (!raw || typeof raw !== 'object') return {}
    const out: Record<string, string[]> = {}
    for (const [id, value] of Object.entries(raw as Record<string, unknown>)) {
      if (!Array.isArray(value)) continue
      const paths = value.filter((p): p is string => typeof p === 'string' && !!p)
      if (paths.length) out[id] = paths
    }
    return out
  }

  function persistDismissedAutoPins() {
    try {
      localStorage.setItem('ciao-dismissed-auto-pins', JSON.stringify(dismissedAutoPins.value))
    } catch { /* ignore */ }
  }

  function persistPendingImages() {
    try {
      localStorage.setItem('ciao-pending-images', JSON.stringify(pendingImagesByChat.value))
    } catch { /* ignore */ }
  }

  function persistPendingComments() {
    try {
      localStorage.setItem('ciao-pending-comments', JSON.stringify(pendingCommentsByChat.value))
    } catch { /* ignore */ }
  }

  function persistPendingChatComments() {
    try {
      localStorage.setItem('ciao-pending-chat-comments', JSON.stringify(pendingChatCommentsByChat.value))
    } catch { /* ignore */ }
  }

  /**
   * Read every annotation key back. Deliberately **not** wrapped in its own
   * try/catch: it runs inside the projects store's single `restoreState` try
   * block, between the active-chat read and the stream/unacked reads, and a
   * malformed value must keep aborting the rest of that restore exactly as it
   * did when these reads were inline. `activeChatId` must already be restored
   * (the bucket normaliser folds legacy flat arrays onto it).
   */
  function restoreFromStorage(): void {
    const fc = localStorage.getItem('ciao-file-comments')
    if (fc) fileComments.value = JSON.parse(fc)
    const pf = localStorage.getItem('ciao-pinned-files')
    if (pf) pinnedFilePaths.value = JSON.parse(pf)
    const pd = localStorage.getItem('ciao-dismissed-auto-pins')
    if (pd) dismissedAutoPins.value = normalizeDismissedAutoPins(JSON.parse(pd))
    const pi = localStorage.getItem('ciao-pending-images')
    if (pi) pendingImagesByChat.value = normalizePendingBuckets<string>(JSON.parse(pi), activeChatId.value)
    const pc = localStorage.getItem('ciao-pending-comments')
    if (pc) pendingCommentsByChat.value = normalizePendingBuckets<PendingComment>(JSON.parse(pc), activeChatId.value)
    const pcc = localStorage.getItem('ciao-pending-chat-comments')
    if (pcc) pendingChatCommentsByChat.value = normalizePendingBuckets<PendingChatComment>(JSON.parse(pcc), activeChatId.value)
  }

  // ── Pending images ─────────────────────────────────────────────────

  function addPendingImageRefs(chatId: string, refs: string[]): void {
    if (!refs.length) return
    const existing = getPendingBucket(pendingImagesByChat.value, chatId)
    setPendingBucket(pendingImagesByChat.value, chatId, [...existing, ...refs])
    persistPendingImages()
  }

  function removePendingImage(index: number) {
    if (!activeChatId.value) return
    const next = pendingImages.value.filter((_, i) => i !== index)
    setPendingBucket(pendingImagesByChat.value, activeChatId.value, next)
    persistPendingImages()
  }

  function clearPendingImages() {
    if (!activeChatId.value) return
    setPendingBucket<string>(pendingImagesByChat.value, activeChatId.value, [])
    persistPendingImages()
  }

  // ── Pending file comments ──────────────────────────────────────────
  // Captured by the markdown viewer when the user highlights text and adds a
  // note. Sent on the next message in the active chat. UUID generation falls
  // back to a Math.random id if crypto.randomUUID is unavailable (older WebView).
  function addPendingComment(c: {
    path: string
    selection: string
    comment: string
    lineStart?: number | null
    lineEnd?: number | null
    colIndex?: number | null
    colHeader?: string | null
    artifactSelector?: string | null
    artifactStartOffset?: number | null
    artifactEndOffset?: number | null
    artifactElementTag?: string | null
    artifactWholeElement?: boolean
    images?: string[]
  }): string {
    const id = (typeof crypto !== 'undefined' && 'randomUUID' in crypto)
      ? (crypto as { randomUUID: () => string }).randomUUID()
      : `c_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`
    const entry: PendingComment = {
      id,
      path: c.path,
      selection: c.selection,
      comment: c.comment,
      lineStart: c.lineStart ?? null,
      lineEnd: c.lineEnd ?? c.lineStart ?? null,
      colIndex: c.colIndex ?? null,
      colHeader: c.colHeader ?? null,
      artifactSelector: c.artifactSelector ?? null,
      artifactStartOffset: c.artifactStartOffset ?? null,
      artifactEndOffset: c.artifactEndOffset ?? null,
      artifactElementTag: c.artifactElementTag ?? null,
      artifactWholeElement: c.artifactWholeElement ?? false,
      images: c.images,
    }
    if (activeChatId.value) {
      const existing = getPendingBucket(pendingCommentsByChat.value, activeChatId.value)
      setPendingBucket(pendingCommentsByChat.value, activeChatId.value, [...existing, entry])
      persistPendingComments()
    }
    // Also persist into the durable per-file store so the comment stays visible
    // in the document viewer after it is sent.
    const list = fileComments.value[c.path] || []
    if (!list.some(x => x.id === id)) {
      fileComments.value[c.path] = [...list, { ...entry, createdAt: new Date().toISOString() }]
      persistFileComments()
    }
    return id
  }
  function removePendingComment(id: string): void {
    pendingComments.value = pendingComments.value.filter(c => c.id !== id)
    persistPendingComments()
  }
  function clearPendingComments(): void {
    pendingComments.value = []
    persistPendingComments()
  }

  // ── Durable file comments ──────────────────────────────────────────
  function fileCommentsFor(path: string): FileComment[] {
    return fileComments.value[path] || []
  }
  function removeFileComment(path: string, id: string): void {
    const list = fileComments.value[path]
    if (!list) return
    const next = list.filter(c => c.id !== id)
    if (next.length) fileComments.value[path] = next
    else delete fileComments.value[path]
    // Also drop from pending if it hasn't been sent yet.
    pendingComments.value = pendingComments.value.filter(c => c.id !== id)
    persistFileComments()
    persistPendingComments()
  }

  function updateFileComment(path: string, id: string, comment: string): void {
    const list = fileComments.value[path]
    if (!list) return
    const next = list.map(c => c.id === id ? { ...c, comment } : c)
    fileComments.value[path] = next
    // Also update pending if it hasn't been sent yet.
    pendingComments.value = pendingComments.value.map(c =>
      c.id === id ? { ...c, comment } : c
    )
    persistFileComments()
    persistPendingComments()
  }

  // ── Pinned file viewer (per chat/project) ──────────────────────────
  function pinFile(id: string, path: string): void {
    pinnedFilePaths.value = { ...pinnedFilePaths.value, [id]: path }
    // Pinning a file the user had closed clears that path's dismissal, so a
    // later surface of it is allowed to reopen it again.
    const dismissed = dismissedAutoPins.value[id]
    if (dismissed?.includes(path)) {
      const remaining = dismissed.filter(p => p !== path)
      const nextDismissed = { ...dismissedAutoPins.value }
      if (remaining.length) nextDismissed[id] = remaining
      else delete nextDismissed[id]
      dismissedAutoPins.value = nextDismissed
      persistDismissedAutoPins()
    }
    persistPinnedFiles()
  }
  function unpinFile(id: string): void {
    const next = { ...pinnedFilePaths.value }
    const closedPath = next[id]
    delete next[id]
    pinnedFilePaths.value = next
    if (closedPath) {
      const dismissed = dismissedAutoPins.value[id] || []
      if (!dismissed.includes(closedPath)) {
        dismissedAutoPins.value = {
          ...dismissedAutoPins.value,
          [id]: [...dismissed, closedPath],
        }
        persistDismissedAutoPins()
      }
    }
    persistPinnedFiles()
  }
  function pinnedFileFor(id: string): string | undefined {
    return pinnedFilePaths.value[id]
  }
  /** True when the user closed this exact path in this chat (see `unpinFile`). */
  function isAutoPinDismissed(id: string, path: string): boolean {
    return Boolean(dismissedAutoPins.value[id]?.includes(path))
  }

  // ── Pending chat comments ─────────────────────────────────────────
  function addPendingChatComment(c: Omit<PendingChatComment, 'id'>): string {
    const id = (typeof crypto !== 'undefined' && 'randomUUID' in crypto)
      ? (crypto as { randomUUID: () => string }).randomUUID()
      : `cc_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`
    if (activeChatId.value) {
      const existing = getPendingBucket(pendingChatCommentsByChat.value, activeChatId.value)
      setPendingBucket(pendingChatCommentsByChat.value, activeChatId.value, [
        ...existing,
        { id, ...c },
      ])
      persistPendingChatComments()
    }
    return id
  }
  function removePendingChatComment(id: string): void {
    pendingChatComments.value = pendingChatComments.value.filter(c => c.id !== id)
    persistPendingChatComments()
  }
  function clearPendingChatComments(): void {
    pendingChatComments.value = []
    persistPendingChatComments()
  }
  function updatePendingChatComment(id: string, comment: string): void {
    const idx = pendingChatComments.value.findIndex(c => c.id === id)
    if (idx === -1) return
    setListIndex(pendingChatComments.value, idx, { ...pendingChatComments.value[idx], comment })
    persistPendingChatComments()
  }
  function addPendingChatCommentImage(id: string, imageRef: string): void {
    const idx = pendingChatComments.value.findIndex(c => c.id === id)
    if (idx === -1) return
    const existing = pendingChatComments.value[idx].images || []
    if (!existing.includes(imageRef)) {
      setListIndex(pendingChatComments.value, idx, { ...pendingChatComments.value[idx], images: [...existing, imageRef] })
      persistPendingChatComments()
    }
  }
  function removePendingChatCommentImage(id: string, imageRef: string): void {
    const idx = pendingChatComments.value.findIndex(c => c.id === id)
    if (idx === -1) return
    const existing = pendingChatComments.value[idx].images || []
    const next = existing.filter(img => img !== imageRef)
    setListIndex(pendingChatComments.value, idx, { ...pendingChatComments.value[idx], images: next.length ? next : undefined })
    persistPendingChatComments()
  }
  function addFileCommentImage(path: string, id: string, imageRef: string): void {
    const list = fileComments.value[path]
    if (!list) return
    const idx = list.findIndex(c => c.id === id)
    if (idx === -1) return
    const existing = list[idx].images || []
    if (!existing.includes(imageRef)) {
      const next = [...list]
      next[idx] = { ...next[idx], images: [...existing, imageRef] }
      fileComments.value[path] = next
      // Sync to pending if it exists there
      const pIdx = pendingComments.value.findIndex(c => c.id === id)
      if (pIdx !== -1) {
        setListIndex(pendingComments.value, pIdx, { ...pendingComments.value[pIdx], images: [...existing, imageRef] })
        persistPendingComments()
      }
      persistFileComments()
    }
  }
  function removeFileCommentImage(path: string, id: string, imageRef: string): void {
    const list = fileComments.value[path]
    if (!list) return
    const idx = list.findIndex(c => c.id === id)
    if (idx === -1) return
    const existing = list[idx].images || []
    const nextImages = existing.filter(img => img !== imageRef)
    const next = [...list]
    next[idx] = { ...next[idx], images: nextImages.length ? nextImages : undefined }
    fileComments.value[path] = next
    const pIdx = pendingComments.value.findIndex(c => c.id === id)
    if (pIdx !== -1) {
      setListIndex(pendingComments.value, pIdx, { ...pendingComments.value[pIdx], images: nextImages.length ? nextImages : undefined })
      persistPendingComments()
    }
    persistFileComments()
  }

  // ── Composing and consuming ───────────────────────────────────────

  function formatPendingComments(comments = pendingComments.value): string {
    return formatFileComments(comments)
  }

  function formatPendingChatComments(comments = pendingChatComments.value): string {
    return formatChatComments(comments)
  }

  /**
   * Staged attachments are unsent content just as much as typed text, and the
   * server cannot see them: without this `isEmptyDraft` would agree the chat is
   * empty and delete the pasted screenshot with it.
   */
  function hasStagedAttachments(chatId: string): boolean {
    if (getPendingBucket(pendingImagesByChat.value, chatId).length) return true
    if (getPendingBucket(pendingCommentsByChat.value, chatId).length) return true
    if (getPendingBucket(pendingChatCommentsByChat.value, chatId).length) return true
    return false
  }

  function prepareMessage(chatId: string, text: string): PreparedMessage {
    const chatImages = getPendingBucket(pendingImagesByChat.value, chatId)
    const fileCommentList = getPendingBucket(pendingCommentsByChat.value, chatId)
    const chatComments = getPendingBucket(pendingChatCommentsByChat.value, chatId)
    // Collect images from pendingImages plus any images attached to comments.
    const allImages = new Set<string>(chatImages)
    for (const c of fileCommentList) {
      if (c.images) c.images.forEach(img => allImages.add(img))
    }
    for (const c of chatComments) {
      if (c.images) c.images.forEach(img => allImages.add(img))
    }
    const imageRefs = allImages.size > 0 ? Array.from(allImages) : undefined
    const fileBlock = formatPendingComments(fileCommentList)
    const chatBlock = formatPendingChatComments(chatComments)
    const hasTyped = text.trim().length > 0
    // Reference blocks (quoted text + note) go FIRST, then the typed prompt,
    // so the model reads the material being discussed before the instruction
    // (Anthropic: placing the query at the end of the input improves quality).
    let composed = ''
    if (fileBlock) composed += fileBlock
    if (chatBlock) composed += (composed ? '\n' : '') + chatBlock
    if (hasTyped) composed += (composed ? '\n\n' : '') + text.trim()
    return { composed, imageRefs, fileComments: fileCommentList, chatComments }
  }

  function consumePreparedAttachments(chatId: string, message: PreparedMessage) {
    setPendingBucket<string>(pendingImagesByChat.value, chatId, [])
    persistPendingImages()
    // Remove sent file comments from the durable store so they don't linger
    // in the viewer after the message has been dispatched.
    for (const c of message.fileComments) {
      const list = fileComments.value[c.path]
      if (list) {
        const next = list.filter(x => x.id !== c.id)
        if (next.length) fileComments.value[c.path] = next
        else delete fileComments.value[c.path]
      }
    }
    persistFileComments()
    setPendingBucket<PendingComment>(pendingCommentsByChat.value, chatId, [])
    setPendingBucket<PendingChatComment>(pendingChatCommentsByChat.value, chatId, [])
    persistPendingComments()
    persistPendingChatComments()
  }

  return {
    // State exposed on the projects store
    pendingImages,
    pendingComments,
    pendingChatComments,
    fileComments,
    // Actions exposed on the projects store
    addPendingImageRefs,
    removePendingImage,
    clearPendingImages,
    addPendingComment,
    removePendingComment,
    clearPendingComments,
    fileCommentsFor,
    removeFileComment,
    updateFileComment,
    pinFile,
    unpinFile,
    pinnedFileFor,
    addPendingChatComment,
    removePendingChatComment,
    clearPendingChatComments,
    updatePendingChatComment,
    addPendingChatCommentImage,
    removePendingChatCommentImage,
    addFileCommentImage,
    removeFileCommentImage,
    // Used by the projects store, not re-exposed
    restoreFromStorage,
    isAutoPinDismissed,
    hasStagedAttachments,
    prepareMessage,
    consumePreparedAttachments,
  }
}

export type ChatAnnotations = ReturnType<typeof createChatAnnotations>
