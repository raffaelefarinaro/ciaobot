import { defineStore } from 'pinia'
import { ref } from 'vue'

// File viewer for workspace files. Opened by clicking a linkified file path
// in a chat or by tapping an inline file-card. Backed by /api/workspace-file
// (sandboxed to config.workspace_root + extra_workspace_roots).
//
// Three "tabs" the modal exposes:
//   - preview: current on-disk content (current contract; default)
//   - history: snapshot list from /api/file-history, drives the diff selector
//   - diff:    side-by-side comparison of two snapshots (or current vs prior)
//
// Plus an editing mode that POSTs to /api/workspace-file to save user edits
// and snapshot them via the active chat's history.

export type FileViewerKind = 'text' | 'image' | 'excalidraw' | 'pdf'
export type FileViewerTab = 'preview' | 'history' | 'diff'

export interface SnapshotMeta {
  seq: number
  ts: string
  action: string
  tool: string
  size: number
  truncated?: boolean
}

export function fileViewerKindForPath(filePath: string): FileViewerKind {
  const cleaned = filePath.replace(/:\d+$/, '').toLowerCase()
  if (/\.excalidraw$/i.test(cleaned)) return 'excalidraw'
  if (/\.(pdf|pptx)$/i.test(cleaned)) return 'pdf'
  return 'text'
}

export const useFileViewerStore = defineStore('fileViewer', () => {
  const isOpen = ref(false)
  const kind = ref<FileViewerKind>('text')
  const path = ref('')
  const line = ref<number | null>(null)
  const content = ref('')
  const loading = ref(false)
  const error = ref('')
  const loadToken = ref(0)

  // Snapshot-related state. `chatId` is set by callers that have a chat
  // context (the inline file card) so we can fetch history. When omitted
  // we still render the Preview tab but History/Diff are unavailable.
  const chatId = ref('')
  const tab = ref<FileViewerTab>('preview')
  const snapshots = ref<SnapshotMeta[]>([])
  const snapshotsLoading = ref(false)
  const snapshotsError = ref('')

  // Diff tab state: `diffSeqA` is the "before", `diffSeqB` is the "after".
  // 0 means "current on-disk content" — useful when you want to diff a
  // snapshot against where the file is right now (e.g. after an external
  // edit). Defaults are wired in `setTab('diff')`.
  const diffSeqA = ref(0)
  const diffSeqB = ref(0)
  const diffContentA = ref('')
  const diffContentB = ref('')
  const diffLoading = ref(false)
  const diffError = ref('')

  // Edit state. When `editing` is true the modal swaps the read-only viewer
  // for a textarea pre-filled with `content`. `editBuffer` holds the in-flight
  // edits so cancel discards cleanly without clobbering on-disk content.
  const editing = ref(false)
  const editBuffer = ref('')
  const editSaving = ref(false)
  const editError = ref('')

  function _reset(): void {
    kind.value = 'text'
    line.value = null
    content.value = ''
    error.value = ''
    loading.value = false
    tab.value = 'preview'
    snapshots.value = []
    snapshotsError.value = ''
    diffSeqA.value = 0
    diffSeqB.value = 0
    diffContentA.value = ''
    diffContentB.value = ''
    diffError.value = ''
    editing.value = false
    editBuffer.value = ''
    editError.value = ''
  }

  async function open(filePath: string, lineNumber: number | null = null, chat: string = ''): Promise<void> {
    if (!filePath) return
    _reset()
    isOpen.value = true
    path.value = filePath
    line.value = lineNumber
    chatId.value = chat
    loading.value = true
    loadToken.value++
    try {
      kind.value = fileViewerKindForPath(filePath)
      if (kind.value === 'pdf') {
        content.value = ''
        return
      }
      const url = `/api/workspace-file?path=${encodeURIComponent(filePath)}`
      const resp = await fetch(url, { credentials: 'same-origin' })
      if (!resp.ok) {
        if (resp.status === 404) error.value = 'File not found.'
        else if (resp.status === 403) error.value = 'Forbidden — path is outside the workspace.'
        else if (resp.status === 413) error.value = 'File is too large to preview (>2 MB).'
        else if (resp.status === 415) error.value = 'Unsupported file type.'
        else error.value = `Failed to load file (HTTP ${resp.status}).`
        return
      }
      content.value = await resp.text()
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
    }
  }

  function openImage(filePath: string, chat: string = ''): void {
    if (!filePath) return
    _reset()
    isOpen.value = true
    kind.value = 'image'
    path.value = filePath
    chatId.value = chat
    loadToken.value++
  }

  function close(): void {
    isOpen.value = false
    path.value = ''
    chatId.value = ''
    _reset()
  }

  // ── Tabs / history / diff ──────────────────────────────────────────────

  async function setTab(t: FileViewerTab): Promise<void> {
    tab.value = t
    if (t === 'history' || t === 'diff') {
      await loadHistory()
    }
    if (t === 'diff' && snapshots.value.length >= 2) {
      // Default to comparing the last two snapshots.
      diffSeqA.value = snapshots.value[snapshots.value.length - 2].seq
      diffSeqB.value = snapshots.value[snapshots.value.length - 1].seq
      await loadDiff()
    } else if (t === 'diff' && snapshots.value.length === 1) {
      // Only one snapshot: compare against current on-disk content.
      diffSeqA.value = snapshots.value[0].seq
      diffSeqB.value = 0  // 0 → "current"
      await loadDiff()
    }
  }

  async function loadHistory(): Promise<void> {
    if (!chatId.value || !path.value) {
      snapshots.value = []
      snapshotsError.value = chatId.value ? 'No file selected.' : 'No chat context — open the file from an inline card.'
      return
    }
    snapshotsLoading.value = true
    snapshotsError.value = ''
    try {
      const url = `/api/file-history?chat_id=${encodeURIComponent(chatId.value)}&file_path=${encodeURIComponent(path.value)}`
      const resp = await fetch(url, { credentials: 'same-origin' })
      if (!resp.ok) {
        snapshotsError.value = `Failed to load history (HTTP ${resp.status}).`
        snapshots.value = []
        return
      }
      const body = await resp.json()
      snapshots.value = Array.isArray(body.snapshots) ? body.snapshots : []
    } catch (e) {
      snapshotsError.value = e instanceof Error ? e.message : String(e)
      snapshots.value = []
    } finally {
      snapshotsLoading.value = false
    }
  }

  async function loadDiff(): Promise<void> {
    diffLoading.value = true
    diffError.value = ''
    try {
      const [a, b] = await Promise.all([
        _fetchSeq(diffSeqA.value),
        _fetchSeq(diffSeqB.value),
      ])
      diffContentA.value = a
      diffContentB.value = b
    } catch (e) {
      diffError.value = e instanceof Error ? e.message : String(e)
    } finally {
      diffLoading.value = false
    }
  }

  async function _fetchSeq(seq: number): Promise<string> {
    if (seq === 0) {
      // Current on-disk content. Reuse the open() text path: we already have
      // it in `content` for the active preview, but it might be stale by the
      // time the user opens Diff, so refetch.
      const resp = await fetch(
        `/api/workspace-file?path=${encodeURIComponent(path.value)}`,
        { credentials: 'same-origin' },
      )
      if (!resp.ok) throw new Error(`current content HTTP ${resp.status}`)
      return resp.text()
    }
    const url = `/api/file-content?chat_id=${encodeURIComponent(chatId.value)}&file_path=${encodeURIComponent(path.value)}&seq=${seq}`
    const resp = await fetch(url, { credentials: 'same-origin' })
    if (!resp.ok) throw new Error(`snapshot ${seq} HTTP ${resp.status}`)
    const body = await resp.json()
    return typeof body.content === 'string' ? body.content : ''
  }

  async function setDiffSeqs(a: number, b: number): Promise<void> {
    diffSeqA.value = a
    diffSeqB.value = b
    await loadDiff()
  }

  // ── Edit mode ──────────────────────────────────────────────────────────

  function startEditing(): void {
    if (kind.value !== 'text' && kind.value !== 'excalidraw') return
    editing.value = true
    editBuffer.value = content.value
    editError.value = ''
  }

  function cancelEditing(): void {
    editing.value = false
    editBuffer.value = ''
    editError.value = ''
  }

  async function saveEdits(): Promise<boolean> {
    if (!editing.value) return false
    editSaving.value = true
    editError.value = ''
    try {
      const body = {
        chat_id: chatId.value,
        path: path.value,
        content: editBuffer.value,
      }
      const resp = await fetch('/api/workspace-file', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!resp.ok) {
        editError.value = `Save failed (HTTP ${resp.status}).`
        return false
      }
      // Adopt the saved buffer as the new preview content. Refresh history
      // so the new snapshot (if any) shows up immediately in the History tab.
      content.value = editBuffer.value
      editing.value = false
      editBuffer.value = ''
      if (chatId.value) await loadHistory()
      return true
    } catch (e) {
      editError.value = e instanceof Error ? e.message : String(e)
      return false
    } finally {
      editSaving.value = false
    }
  }

  async function restoreSnapshot(seq: number): Promise<boolean> {
    if (!chatId.value || !path.value || seq <= 0) return false
    const resp = await fetch('/api/file-restore', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        chat_id: chatId.value,
        file_path: path.value,
        seq,
      }),
    })
    if (!resp.ok) return false
    // Reload content + history so the modal reflects the new state.
    await Promise.all([
      (async () => {
        const r = await fetch(`/api/workspace-file?path=${encodeURIComponent(path.value)}`, { credentials: 'same-origin' })
        if (r.ok) content.value = await r.text()
      })(),
      loadHistory(),
    ])
    return true
  }

  return {
    // state
    isOpen,
    kind,
    path,
    line,
    content,
    loading,
    error,
    loadToken,
    chatId,
    tab,
    snapshots,
    snapshotsLoading,
    snapshotsError,
    diffSeqA,
    diffSeqB,
    diffContentA,
    diffContentB,
    diffLoading,
    diffError,
    editing,
    editBuffer,
    editSaving,
    editError,
    // actions
    open,
    openImage,
    close,
    setTab,
    loadHistory,
    setDiffSeqs,
    startEditing,
    cancelEditing,
    saveEdits,
    restoreSnapshot,
  }
})
