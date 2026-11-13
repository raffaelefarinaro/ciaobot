import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'
import { api } from '../lib/api'
import { askConfirm } from '../lib/confirm'
import { fileLoadErrorMessage } from '../lib/fileLoadError'
import { isImageFilePath } from '../lib/chatActivity'
import { looksLikeFolderPath } from '../lib/filePaths'
import { fetchWorkspaceDir, folderErrorMessage } from '../lib/folderBrowserDriver'
import { useProjectStore } from './projects'

// File viewer for workspace files. Opened by clicking a linkified file path
// in a chat or by tapping an inline file-card. Backed by /api/workspace-file
// (no workspace sandbox; relative paths anchor to config.workspace_root).
//
// The viewer has one reading surface: the current on-disk preview (which also
// becomes the editing surface when editing is available).

export type FileViewerKind = 'text' | 'image' | 'pdf' | 'html' | 'folder'
// Artifacts render by default and show their source on demand. Code view is
// also the only place they can be edited, since editing needs the source.
export type HtmlArtifactView = 'preview' | 'code'

export function fileViewerKindForPath(filePath: string): FileViewerKind {
  const cleaned = filePath.replace(/:\d+$/, '').toLowerCase()
  if (/\.(pdf|pptx)$/i.test(cleaned)) return 'pdf'
  if (/\.html?$/i.test(cleaned)) return 'html'
  return 'text'
}

/** A folder pin is stored with one trailing slash (PATCH /api/chats/{id} pin contract, D1). */
export function isFolderPin(path: string): boolean {
  return /[/\\]$/.test(path)
}

// Mirrors `_WORKSPACE_FILE_EXTS` in ciao/web/routes_api.py: the only files the
// server accepts as a pin. Anything else would 415 and toast, so the tile
// opens it in the viewer instead of trying to dock it.
const PINNABLE_FILE_EXTS = new Set([
  'md', 'markdown', 'txt', 'py', 'ts', 'tsx', 'js', 'jsx', 'vue', 'css', 'html', 'htm', 'json',
  'yaml', 'yml', 'toml', 'sh', 'rs', 'go', 'java', 'xml', 'sql', 'cfg', 'ini', 'log', 'csv',
])
export function isPinnableFilePath(path: string): boolean {
  const m = /\.([^./\\]+)$/.exec(path.replace(/:\d+$/, ''))
  return !!m && PINNABLE_FILE_EXTS.has(m[1].toLowerCase())
}

export const useFileViewerStore = defineStore('fileViewer', () => {
  const projectStore = useProjectStore()
  const isOpen = ref(false)
  const kind = ref<FileViewerKind>('text')
  const path = ref('')
  // '' = a workspace-path image (the existing `<path>` mode); a served chat
  // attachment opens by URL, which `/api/workspace-image` cannot resolve.
  const imageUrl = ref('')
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

  // The folder a file was opened from in the folder browser; Back returns
  // there. Deliberately NOT cleared by _reset(): a link followed inside that
  // file keeps it. close() and openFolder() clear it.
  const returnFolder = ref('')

  // Where a file opens docked instead of in this viewer: the pin key of the
  // chat on screen, set by ChatLayout while its split view is available (a
  // wide pane on a chat route), '' everywhere else. Owned by the layout so the
  // viewer never pins to a chat that is not shown.
  const dockTarget = ref('')
  // Bumped when a dock targets the folder already pinned: the tile resets its browser to it.
  const folderRedock = ref({ path: '', seq: 0 })
  // Generation of the latest open: a dock whose path resolution lands after a
  // newer open must not pin over the file the user opened since.
  let dockSeq = 0
  function setDockTarget(key: string): void {
    dockTarget.value = key
  }

  /**
   * Pin the file beside the chat instead of opening the viewer, so it lands in
   * the tile without the modal flashing up first. Only an open that carries the
   * chat id of the dock target (a link or file card inside the chat that owns
   * the tile on screen) docks, replacing that chat's pin. A file opened from the
   * chat beside it opens the viewer: docking would pin it onto the other chat.
   * Every other open (a project page, the sidebar, an
   * archived transcript) uses the viewer. Returns false (and the caller opens
   * the viewer as usual) when there is no dock, no chat, or the pin write
   * fails. An open that targets a line (a `path:N` link, a comment chip) keeps
   * the viewer, which scrolls to it; the tile has no line to land on.
   */
  async function dockInstead(filePath: string, chat: string): Promise<boolean> {
    const seq = ++dockSeq
    const key = dockTarget.value
    // Only the chat the tile belongs to docks: a file opened from the chat beside it opens the
    // viewer, or it would be pinned onto the other chat.
    if (!key || !chat || chat !== key || isOpen.value) return false
    let target = filePath
    try {
      const resolved = await api.get<{ path: string }>(
        `/api/chats/${encodeURIComponent(chat)}/file-path?path=${encodeURIComponent(filePath)}`,
      )
      target = resolved.path
    } catch {
      return false
    }
    target = target.replace(/:\d+$/, '')
    // Superseded by a newer open: that open owns the outcome, so this one
    // neither pins nor falls back to the viewer.
    if (seq !== dockSeq) return true
    if (dockTarget.value !== key) return false
    if (projectStore.pinnedFileFor(key) === target) return true
    return projectStore.pinFile(key, target)
  }

  /**
   * Dock a folder beside the chat that owns the tile, like dockInstead does
   * for files: same gate, same stale-open guard. '' (Browse files) is the
   * chat's first root, resolved to its absolute path before pinning. A folder
   * that is already pinned may be showing a subfolder, so it is reset, not
   * skipped.
   */
  async function dockFolderInstead(dirPath: string, chat: string): Promise<boolean> {
    const seq = ++dockSeq
    const key = dockTarget.value
    if (!key || !chat || chat !== key || isOpen.value) return false
    let target = dirPath
    if (!target) {
      const probe = await fetchWorkspaceDir('', chat)
      if (!probe.ok) return false
      target = probe.listing.path
    }
    target = `${target.replace(/[/\\]+$/, '')}/`
    if (seq !== dockSeq) return true
    if (dockTarget.value !== key) return false
    if (projectStore.pinnedFileFor(key) === target) {
      folderRedock.value = { path: target, seq: folderRedock.value.seq + 1 }
      return true
    }
    return projectStore.pinFile(key, target)
  }

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
    imageUrl.value = ''
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
    if (lineNumber === null && await dockInstead(filePath, chat)) return true
    _reset()
    isOpen.value = true
    path.value = filePath
    line.value = lineNumber
    chatId.value = chat
    // Provisional until the chat path resolves, so the window opens at its
    // final size (the prose width or the wide one) instead of resizing.
    kind.value = fileViewerKindForPath(filePath)
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
        error.value = fileLoadErrorMessage(resp.status)
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
   * An image pin takes the image path: `open()` reads through the text-only
   * `/api/workspace-file`, which refuses an image with 415.
   */
  async function openSharedPin(filePath: string, chat: string): Promise<boolean> {
    const ok = isImageFilePath(filePath.replace(/:\d+$/, ''))
      ? await openImage(filePath, chat)
      : await open(filePath, null, chat)
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

  // A served image (a chat message attachment) has no workspace path: render
  // its URL directly and make no request until the `<img>` loads it.
  function openImageUrl(url: string, name: string, chat = ''): void {
    if (!url) return
    _reset()
    isOpen.value = true
    kind.value = 'image'
    path.value = name || 'image'
    imageUrl.value = url
    chatId.value = chat
    loadToken.value++
  }

  async function openImage(filePath: string, chat: string = ''): Promise<boolean> {
    if (!filePath || !await canReplaceOpenFile(filePath)) return false
    if (await dockInstead(filePath, chat)) return true
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

  /** Show a folder in the viewer. '' lists the first browsable root. */
  async function openFolder(dirPath: string, chat: string = ''): Promise<boolean> {
    if (!await canReplaceOpenFile(dirPath)) return false
    if (await dockFolderInstead(dirPath, chat)) return true
    _reset()
    returnFolder.value = ''
    fetchSeq++ // a file fetch still in flight must not repaint the folder
    isOpen.value = true
    kind.value = 'folder'
    path.value = dirPath
    chatId.value = chat
    loadToken.value++
    return true
  }

  /**
   * Project page › Browse files: the folder browser at the project's own
   * folder. Resolved first, because the viewer itself browses without a chat.
   * Returns the engine's error message when the folder cannot be listed.
   */
  async function openProjectFolder(projectId: string): Promise<string> {
    const probe = await fetchWorkspaceDir('', '', undefined, projectId)
    if (!probe.ok) return folderErrorMessage(probe.status, probe.error)
    await openFolder(probe.listing.path)
    return ''
  }

  /** The browser moved to another folder: keep the header and Copy path on it. */
  function setFolderPath(dirPath: string): void {
    if (kind.value === 'folder') path.value = dirPath
  }

  /** Open a file chosen in the folder browser, remembering the folder for Back. */
  async function openFromFolder(filePath: string, chat: string = '', fromFolder: string = ''): Promise<boolean> {
    const folder = fromFolder || (kind.value === 'folder' ? path.value : '')
    // Recorded before the load so Back pressed while the file loads still
    // returns to the folder (backOrClose ignores it while a folder is shown).
    if (folder && isOpen.value) returnFolder.value = folder
    const ok = isImageFilePath(filePath) ? await openImage(filePath, chat) : await open(filePath, null, chat)
    if (ok && folder && isOpen.value && kind.value !== 'folder') returnFolder.value = folder
    return ok
  }

  /** Back: return to the folder a file was opened from (and stay open), else close. */
  async function backOrClose(): Promise<boolean> {
    const folder = returnFolder.value
    if (folder && kind.value !== 'folder') {
      await openFolder(folder, chatId.value)
      return false
    }
    return close()
  }

  /**
   * Open what a link names. Anything that is not folder-shaped (a dotted
   * extension, a known extensionless name like README) opens exactly as it
   * always has, with no extra request. A folder-shaped one asks
   * /api/workspace-dir once: a folder opens in the browser; anything else,
   * a file included, takes the same open() it always took.
   */
  // Generation of the latest link click: a folder probe that answers after a
  // newer click must not repaint the viewer with the folder it named.
  let linkSeq = 0
  async function openLink(linkPath: string, lineNumber: number | null = null, chat: string = ''): Promise<boolean> {
    const mySeq = ++linkSeq
    const clean = linkPath.replace(/:\d+$/, '')
    if (isImageFilePath(clean)) return openImage(linkPath, chat)
    if (!looksLikeFolderPath(clean)) return open(linkPath, lineNumber, chat)
    const probe = await fetchWorkspaceDir(clean, chat)
    if (mySeq !== linkSeq) return false
    if (probe.ok) return openFolder(probe.listing.path, chat)
    // A folder the browser may not show is named as such, not as an unsupported file.
    if (probe.status === 403) {
      if (!await canReplaceOpenFile(linkPath)) return false
      _reset()
      fetchSeq++ // a file fetch still in flight must not repaint the message
      isOpen.value = true
      path.value = linkPath
      chatId.value = chat
      error.value = folderErrorMessage(probe.status, probe.error)
      return true
    }
    return open(linkPath, lineNumber, chat)
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
    returnFolder.value = ''
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
    imageUrl,
    line,
    content,
    loading,
    error,
    loadToken,
    chatId,
    sharedPinChatId,
    returnFolder,
    dockTarget,
    folderRedock,
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
    openImageUrl,
    openLink,
    openFolder,
    openProjectFolder,
    setFolderPath,
    openFromFolder,
    backOrClose,
    openSharedPin,
    setDockTarget,
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
