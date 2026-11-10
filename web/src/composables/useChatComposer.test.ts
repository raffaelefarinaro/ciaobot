// @vitest-environment jsdom
//
// The point of this file: every assertion below runs against the composable
// alone. Nothing here mounts ChatPanel, creates a Pinia store or a router —
// the composer's draft, prompt-history recall, textarea sizing and the
// attachment paths are all reachable from plain refs and a stub store.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import { renumberImageRefs, useChatComposer, type ComposerAttachmentStore } from './useChatComposer'
import { recordSentPrompt, readChatDraft, writeChatDraft } from '../lib/chatDrafts'
import type { FormPoster, UploadProgressHandler } from '../lib/chatAttachments'

const CHAT_ID = 'chat-1'

function makeStore() {
  return {
    uploadImages: vi.fn(async (_chatId: string, _files: File[], _onProgress?: UploadProgressHandler) => [] as string[]),
    addPendingImageRefs: vi.fn(),
    removePendingImage: vi.fn(),
    pendingImagesFor: vi.fn((_chatId: string) => [] as string[]),
    addPendingFiles: vi.fn(),
    removePendingFile: vi.fn(),
    pushErrorToast: vi.fn(),
  } satisfies ComposerAttachmentStore & Record<string, unknown>
}

function attachTextarea(
  composer: { input: { value: HTMLTextAreaElement | undefined } },
  value = '',
  { tall = false }: { tall?: boolean } = {},
): HTMLTextAreaElement {
  const bar = document.createElement('div')
  bar.className = tall ? 'input-bar tall' : 'input-bar'
  const el = document.createElement('textarea')
  el.value = value
  el.selectionStart = value.length
  el.selectionEnd = value.length
  bar.appendChild(el)
  document.body.appendChild(bar)
  composer.input.value = el
  return el
}

/** jsdom always reports scrollHeight 0; pin it so the sizing rules are testable. */
function pinScrollHeight(el: HTMLTextAreaElement, px: number): void {
  Object.defineProperty(el, 'scrollHeight', { value: px, configurable: true })
}

function makeComposer(overrides: Partial<Parameters<typeof useChatComposer>[0]> = {}) {
  const store = overrides.store ?? makeStore()
  const composer = useChatComposer({
    draftChatId: CHAT_ID,
    chatId: CHAT_ID,
    projectId: 'project-1',
    workspace: 'personal',
    vaultFolder: 'memory-vault/personal/Projects/One',
    ...overrides,
    store,
  })
  return { composer, store: store as ReturnType<typeof makeStore> }
}

function keyEvent(key: string): KeyboardEvent {
  return new KeyboardEvent('keydown', { key, cancelable: true })
}

beforeEach(() => {
  localStorage.clear()
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

describe('useChatComposer draft ownership', () => {
  it('restores the stored draft and writes every keystroke back synchronously', () => {
    writeChatDraft(CHAT_ID, 'half typed')
    const { composer } = makeComposer()

    expect(composer.draft.value).toBe('half typed')

    composer.draft.value = 'half typed more'
    // `flush: 'sync'` — no await: switching chats right after a keystroke
    // must not be able to outrun the write.
    expect(readChatDraft(CHAT_ID)).toBe('half typed more')
  })

  it('tags the persisted draft with the project and workspace it was typed in', () => {
    const { composer } = makeComposer()
    composer.draft.value = 'note to self'

    const stored = JSON.parse(localStorage.getItem('ciao-chat-drafts') || '{}')
    expect(stored[CHAT_ID]).toMatchObject({
      text: 'note to self',
      projectId: 'project-1',
      workspace: 'personal',
    })
  })

  it('clears the draft and its stored copy once a send is accepted', () => {
    const { composer } = makeComposer()
    composer.draft.value = 'sent text'

    composer.clearDraft(CHAT_ID)

    expect(composer.draft.value).toBe('')
    expect(readChatDraft(CHAT_ID)).toBe('')
  })
})

describe('useChatComposer prompt history', () => {
  beforeEach(() => {
    recordSentPrompt(CHAT_ID, 'first prompt')
    recordSentPrompt(CHAT_ID, 'second prompt')
  })

  it('walks the session history on ArrowUp and restores the draft on ArrowDown', async () => {
    const { composer } = makeComposer()
    attachTextarea(composer)

    const up = keyEvent('ArrowUp')
    expect(composer.handlePromptHistoryKey(up)).toBe(true)
    expect(up.defaultPrevented).toBe(true)
    expect(composer.draft.value).toBe('second prompt')

    expect(composer.handlePromptHistoryKey(keyEvent('ArrowUp'))).toBe(true)
    expect(composer.draft.value).toBe('first prompt')

    expect(composer.handlePromptHistoryKey(keyEvent('ArrowDown'))).toBe(true)
    expect(composer.draft.value).toBe('second prompt')

    expect(composer.handlePromptHistoryKey(keyEvent('ArrowDown'))).toBe(true)
    expect(composer.draft.value).toBe('')
    await nextTick()
  })

  it('leaves the arrows alone while a prompt is being edited', () => {
    const { composer } = makeComposer()
    composer.draft.value = 'mid-edit'

    expect(composer.handlePromptHistoryKey(keyEvent('ArrowUp'))).toBe(false)
    expect(composer.handlePromptHistoryKey(keyEvent('ArrowDown'))).toBe(false)
    expect(composer.draft.value).toBe('mid-edit')
  })

  it('declines keys that are not the recall arrows', () => {
    const { composer } = makeComposer()
    expect(composer.handlePromptHistoryKey(keyEvent('Enter'))).toBe(false)
  })

  it('persists the draft that recall replaced, not the recalled prompt', () => {
    const { composer } = makeComposer()
    attachTextarea(composer)

    // Recall only starts from an empty composer, so what it displaces is the
    // empty draft — persisting the recalled prompt instead would turn a
    // glance at history into an unsent message on the next open.
    composer.handlePromptHistoryKey(keyEvent('ArrowUp'))
    expect(composer.draft.value).toBe('second prompt')

    composer.persistDraft()
    expect(readChatDraft(CHAT_ID)).toBe('')
  })
})

describe('useChatComposer textarea sizing', () => {
  it('floors the height at the 44px touch target and caps it at 200px', () => {
    const { composer } = makeComposer()
    const el = attachTextarea(composer)

    pinScrollHeight(el, 18)
    composer.autoResize()
    expect(el.style.height).toBe('44px')

    pinScrollHeight(el, 900)
    composer.autoResize()
    expect(el.style.height).toBe('200px')
  })

  it('applies hysteresis to the tall input bar so the buttons cannot flicker', () => {
    const { composer } = makeComposer()
    const el = attachTextarea(composer)
    const bar = el.parentElement as HTMLElement

    pinScrollHeight(el, 100)
    composer.autoResize()
    expect(bar.classList.contains('tall')).toBe(false)

    pinScrollHeight(el, 120)
    composer.autoResize()
    expect(bar.classList.contains('tall')).toBe(true)

    // Between the two thresholds: stays tall.
    pinScrollHeight(el, 100)
    composer.autoResize()
    expect(bar.classList.contains('tall')).toBe(true)

    pinScrollHeight(el, 60)
    composer.autoResize()
    expect(bar.classList.contains('tall')).toBe(false)
  })
})

describe('useChatComposer cursor insertion', () => {
  it('inserts at the caret with a separating space and leaves the caret after the token', async () => {
    const { composer } = makeComposer()
    const el = attachTextarea(composer, 'look at')
    composer.draft.value = 'look at'

    composer.insertTextAtCursor('`/tmp/a.md`')
    expect(composer.draft.value).toBe('look at `/tmp/a.md` ')

    // v-model would have pushed the new text into the element by now; the
    // caret is restored on the following tick.
    el.value = composer.draft.value
    await nextTick()
    expect(el.selectionStart).toBe(composer.draft.value.length)
  })

  it('does not add a leading space at the start of an empty draft', () => {
    const { composer } = makeComposer()
    attachTextarea(composer, '')

    composer.insertTextAtCursor('`/tmp/a.md`')
    expect(composer.draft.value).toBe('`/tmp/a.md` ')
  })

})

describe('useChatComposer attachments', () => {
  function imageFile(name = 'shot.png'): File {
    return new File([new Uint8Array([1])], name, { type: 'image/png' })
  }

  function textFile(name: string, path?: string): File {
    const file = new File(['hello'], name, { type: 'text/plain' })
    if (path) Object.defineProperty(file, 'path', { value: path })
    return file
  }

  function dropEvent(files: File[]): DragEvent {
    const event = new Event('drop') as DragEvent
    Object.defineProperty(event, 'dataTransfer', {
      value: { items: [], files },
    })
    return event
  }

  function jsonResponse(body: unknown, ok = true): Response {
    return { ok, status: ok ? 200 : 500, json: async () => body } as Response
  }

  /** A stub for the XHR poster: resolves 200 with the given body. */
  function fakePostForm(body: unknown) {
    return vi.fn<FormPoster>(async () => ({ status: 200, body }))
  }

  function pickerInput(files: File[]): HTMLInputElement {
    const input = document.createElement('input')
    input.type = 'file'
    Object.defineProperty(input, 'files', { value: files, configurable: true })
    return input
  }

  it('uploads pasted images and stops the browser inserting them itself', async () => {
    const { composer, store } = makeComposer()
    const file = imageFile()
    const event = {
      preventDefault: vi.fn(),
      clipboardData: { items: [{ type: 'image/png', getAsFile: () => file }] },
    } as unknown as ClipboardEvent

    await composer.handlePaste(event)

    expect(event.preventDefault).toHaveBeenCalled()
    expect(store.uploadImages).toHaveBeenCalledWith(CHAT_ID, [file], expect.any(Function))
  })

  it('ignores a paste that carries no image', async () => {
    const { composer, store } = makeComposer()
    const event = {
      preventDefault: vi.fn(),
      clipboardData: { items: [{ type: 'text/plain', getAsFile: () => null }] },
    } as unknown as ClipboardEvent

    await composer.handlePaste(event)

    expect(event.preventDefault).not.toHaveBeenCalled()
    expect(store.uploadImages).not.toHaveBeenCalled()
  })

  it('drops an image straight into the pending uploads', async () => {
    const { composer, store } = makeComposer()
    attachTextarea(composer, '')
    const file = imageFile()

    await composer.handleDrop(dropEvent([file]))

    expect(store.uploadImages).toHaveBeenCalledWith(CHAT_ID, [file], expect.any(Function))
    expect(composer.draft.value).toBe('')
    expect(composer.dragOver.value).toBe(false)
  })

  it('uploads a dropped file and stages it as a file chip', async () => {
    const ref = `drop_${'a'.repeat(32)}`
    const postForm = fakePostForm({ file_refs: [{ ref, name: 'notes.md' }] })
    const { composer, store } = makeComposer({ postForm })
    attachTextarea(composer, '')

    await composer.handleDrop(dropEvent([textFile('notes.md', '/Users/me/notes.md')]))

    expect(postForm.mock.calls[0][0]).toContain(`/api/chats/${CHAT_ID}/attachments`)
    expect(store.addPendingFiles).toHaveBeenCalledWith(CHAT_ID, [{ ref, name: 'notes.md' }])
    expect(composer.draft.value).toBe('')
  })

  it('uploads a dropped file instead of exposing a browser path', async () => {
    const ref = `drop_${'b'.repeat(32)}`
    const postForm = fakePostForm({ file_refs: [{ ref, name: 'notes.md' }] })
    const { composer, store } = makeComposer({ postForm })
    attachTextarea(composer, '')

    await composer.handleDrop(dropEvent([textFile('notes.md', '/Users/me/notes.md')]))

    expect(postForm.mock.calls[0][0]).toContain(`/api/chats/${CHAT_ID}/attachments`)
    expect(composer.draft.value).not.toContain('/Users/me')
    expect(store.addPendingFiles).toHaveBeenCalledWith(CHAT_ID, [{ ref, name: 'notes.md' }])
  })

  it('reports an upload the project has no folder for', async () => {
    const postForm = fakePostForm({ file_refs: [] })
    const { composer, store } = makeComposer({ postForm, vaultFolder: undefined })
    attachTextarea(composer, '')

    await composer.handleDrop(dropEvent([textFile('notes.md')]))

    expect(store.pushErrorToast).toHaveBeenCalledWith(
      'Could not attach file',
      'This project has no folder for uploaded files.',
    )
    expect(postForm).not.toHaveBeenCalled()
    expect(composer.uploads.value).toEqual([])
    expect(composer.draft.value).toBe('')
  })

  it('imports a native desktop drop as staged files plus pending image refs', async () => {
    const ref = `drop_${'c'.repeat(32)}`
    const fetchImpl = vi.fn(async () => jsonResponse({
      file_refs: [{ ref, name: 'a.pdf' }],
      image_refs: ['img-1'],
    })) as unknown as typeof fetch
    const { composer, store } = makeComposer({ fetchImpl })
    attachTextarea(composer, '')

    composer.handleNativeFileDragEnter()
    expect(composer.dragOver.value).toBe(true)

    composer.handleNativeFileDrop(new CustomEvent('ciao:native-file-drop', {
      detail: { grantId: 'grant-1' },
    }))
    await vi.waitFor(() => expect(store.addPendingImageRefs).toHaveBeenCalled())

    expect(composer.draft.value).toBe('')
    expect(store.addPendingFiles).toHaveBeenCalledWith(CHAT_ID, [{ ref, name: 'a.pdf' }])
    expect(store.addPendingImageRefs).toHaveBeenCalledWith(CHAT_ID, ['img-1'])
    expect(composer.dragOver.value).toBe(false)
  })

  it('surfaces a native drop that arrived without a grant', async () => {
    const { composer, store } = makeComposer()

    composer.handleNativeFileDrop(new CustomEvent('ciao:native-file-drop', { detail: {} }))
    await vi.waitFor(() => expect(store.pushErrorToast).toHaveBeenCalled())

    expect(store.pushErrorToast).toHaveBeenCalledWith(
      'Could not attach file',
      'The native file-drop grant was missing.',
    )
  })

  it('clears the drag overlay when the native drag leaves', () => {
    const { composer } = makeComposer()
    composer.handleNativeFileDragEnter()
    composer.handleNativeFileDragLeave()
    expect(composer.dragOver.value).toBe(false)
  })

  it('uploads picked images and resets the file input so the same file can be picked twice', async () => {
    const { composer, store } = makeComposer()
    const file = imageFile()
    const input = document.createElement('input')
    input.type = 'file'
    Object.defineProperty(input, 'files', { value: [file], configurable: true })

    await composer.handleFileSelect({ target: input } as unknown as Event)

    expect(store.uploadImages).toHaveBeenCalledWith(CHAT_ID, [file], expect.any(Function))
    expect(input.value).toBe('')
  })

  it('forwards a pending-image removal to the store', () => {
    const { composer, store } = makeComposer()
    composer.removePendingImage(2)
    expect(store.removePendingImage).toHaveBeenCalledWith(CHAT_ID, 2)
  })

  it('writes each uploaded image reference into the prompt', async () => {
    const { composer, store } = makeComposer()
    composer.draft.value = 'look at'
    const el = attachTextarea(composer, 'look at')
    store.uploadImages.mockResolvedValueOnce(['b', 'c'])
    store.pendingImagesFor.mockReturnValue(['a', 'b', 'c'])
    const input = document.createElement('input')
    input.type = 'file'
    Object.defineProperty(input, 'files', { value: [imageFile(), imageFile()], configurable: true })

    await composer.handleFileSelect({ target: input } as unknown as Event)

    expect(composer.draft.value).toBe('look at [Image 2] [Image 3] ')
    el.parentElement?.remove()
  })

  it('inserts no reference when the chat changed during the upload', async () => {
    let live = CHAT_ID
    const { composer, store } = makeComposer({ chatId: () => live })
    attachTextarea(composer, '')
    store.uploadImages.mockImplementationOnce(async () => { live = 'other-chat'; return ['a'] })
    store.pendingImagesFor.mockReturnValue(['a'])
    const input = document.createElement('input')
    input.type = 'file'
    Object.defineProperty(input, 'files', { value: [imageFile()], configurable: true })

    await composer.handleFileSelect({ target: input } as unknown as Event)

    expect(composer.draft.value).toBe('')
  })

  it('does not move focus into an unfocused prompt when an upload lands', async () => {
    const { composer, store } = makeComposer()
    const el = attachTextarea(composer, '')
    const other = document.createElement('button')
    document.body.appendChild(other)
    other.focus()
    store.uploadImages.mockResolvedValueOnce(['a'])
    store.pendingImagesFor.mockReturnValue(['a'])
    const input = document.createElement('input')
    input.type = 'file'
    Object.defineProperty(input, 'files', { value: [imageFile()], configurable: true })

    await composer.handleFileSelect({ target: input } as unknown as Event)
    await nextTick()

    expect(composer.draft.value).toBe('[Image 1] ')
    expect(document.activeElement).toBe(other)
    other.remove(); el.parentElement?.remove()
  })

  it('removing an image drops its reference and renumbers the later ones', () => {
    const { composer, store } = makeComposer()
    composer.draft.value = 'see [Image 1] vs [Image 2] and [Image 3]'
    composer.removePendingImage(1)
    expect(store.removePendingImage).toHaveBeenCalledWith(CHAT_ID, 1)
    expect(composer.draft.value).toBe('see [Image 1] vs and [Image 2]')
  })

  it('renumberImageRefs leaves lower references and surrounding text alone', () => {
    expect(renumberImageRefs('[Image 1] hi', 1)).toBe('hi')
    expect(renumberImageRefs('[Image 1] [Image 2]', 3)).toBe('[Image 1] [Image 2]')
    expect(renumberImageRefs('a [Image 12] b', 2)).toBe('a [Image 11] b')
  })

  it('forwards a pending-file removal to the store', () => {
    const { composer, store } = makeComposer()
    composer.removePendingFile(1)
    expect(store.removePendingFile).toHaveBeenCalledWith(CHAT_ID, 1)
  })

  it('tracks an upload\'s progress until it settles', async () => {
    const { composer, store } = makeComposer()
    let finish: (refs: string[]) => void = () => {}
    store.uploadImages.mockImplementation((_chatId, _files, onProgress) => {
      onProgress?.(50, 100)
      return new Promise<string[]>((resolve) => { finish = resolve })
    })
    const input = pickerInput([imageFile('shot.png')])

    const pending = composer.handleFileSelect({ target: input } as unknown as Event)
    expect(composer.uploads.value).toEqual([{ id: expect.any(Number), label: 'shot.png', loaded: 50, total: 100 }])

    finish([])
    await pending
    expect(composer.uploads.value).toEqual([])
  })

  it('reports a failed image upload and clears the bar', async () => {
    const { composer, store } = makeComposer()
    store.uploadImages.mockRejectedValueOnce(new Error('boom'))

    await composer.handleFileSelect({ target: pickerInput([imageFile()]) } as unknown as Event)

    expect(store.pushErrorToast).toHaveBeenCalledWith('Could not attach images', 'boom')
    expect(composer.uploads.value).toEqual([])
  })

  it('a picked document stages as a file chip and a picked image as a thumbnail', async () => {
    const ref = `drop_${'d'.repeat(32)}`
    const postForm = fakePostForm({ file_refs: [{ ref, name: 'a.md' }] })
    const { composer, store } = makeComposer({ postForm })
    const image = imageFile()

    await composer.handleFileSelect({ target: pickerInput([textFile('a.md'), image]) } as unknown as Event)

    expect(postForm).toHaveBeenCalledTimes(1)
    expect(store.addPendingFiles).toHaveBeenCalledWith(CHAT_ID, [{ ref, name: 'a.md' }])
    expect(store.uploadImages).toHaveBeenCalledTimes(1)
    expect(store.uploadImages).toHaveBeenCalledWith(CHAT_ID, [image], expect.any(Function))
  })

  it('labels a multi-file upload by count', async () => {
    const { composer } = makeComposer({
      postForm: vi.fn<FormPoster>((_url, _form, onProgress) => {
        onProgress?.(1, 2)
        return new Promise(() => {})
      }),
    })

    void composer.handleFileSelect({ target: pickerInput([textFile('a.md'), textFile('b.md')]) } as unknown as Event)

    expect(composer.uploads.value[0].label).toBe('2 files')
  })
})
