import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api } from '../lib/api'
import { askConfirm } from '../lib/confirm'

// File viewer for workspace files. Opened by clicking a linkified file path
// in a chat or by tapping an inline file-card. Backed by /api/workspace-file
// (no workspace sandbox; relative paths anchor to config.workspace_root).
//
// The only reading tabs are:
//   - preview: current on-disk content (default, also the editing surface)
//   - backlinks: markdown incoming links, when the file is markdown
// History/Diff were removed as overkill for the viewer.

export type FileViewerKind = 'text' | 'image' | 'pdf' | 'html'
export type FileViewerTab = 'preview' | 'backlinks'
// Artifacts render by default and show their source on demand. Code view is
// also the only place they can be edited, since editing needs the source.
export type HtmlArtifactView = 'preview' | 'code'

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

  // `chatId` is kept for pin/open context (inline card). History/Diff were
  // removed from the UI so no snapshot state is needed.
  const chatId = ref('')
  const tab = ref<FileViewerTab>('preview')

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

  // ── Tabs ─────────────────────────────────────────────────────────────────

  async function setTab(t: FileViewerTab): Promise<void> {
    tab.value = t
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
      content.value = editBuffer.value
      editing.value = false
      editBuffer.value = ''
      // Artifacts render from a URL, so adopting the buffer is not enough:
      // bump the token or Preview keeps showing the pre-save page.
      if (kind.value === 'html') loadToken.value++
      return true
    } catch (e) {
      editError.value = e instanceof Error ? e.message : String(e)
      return false
    } finally {
      editSaving.value = false
    }
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
    startEditing,
    cancelEditing,
    saveEdits,
    installLibreoffice,
  }
})
