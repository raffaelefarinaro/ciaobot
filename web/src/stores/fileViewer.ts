import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'
import { api } from '../lib/api'
import { askConfirm } from '../lib/confirm'
import { useProjectStore } from './projects'

// File viewer for workspace files. Opened by clicking a linkified file path
// in a chat or by tapping an inline file-card. Backed by /api/workspace-file
// (no workspace sandbox; relative paths anchor to config.workspace_root).
//
// The viewer has one reading surface: the current on-disk preview (which also
// becomes the editing surface when editing is available).

export type FileViewerKind = 'text' | 'image' | 'pdf' | 'html'
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
  const projectStore = useProjectStore()
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

  // `chatId` is kept for pin/open context (inline card).
  const chatId = ref('')

  // When non-empty, this open is the chat's *shared* (server-owned) pin surface
  // rather than an ordinary manual file open. A remote unpin/replacement
  // reconciles only this surface, and only for this chat; a user close of it
  // unpins through the server, whereas an ordinary dismiss stays local.
  const sharedPinChatId = ref('')

  // Edit state. When `editing` is true the modal swaps the read-only viewer
  // for a textarea pre-filled with `content`. `editBuffer` holds the in-flight
  // edits so cancel discards cleanly without clobbering on-disk content.
  const editing = ref(false)
  const editBuffer = ref('')
  const editSaving = ref(false)
  const editError = ref('')
  const isDirty = computed(() => editing.value && editBuffer.value !== content.value)

  // .pptx preview needs LibreOffice (soffice) server-side to convert to PDF.
  // Checked proactively so a missing install shows guidance instead of the
  // iframe silently failing to load with a browser-level error.
  const pptxNeedsLibreoffice = ref(false)
  const libreofficeInstallError = ref('')

  async function installLibreofficeInChat(): Promise<void> {
    try {
      await projectStore.fixError({
        errorText:
          'LibreOffice (soffice) is not installed, so PowerPoint files cannot be previewed in the Ciaobot file viewer.',
        context:
          'Previewing a .pptx from the Ciaobot file viewer. Install LibreOffice (e.g. `brew install --cask libreoffice` on macOS) so soffice can render slides; Ciaobot never runs package installs on its own.',
        title: 'Install LibreOffice',
      })
    } catch (e) {
      libreofficeInstallError.value = e instanceof Error ? e.message : String(e)
    }
  }

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
    editing.value = false
    editBuffer.value = ''
    editError.value = ''
    pptxNeedsLibreoffice.value = false
    libreofficeInstallError.value = ''
    sharedPinChatId.value = ''
  }

  async function checkLibreofficeStatus(): Promise<void> {
    try {
      const res = await api.get<{ available: boolean }>('/api/libreoffice-status')
      pptxNeedsLibreoffice.value = !res.available
    } catch {
      pptxNeedsLibreoffice.value = false
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
    try {
      let resolvedPath = filePath
      if (chat) {
        const resolved = await api.get<{ path: string }>(
          `/api/chats/${encodeURIComponent(chat)}/file-path?path=${encodeURIComponent(filePath)}`,
        )
        if (seq !== fetchSeq) return true
        resolvedPath = resolved.path
        path.value = resolvedPath
      }
      const isMarkdownFile = /\.(md|markdown)$/i.test(resolvedPath.replace(/:\d+$/, ''))
      const pathsPromise = isMarkdownFile ? loadMarkdownPaths() : Promise.resolve([])
      kind.value = fileViewerKindForPath(resolvedPath)
      if (kind.value === 'pdf') {
        content.value = ''
        if (/\.pptx$/i.test(resolvedPath.replace(/:\d+$/, ''))) void checkLibreofficeStatus()
        return true
      }
      if (kind.value === 'html') {
        // The frame loads /api/workspace-html on its own, keyed off loadToken.
        content.value = ''
        return true
      }
      const url = `/api/workspace-file?path=${encodeURIComponent(resolvedPath)}`
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

  /**
   * Open a file as the chat's shared-pin surface (the narrow opener, or the
   * split panel). Marks the open so a remote unpin/replacement can reconcile it,
   * and so a user close can unpin through the server instead of staying local.
   */
  async function openSharedPin(filePath: string, chat: string): Promise<boolean> {
    const ok = await open(filePath, null, chat)
    if (ok) sharedPinChatId.value = chat
    else sharedPinChatId.value = ''
    return ok
  }

  // Remote reconciliation: when the server pin for the chat this shared-pin
  // preview represents changes (unpin or replacement), close a clean preview
  // and never auto-open the replacement. A dirty editor is kept alive and the
  // open is demoted to an ordinary preview (it is no longer the pin); nothing
  // here issues a PATCH, so an event-driven close never writes back.
  watch(
    () => {
      const cid = sharedPinChatId.value
      return cid ? projectStore.pinnedFileFor(cid) : undefined
    },
    (serverPath) => {
      const cid = sharedPinChatId.value
      if (!cid) return
      if (serverPath === path.value) return
      if (isDirty.value) {
        sharedPinChatId.value = ''
        return
      }
      void close()
    },
  )

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
    const seq = ++fetchSeq
    loading.value = true
    try {
      if (chat) {
        const resolved = await api.get<{ path: string }>(
          `/api/chats/${encodeURIComponent(chat)}/file-path?path=${encodeURIComponent(filePath)}`,
        )
        if (seq !== fetchSeq) return true
        path.value = resolved.path
      }
    } catch (e) {
      if (seq === fetchSeq) error.value = e instanceof Error ? e.message : String(e)
    } finally {
      if (seq === fetchSeq) loading.value = false
    }
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
    fetchSeq++
    path.value = ''
    chatId.value = ''
    _reset()
    return true
  }

  /**
   * Close the viewer. When this open was the chat's shared-pin surface, closing
   * it is a request to unpin that file through the server (one PATCH); an
   * ordinary manual open just dismisses locally. Respects the dirty-edit guard
   * before touching server state, and never double-pins or repins.
   */
  async function dismissSharedPin(): Promise<void> {
    const cid = sharedPinChatId.value
    if (!cid) {
      await close()
      return
    }
    if (!(await close())) return
    await projectStore.unpinFile(cid)
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
    sharedPinChatId,
    editing,
    isDirty,
    editBuffer,
    editSaving,
    editError,
    pptxNeedsLibreoffice,
    libreofficeInstallError,
    markdownPaths,
    htmlView,
    sourceLoading,
    sourceError,
    sourceLoaded,
    // actions
    open,
    openImage,
    openSharedPin,
    loadSource,
    setHtmlView,
    close,
    dismissSharedPin,
    loadMarkdownPaths,
    startEditing,
    cancelEditing,
    saveEdits,
    installLibreofficeInChat,
  }
})
