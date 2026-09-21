import { ref, toValue, watch, nextTick, type MaybeRefOrGetter, type Ref } from 'vue'
import { formatAttachedFilePath, nativeAbsoluteFilePath } from '../lib/chatAttachments'
import { readChatDraft, readSentPromptHistory, writeChatDraft } from '../lib/chatDrafts'

/**
 * Composer, draft and attachment behaviour for one chat.
 *
 * Extracted from `ChatPanel.vue` so it can be exercised without mounting the
 * panel. Everything it touches outside itself arrives through `options`:
 * no Pinia store, no router, no lifecycle hooks. `ChatPanel` still owns the
 * pickers (slash commands, @-mentions), the send path and the scroll
 * behaviour, and calls into this for the textarea, the draft and attachments.
 */

/** The slice of the project store the composer needs. The Pinia store
 *  satisfies this structurally; a test can pass a plain object of spies. */
export interface ComposerAttachmentStore {
  uploadImages(chatId: string, files: File[]): Promise<string[]>
  addPendingImageRefs(chatId: string, refs: string[]): void
  removePendingImage(index: number): void
  pushErrorToast(title: string, errorText: string): unknown
}

export interface ChatComposerOptions {
  /** Draft key. `ChatLayout` keys the panel by chat id, so one panel
   *  instance owns exactly one draft and this never changes for its life. */
  draftChatId: string | null | undefined
  /** The live chat id, used for upload and attachment endpoints. */
  chatId: MaybeRefOrGetter<string>
  /** Project the chat belongs to; tags the persisted draft and scopes the
   *  desktop file-drop grant. */
  projectId?: MaybeRefOrGetter<string | undefined>
  /** Workspace the draft belongs to; tags the persisted draft. */
  workspace?: MaybeRefOrGetter<string | undefined>
  /** The project's vault folder. Dropped non-image files can only be
   *  uploaded when the project has one. */
  vaultFolder?: MaybeRefOrGetter<string | undefined>
  store: ComposerAttachmentStore
  /** Injected for tests; defaults to the global `fetch`. */
  fetchImpl?: typeof fetch
}

type DroppedProjectFile = {
  path: string
  vault_path: string
  absolute_path?: string
  original_path?: string | null
  markdown_path?: string | null
}

type ProjectUploadResult = {
  saved?: DroppedProjectFile[]
  errors?: { filename: string; error: string }[]
  error?: string
}

export type NativeFileDropDetail = {
  grantId?: string
  paths?: string[]
  error?: string
}

type NativeFileDropResult = {
  paths?: string[]
  attachments?: { original_path?: string | null; markdown_path?: string | null }[]
  image_refs?: string[]
  errors?: { filename: string; error: string }[]
  error?: string
}

export interface ChatComposer {
  /** The textarea's text. `v-model` binds straight to it. */
  draft: Ref<string>
  /** Template ref for the textarea. */
  input: Ref<HTMLTextAreaElement | undefined>
  /** True while a drag hovers the panel, for the drop overlay. */
  dragOver: Ref<boolean>
  autoResize(): void
  /** ArrowUp/ArrowDown recall of this session's sent prompts. Returns true
   *  when the key was consumed. */
  handlePromptHistoryKey(e: KeyboardEvent): boolean
  insertTextAtCursor(token: string): void
  insertImageRef(n: number): void
  handleFileSelect(e: Event): Promise<void>
  handlePaste(e: ClipboardEvent): Promise<void>
  handleDrop(e: DragEvent): Promise<void>
  removePendingImage(index: number): void
  handleNativeFileDragEnter(): void
  handleNativeFileDragLeave(): void
  handleNativeFileDrop(event: Event): void
  /** Clear the draft and its persisted copy. Called once a send is accepted. */
  clearDraft(chatId: string): void
  /** Write the user's own draft back to storage. Called when the panel
   *  unmounts, so a recalled history entry is never mistaken for a draft. */
  persistDraft(): void
}

export function useChatComposer(options: ChatComposerOptions): ChatComposer {
  const { draftChatId, store } = options
  const doFetch: typeof fetch = options.fetchImpl ?? ((...args) => fetch(...args))

  const draft = ref(readChatDraft(draftChatId))
  const input = ref<HTMLTextAreaElement>()
  const dragOver = ref(false)

  const promptHistoryIndex = ref(-1)
  const promptHistoryDraft = ref('')
  let settingPromptHistoryText = false

  const chatId = () => toValue(options.chatId)

  // Persist synchronously to avoid losing the last keystroke when switching
  // chats immediately after typing.
  watch(draft, (text) => {
    if (settingPromptHistoryText) return
    promptHistoryIndex.value = -1
    promptHistoryDraft.value = ''
    writeChatDraft(draftChatId, text, undefined, {
      projectId: toValue(options.projectId),
      workspace: toValue(options.workspace),
    })
  }, { flush: 'sync' })

  function clearDraft(id: string): void {
    writeChatDraft(id, '')
    draft.value = ''
  }

  function persistDraft(): void {
    writeChatDraft(
      draftChatId,
      promptHistoryIndex.value < 0 ? draft.value : promptHistoryDraft.value,
      undefined,
      { projectId: toValue(options.projectId), workspace: toValue(options.workspace) },
    )
  }

  function promptHistory(): string[] {
    return readSentPromptHistory(draftChatId)
  }

  function setPromptHistoryText(text: string): void {
    settingPromptHistoryText = true
    draft.value = text
    settingPromptHistoryText = false
    nextTick(() => autoResize())
  }

  function handlePromptHistoryKey(e: KeyboardEvent): boolean {
    if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return false
    const history = promptHistory()
    if (!history.length) return false

    if (e.key === 'ArrowUp') {
      if (promptHistoryIndex.value < 0 && draft.value.trim() !== '') return false
      e.preventDefault()
      if (promptHistoryIndex.value < 0) promptHistoryDraft.value = draft.value
      promptHistoryIndex.value = promptHistoryIndex.value < 0
        ? history.length - 1
        : Math.max(0, promptHistoryIndex.value - 1)
      setPromptHistoryText(history[promptHistoryIndex.value])
      return true
    }

    if (promptHistoryIndex.value < 0) return false
    e.preventDefault()
    if (promptHistoryIndex.value >= history.length - 1) {
      promptHistoryIndex.value = -1
      setPromptHistoryText(promptHistoryDraft.value)
      promptHistoryDraft.value = ''
    } else {
      promptHistoryIndex.value += 1
      setPromptHistoryText(history[promptHistoryIndex.value])
    }
    return true
  }

  function autoResize(): void {
    const el = input.value
    if (!el) return
    el.style.height = 'auto'
    // Floor at the shared touch target so an empty composer stays aligned
    // with the sidebar "+ New Project" row (both 44px inside 61px footers).
    const next = Math.min(Math.max(el.scrollHeight, 44), 200)
    el.style.height = next + 'px'
    const bar = el.closest('.input-bar')
    if (!bar) return
    const isTall = bar.classList.contains('tall')
    // Hysteresis: once tall, stay tall until the text shrinks by ~2 lines;
    // once short, stay short until it grows past the threshold. This stops
    // the buttons from flickering when typing hovers near the boundary.
    const enterTall = el.scrollHeight >= 120
    const leaveTall = el.scrollHeight < 80
    if (!isTall && enterTall) {
      bar.classList.add('tall')
    } else if (isTall && leaveTall) {
      bar.classList.remove('tall')
    }
  }

  function insertTextAtCursor(token: string): void {
    const el = input.value
    if (!el) return
    const start = el.selectionStart ?? 0
    const end = el.selectionEnd ?? 0
    const before = draft.value.slice(0, start)
    const after = draft.value.slice(end)
    // Add a leading space if we're appending to existing text and the token
    // isn't at the start or already preceded by whitespace.
    const prefix = start > 0 && !/\s$/.test(before) ? ' ' : ''
    // Add a trailing space so the user can keep typing.
    const suffix = ' '
    draft.value = before + prefix + token + suffix + after
    nextTick(() => {
      const pos = start + prefix.length + token.length + suffix.length
      el.selectionStart = el.selectionEnd = pos
      el.focus()
    })
  }

  function insertImageRef(n: number): void {
    insertTextAtCursor(`[Image ${n}]`)
  }

  async function handleFileSelect(e: Event): Promise<void> {
    const el = e.target as HTMLInputElement
    if (!el.files?.length) return
    await store.uploadImages(chatId(), Array.from(el.files))
    el.value = ''
  }

  async function handlePaste(e: ClipboardEvent): Promise<void> {
    const items = Array.from(e.clipboardData?.items || []).filter(i => i.type.startsWith('image/'))
    if (!items.length) return
    e.preventDefault()
    await store.uploadImages(chatId(), items.map(i => i.getAsFile()).filter(Boolean) as File[])
  }

  function removePendingImage(index: number): void {
    store.removePendingImage(index)
  }

  async function importNativeFileDrop(detail: NativeFileDropDetail): Promise<void> {
    dragOver.value = false
    if (detail.error || !detail.grantId) {
      store.pushErrorToast(
        'Could not attach file',
        detail.error || 'The native file-drop grant was missing.',
      )
      return
    }
    try {
      const response = await doFetch('/api/desktop-drop', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          grant_id: detail.grantId,
          project_id: toValue(options.projectId) || '',
          chat_id: chatId(),
        }),
      })
      const result = await response.json().catch(() => ({})) as NativeFileDropResult
      if (!response.ok) {
        throw new Error(result.error || `Native file import failed (HTTP ${response.status})`)
      }
      for (const failure of result.errors || []) {
        store.pushErrorToast(`Could not attach ${failure.filename}`, failure.error)
      }
      const paths = (result.attachments || []).flatMap((entry) =>
        [entry.original_path, entry.markdown_path].filter((path): path is string => Boolean(path)))
      paths.push(...(result.paths || []))
      if (paths.length) {
        insertTextAtCursor(paths.map(formatAttachedFilePath).join(' '))
      }
      store.addPendingImageRefs(chatId(), result.image_refs || [])
    } catch (error) {
      store.pushErrorToast(
        'Could not attach file',
        error instanceof Error ? error.message : String(error),
      )
    }
  }

  function handleNativeFileDragEnter(): void {
    dragOver.value = true
  }

  function handleNativeFileDragLeave(): void {
    dragOver.value = false
  }

  function handleNativeFileDrop(event: Event): void {
    const detail = (event as CustomEvent<NativeFileDropDetail>).detail || {}
    void importNativeFileDrop(detail)
  }

  async function localDropNeedsUpload(): Promise<boolean> {
    try {
      // This endpoint is deliberately handled by the local node instead of the
      // client proxy, so it reveals whether the browser and agent are on
      // different computers.
      const response = await doFetch('/api/startup-status', {
        credentials: 'same-origin',
      })
      if (!response.ok) return true
      const role = String((await response.json()).node_role || '')
      return role === 'client' || role === 'standby'
    } catch {
      // Uploading is the safe fallback: a local-only path would be unusable if
      // this browser turns out to be connected to a remote host.
      return true
    }
  }

  async function uploadDroppedProjectFiles(files: File[]): Promise<string[]> {
    if (!toValue(options.vaultFolder)) {
      throw new Error('This project has no folder for uploaded files.')
    }
    const form = new FormData()
    files.forEach((file, index) => form.append(`file${index}`, file, file.name))
    const response = await doFetch(`/api/chats/${chatId()}/attachments`, {
      method: 'POST',
      credentials: 'same-origin',
      body: form,
    })
    const result = await response.json().catch(() => ({})) as ProjectUploadResult
    if (!response.ok) {
      throw new Error(result.error || `Upload failed (HTTP ${response.status})`)
    }
    for (const failure of result.errors || []) {
      store.pushErrorToast(`Could not attach ${failure.filename}`, failure.error)
    }
    return (result.saved || []).flatMap((file) => {
      const paths = [file.original_path, file.markdown_path]
      return (paths.some(Boolean) ? paths : [file.absolute_path || file.vault_path])
        .filter((path): path is string => Boolean(path))
    })
  }

  async function handleDrop(e: DragEvent): Promise<void> {
    dragOver.value = false
    const dt = e.dataTransfer
    if (!dt) return

    // Capture DataTransfer contents synchronously; browsers may invalidate the
    // drag store once this event handler yields to the startup-status request.
    const files: File[] = []
    const folders: { name: string; file: File | null }[] = []
    const items = Array.from(dt.items || [])
    if (items.length) {
      for (const item of items) {
        if (item.kind !== 'file') continue
        const entry = (item as DataTransferItem & {
          webkitGetAsEntry?: () => { isDirectory?: boolean; name?: string } | null
        }).webkitGetAsEntry?.()
        if (entry?.isDirectory) {
          folders.push({ name: entry.name || 'folder', file: item.getAsFile() })
          continue
        }
        const file = item.getAsFile()
        if (file) files.push(file)
      }
    } else {
      files.push(...Array.from(dt.files || []))
    }

    const imageFiles = files.filter(file => file.type.startsWith('image/'))
    const regularFiles = files.filter(file => !file.type.startsWith('image/'))
    const paths: string[] = []
    const needsUpload = regularFiles.length || folders.length
      ? await localDropNeedsUpload()
      : false

    const unavailableFolders: string[] = []
    for (const folder of folders) {
      const nativePath = needsUpload || !folder.file
        ? null
        : nativeAbsoluteFilePath(folder.file)
      if (nativePath) paths.push(nativePath)
      else unavailableFolders.push(folder.name)
    }
    if (unavailableFolders.length) {
      store.pushErrorToast(
        'Could not attach folder',
        'Drop individual files instead; remote clients and sandboxed browsers cannot expose an absolute folder path.',
      )
    }

    if (regularFiles.length) {
      const uploadFiles: File[] = []
      for (const file of regularFiles) {
        const nativePath = needsUpload ? null : nativeAbsoluteFilePath(file)
        if (nativePath) paths.push(nativePath)
        else uploadFiles.push(file)
      }
      if (uploadFiles.length) {
        try {
          paths.push(...await uploadDroppedProjectFiles(uploadFiles))
        } catch (error) {
          store.pushErrorToast(
            'Could not attach file',
            error instanceof Error ? error.message : String(error),
          )
        }
      }
    }

    if (paths.length) {
      insertTextAtCursor(paths.map(formatAttachedFilePath).join(' '))
    }
    if (imageFiles.length) await store.uploadImages(chatId(), imageFiles)
  }

  return {
    draft,
    input,
    dragOver,
    autoResize,
    handlePromptHistoryKey,
    insertTextAtCursor,
    insertImageRef,
    handleFileSelect,
    handlePaste,
    handleDrop,
    removePendingImage,
    handleNativeFileDragEnter,
    handleNativeFileDragLeave,
    handleNativeFileDrop,
    clearDraft,
    persistDraft,
  }
}
