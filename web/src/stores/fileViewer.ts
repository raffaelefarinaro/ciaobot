import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api } from '../lib/api'
import { askConfirm } from '../lib/confirm'

// File viewer for workspace files. Opened by clicking a linkified file path
// in a chat or by tapping an inline file-card. Backed by /api/workspace-file
// (no workspace sandbox; relative paths anchor to config.workspace_root).
//
// Three "tabs" the modal exposes:
//   - preview: current on-disk content (current contract; default)
//   - history: snapshot list from /api/file-history, drives the diff selector
//   - diff:    side-by-side comparison of two snapshots (or current vs prior)
//
// Plus an editing mode that POSTs to /api/workspace-file to save user edits
// and snapshot them via the active chat's history.

export type FileViewerKind = 'text' | 'image' | 'pdf' | 'html'
export type FileViewerTab = 'preview' | 'history' | 'diff' | 'backlinks'
// Artifacts render by default and show their source on demand. Code view is
// also the only place they can be edited, since editing needs the source.
export type HtmlArtifactView = 'preview' | 'code'

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
  if (/\.(pdf|pptx)$/i.test(cleaned)) return 'pdf'
  if (/\.html?$/i.test(cleaned)) return 'html'
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
  // Generation counter for file/source FETCHES, deliberately separate from
  // `loadToken` (which reloads the html frame and is bumped for unrelated
  // reasons - discarding a response on one of those would drop a good result).
  // A path check alone is not enough: open A, open B, open A again, and A's
  // first response matches `path.value` and overwrites the newer one, which
  // can then be saved back over the current file.
  let fetchSeq = 0

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
  const isDirty = computed(() => editing.value && editBuffer.value !== content.value)

  // .pptx preview needs LibreOffice (soffice) server-side to convert to PDF.
  // Checked proactively so a missing install shows a real "Install" button
  // instead of the iframe silently failing to load with a browser-level error.
  const pptxNeedsLibreoffice = ref(false)
  const libreofficeInstalling = ref(false)
  const libreofficeInstallError = ref('')

  // Markdown vault-link resolution uses a vault-wide path index from the API.
  const markdownPaths = ref<string[]>([])

  // Artifact (.html) state. The source is deliberately NOT fetched on open:
  // `error` blanks the whole viewer body, so a failed text fetch would replace
  // a perfectly renderable page with an error string. Source loads only when
  // the user asks for Code view, and its failures stay in `sourceError`.
  const htmlView = ref<HtmlArtifactView>('preview')
  const sourceLoading = ref(false)
  const sourceError = ref('')
  const sourceLoaded = ref(false)

  function _reset(): void {
    kind.value = 'text'
    htmlView.value = 'preview'
    sourceLoading.value = false
    sourceError.value = ''
    sourceLoaded.value = false
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
    pptxNeedsLibreoffice.value = false
    libreofficeInstallError.value = ''
  }

  async function checkLibreofficeStatus(): Promise<void> {
    try {
      const res = await api.get<{ available: boolean }>('/api/libreoffice-status')
      pptxNeedsLibreoffice.value = !res.available
    } catch {
      pptxNeedsLibreoffice.value = false
    }
  }

  async function installLibreoffice(): Promise<void> {
    libreofficeInstalling.value = true
    libreofficeInstallError.value = ''
    try {
      const res = await api.post<{ ok: boolean; error?: string }>('/api/libreoffice-install', {})
      if (res.ok) {
        await checkLibreofficeStatus()
        if (!pptxNeedsLibreoffice.value) loadToken.value++
      } else {
        libreofficeInstallError.value = res.error || 'Installation failed.'
      }
    } catch (e) {
      libreofficeInstallError.value = e instanceof Error ? e.message : String(e)
    } finally {
      libreofficeInstalling.value = false
    }
  }

  async function loadMarkdownPaths(): Promise<string[]> {
    try {
      const res = await api.get<{ paths: string[] }>('/api/vault-markdown-paths')
      markdownPaths.value = res.paths ?? []
    } catch {
      markdownPaths.value = []
    }
    return markdownPaths.value
  }

  async function canReplaceOpenFile(nextPath: string): Promise<boolean> {
    if (!isDirty.value) return true
    // A background refresh of the currently-open file must never interrupt an
    // in-progress edit. Explicit navigation to a different file asks first.
    if (nextPath === path.value) return false
    return askConfirm('You have unsaved file changes. Discard them and open another file?', {
      title: 'Discard unsaved changes?',
      confirmLabel: 'Discard and open',
      destructive: true,
    })
  }

  async function open(
    filePath: string,
    lineNumber: number | null = null,
    chat: string = '',
  ): Promise<boolean> {
    if (!filePath || !await canReplaceOpenFile(filePath)) return false
    _reset()
    isOpen.value = true
    path.value = filePath
    line.value = lineNumber
    chatId.value = chat
    loading.value = true
    loadToken.value++
    const seq = ++fetchSeq
    const isMarkdownFile = /\.(md|markdown)$/i.test(filePath.replace(/:\d+$/, ''))
    const pathsPromise = isMarkdownFile ? loadMarkdownPaths() : Promise.resolve([])
    try {
      kind.value = fileViewerKindForPath(filePath)
      if (kind.value === 'pdf') {
        content.value = ''
        if (/\.pptx$/i.test(filePath.replace(/:\d+$/, ''))) void checkLibreofficeStatus()
        return true
      }
      if (kind.value === 'html') {
        // The frame loads /api/workspace-html on its own, keyed off loadToken.
        content.value = ''
        return true
      }
      const url = `/api/workspace-file?path=${encodeURIComponent(filePath)}`
      const [resp] = await Promise.all([
        fetch(url, { credentials: 'same-origin' }),
        pathsPromise,
      ])
      // Every write below belongs to `filePath`, not to whatever is open when
      // the response lands. A slow fetch used to repaint the viewer with the
      // file the user had already navigated away from — and since saveEdits
      // posts `content` to `path`, the next save then wrote one file's bytes
      // to another file's path. Anything that no longer matches is discarded.
      if (seq !== fetchSeq) return true
      if (!resp.ok) {
        if (resp.status === 404) error.value = 'File not found.'
        else if (resp.status === 403) error.value = 'Forbidden — path is outside the workspace.'
        else if (resp.status === 413) error.value = 'File is too large to preview (>2 MB).'
        else if (resp.status === 415) error.value = 'Unsupported file type.'
        else error.value = `Failed to load file (HTTP ${resp.status}).`
        return true
      }
      const text = await resp.text()
      if (seq !== fetchSeq) return true
      content.value = text
    } catch (e) {
      if (seq === fetchSeq) error.value = e instanceof Error ? e.message : String(e)
    } finally {
      // A superseding open() owns `loading` now; clearing it here would hide
      // its spinner while its own request is still in flight.
      if (seq === fetchSeq) loading.value = false
    }
    return true
  }

  // ── Artifact source (Code view) ────────────────────────────────────────

  async function loadSource(force = false): Promise<void> {
    if (!path.value || (sourceLoaded.value && !force)) return
    // The file this request is for. Same race as open(), and the damaging one:
    // the source fetch of an artifact the user has left used to land in
    // `content` under the newly-opened file's `path`, so startEditing seeded
    // the textarea from the wrong file and saveEdits POSTed those bytes to the
    // open file's path — one file overwritten with another's content.
    const requestedPath = path.value
    // Generation, not just path: reopening the SAME artifact while an earlier
    // request for it is still in flight leaves the path matching, so the older
    // response passed the check and overwrote the newer one.
    const seq = ++fetchSeq
    sourceLoading.value = true
    sourceError.value = ''
    try {
      const resp = await fetch(
        `/api/workspace-file?path=${encodeURIComponent(requestedPath)}`,
        { credentials: 'same-origin' },
      )
      if (seq !== fetchSeq) return
      if (!resp.ok) {
        sourceError.value = resp.status === 413
          ? 'Source is too large to show (>2 MB).'
          : `Failed to load source (HTTP ${resp.status}).`
        return
      }
      const text = await resp.text()
      if (seq !== fetchSeq) return
      content.value = text
      sourceLoaded.value = true
    } catch (e) {
      if (seq === fetchSeq) sourceError.value = e instanceof Error ? e.message : String(e)
    } finally {
      if (seq === fetchSeq) sourceLoading.value = false
    }
  }

  async function setHtmlView(view: HtmlArtifactView): Promise<void> {
    htmlView.value = view
    if (view === 'code') await loadSource()
  }

  async function openImage(filePath: string, chat: string = ''): Promise<boolean> {
    if (!filePath || !await canReplaceOpenFile(filePath)) return false
    _reset()
    isOpen.value = true
    kind.value = 'image'
    path.value = filePath
    chatId.value = chat
    loadToken.value++
    return true
  }

  async function close(force = false): Promise<boolean> {
    if (!force && isDirty.value) {
      if (!await askConfirm('You have unsaved file changes. Are you sure you want to close?', {
        title: 'Discard unsaved changes?',
        confirmLabel: 'Discard and close',
        destructive: true,
      })) {
        return false
      }
    }
    isOpen.value = false
    path.value = ''
    chatId.value = ''
    _reset()
    return true
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
    if (kind.value === 'html') {
      // Editing an artifact edits its source, so only from Code view and only
      // once the source is actually in hand.
      if (htmlView.value !== 'code' || !sourceLoaded.value) return
    } else if (kind.value !== 'text') return
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
      // Artifacts render from a URL, so adopting the buffer is not enough:
      // bump the token or Preview keeps showing the pre-save page.
      if (kind.value === 'html') loadToken.value++
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
    // Same reason as saveEdits: the artifact frame reloads from its URL.
    if (kind.value === 'html') loadToken.value++
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
    isDirty,
    editBuffer,
    editSaving,
    editError,
    pptxNeedsLibreoffice,
    libreofficeInstalling,
    libreofficeInstallError,
    markdownPaths,
    htmlView,
    sourceLoading,
    sourceError,
    sourceLoaded,
    // actions
    open,
    openImage,
    loadSource,
    setHtmlView,
    close,
    loadMarkdownPaths,
    setTab,
    loadHistory,
    setDiffSeqs,
    startEditing,
    cancelEditing,
    saveEdits,
    restoreSnapshot,
    installLibreoffice,
  }
})
