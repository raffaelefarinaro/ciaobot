import { ref, toValue, watch, nextTick, type MaybeRefOrGetter, type Ref } from 'vue'
import {
  importDesktopDrop,
  postFormWithProgress,
  uploadChatAttachments,
  type AttachedFile,
  type FormPoster,
  type UploadProgressHandler,
} from '../lib/chatAttachments'
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
  uploadImages(chatId: string, files: File[], onProgress?: UploadProgressHandler): Promise<string[]>
  addPendingImageRefs(chatId: string, refs: string[]): void
  removePendingImage(chatId: string, index: number): void
  pendingImagesFor(chatId: string): string[]
  addPendingFiles(chatId: string, files: AttachedFile[]): void
  removePendingFile(chatId: string, index: number): void
  pushErrorToast(title: string, errorText: string): unknown
}

/** One upload in flight; `ComposerUploadProgress.vue` renders the list. */
export interface ComposerUpload { id: number; label: string; loaded: number; total: number }

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
  /** Injected for tests; defaults to XHR `postFormWithProgress` (fetch cannot report upload progress). */
  postForm?: FormPoster
}

export type NativeFileDropDetail = {
  grantId?: string
  names?: string[]
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
  /** Attach picked files: images stage as thumbnails, others as file chips. */
  handleFileSelect(e: Event): Promise<void>
  handlePaste(e: ClipboardEvent): Promise<void>
  handleDrop(e: DragEvent): Promise<void>
  removePendingImage(index: number): void
  removePendingFile(index: number): void
  /** Uploads in flight. Send is disabled while this is non-empty. */
  uploads: Ref<ComposerUpload[]>
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
  const postForm: FormPoster = options.postForm ?? postFormWithProgress
  const uploads = ref<ComposerUpload[]>([])
  let uploadSeq = 0

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

  // Re-fit on every draft change, not only on the textarea's input event: an
  // inserted [Image N], a renumbered image ref, a slash insert or a recalled
  // prompt sets the draft in code, and a stale height clips the new line.
  // `post` runs after Vue has written the new value into the textarea.
  watch(draft, () => autoResize(), { flush: 'post' })

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
    // at the shared 44px touch target inside the 61px footer.
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

  function insertTextAtCursor(token: string, focus = true): void {
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
      if (focus) el.focus()
    })
  }

  function errorText(error: unknown): string {
    return error instanceof Error ? error.message : String(error)
  }

  function uploadLabel(names: string[]): string {
    return names.length === 1 ? (names[0] || 'file') : `${names.length} files`
  }

  /** Show a bar for one upload until it settles, whether it succeeds or fails. */
  async function trackUpload<T>(
    label: string,
    total: number,
    run: (onProgress: UploadProgressHandler) => Promise<T>,
  ): Promise<T> {
    const id = ++uploadSeq
    uploads.value = [...uploads.value, { id, label, loaded: 0, total }]
    const onProgress: UploadProgressHandler = (loaded, nextTotal) => {
      uploads.value = uploads.value.map(u => (u.id === id ? { ...u, loaded, total: nextTotal } : u))
    }
    try {
      return await run(onProgress)
    } finally {
      uploads.value = uploads.value.filter(u => u.id !== id)
    }
  }

  const totalBytes = (files: File[]) => files.reduce((sum, f) => sum + (f.size || 0), 0)

  async function attachImages(id: string, images: File[]): Promise<void> {
    try {
      const refs = await trackUpload(uploadLabel(images.map(f => f.name)), totalBytes(images),
        onProgress => store.uploadImages(id, images, onProgress))
      // Each new image's reference lands in the prompt at the cursor, so the
      // message says which image it means without a second step.
      const staged = store.pendingImagesFor(id)
      const tokens = refs.map(r => staged.indexOf(r) + 1).filter(n => n > 0).map(n => `[Image ${n}]`)
      if (tokens.length && id === chatId()) {
        insertTextAtCursor(tokens.join(' '), document.activeElement === input.value)
      }
    } catch (error) {
      store.pushErrorToast('Could not attach images', errorText(error))
    }
  }

  async function attachDocuments(id: string, documents: File[]): Promise<void> {
    try {
      if (!toValue(options.vaultFolder)) throw new Error('This project has no folder for uploaded files.')
      const result = await trackUpload(uploadLabel(documents.map(f => f.name)), totalBytes(documents),
        onProgress => uploadChatAttachments(id, documents, onProgress, postForm))
      for (const failure of result.failures) store.pushErrorToast(`Could not attach ${failure.filename}`, failure.error)
      store.addPendingFiles(id, result.files)
    } catch (error) {
      store.pushErrorToast('Could not attach file', errorText(error))
    }
  }

  async function attachFiles(files: File[]): Promise<void> {
    const id = chatId()
    const images = files.filter(f => f.type.startsWith('image/'))
    const documents = files.filter(f => !f.type.startsWith('image/'))
    await Promise.all([
      documents.length ? attachDocuments(id, documents) : Promise.resolve(),
      images.length ? attachImages(id, images) : Promise.resolve(),
    ])
  }

  async function handleFileSelect(e: Event): Promise<void> {
    const el = e.target as HTMLInputElement
    if (!el.files?.length) return
    const files = Array.from(el.files)
    el.value = ''
    await attachFiles(files)
  }

  async function handlePaste(e: ClipboardEvent): Promise<void> {
    const items = Array.from(e.clipboardData?.items || []).filter(i => i.type.startsWith('image/'))
    if (!items.length) return
    e.preventDefault()
    await attachImages(chatId(), items.map(i => i.getAsFile()).filter(Boolean) as File[])
  }

  /** Drop an image and its `[Image N]` from the prompt; later images move up
   *  one, so their references are renumbered to match what is sent. */
  function removePendingImage(index: number): void {
    store.removePendingImage(chatId(), index)
    draft.value = renumberImageRefs(draft.value, index + 1)
  }

  function removePendingFile(index: number): void {
    store.removePendingFile(chatId(), index)
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
    const id = chatId()
    try {
      // Total 0: no bytes leave the browser, so the bar reads "Processing" at once.
      const result = await trackUpload(uploadLabel(detail.names?.length ? detail.names : ['files']), 0,
        () => importDesktopDrop(detail.grantId as string, { chatId: id, projectId: toValue(options.projectId) || '' }, doFetch))
      for (const failure of result.failures) {
        store.pushErrorToast(`Could not attach ${failure.filename}`, failure.error)
      }
      store.addPendingFiles(id, result.files)
      store.addPendingImageRefs(id, result.imageRefs)
    } catch (error) {
      store.pushErrorToast('Could not attach file', errorText(error))
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

  async function handleDrop(e: DragEvent): Promise<void> {
    dragOver.value = false
    const dt = e.dataTransfer
    if (!dt) return

    // Capture DataTransfer contents synchronously before any async upload.
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

    if (folders.length) {
      store.pushErrorToast(
        'Could not attach folder',
        'Drop individual files instead; remote clients and sandboxed browsers cannot expose an absolute folder path.',
      )
    }

    await attachFiles(files)
  }

  return {
    draft,
    input,
    dragOver,
    autoResize,
    handlePromptHistoryKey,
    insertTextAtCursor,
    handleFileSelect,
    handlePaste,
    handleDrop,
    removePendingImage,
    removePendingFile,
    uploads,
    handleNativeFileDragEnter,
    handleNativeFileDragLeave,
    handleNativeFileDrop,
    clearDraft,
    persistDraft,
  }
}

/** Remove `[Image removed]` (with one adjoining space) and shift every higher
 *  `[Image N]` down by one. */
export function renumberImageRefs(text: string, removed: number): string {
  return text
    .replace(new RegExp(`( ?)\\[Image ${removed}\\]( ?)`, 'g'), (_m, a: string, b: string) => (a && b ? ' ' : ''))
    .replace(/\[Image (\d+)\]/g, (m, n: string) => (Number(n) > removed ? `[Image ${Number(n) - 1}]` : m))
}
