// @vitest-environment jsdom
//
// The point of this file: every assertion below runs against the composable
// alone. Nothing here mounts ChatPanel, creates a Pinia store or a router —
// the composer's draft, prompt-history recall, textarea sizing and the
// attachment paths are all reachable from plain refs and a stub store.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import { useChatComposer, type ComposerAttachmentStore } from './useChatComposer'
import { recordSentPrompt, readChatDraft, writeChatDraft } from '../lib/chatDrafts'

const CHAT_ID = 'chat-1'

function makeStore() {
  return {
    uploadImages: vi.fn(async () => []),
    addPendingImageRefs: vi.fn(),
    removePendingImage: vi.fn(),
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

  it('inserts a numbered image reference', () => {
    const { composer } = makeComposer()
    attachTextarea(composer, '')

    composer.insertImageRef(2)
    expect(composer.draft.value).toBe('[Image 2] ')
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

  it('uploads pasted images and stops the browser inserting them itself', async () => {
    const { composer, store } = makeComposer()
    const file = imageFile()
    const event = {
      preventDefault: vi.fn(),
      clipboardData: { items: [{ type: 'image/png', getAsFile: () => file }] },
    } as unknown as ClipboardEvent

    await composer.handlePaste(event)

    expect(event.preventDefault).toHaveBeenCalled()
    expect(store.uploadImages).toHaveBeenCalledWith(CHAT_ID, [file])
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

    expect(store.uploadImages).toHaveBeenCalledWith(CHAT_ID, [file])
    expect(composer.draft.value).toBe('')
    expect(composer.dragOver.value).toBe(false)
  })

  it('references a dropped file by absolute path when the agent runs on this machine', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ node_role: 'host' })) as unknown as typeof fetch
    const { composer } = makeComposer({ fetchImpl })
    attachTextarea(composer, '')

    await composer.handleDrop(dropEvent([textFile('notes.md', '/Users/me/notes.md')]))

    expect(composer.draft.value).toBe('`/Users/me/notes.md` ')
    expect(fetchImpl).toHaveBeenCalledTimes(1)
  })

  it('uploads a dropped file instead when the browser is on a client node', async () => {
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes('startup-status')) return jsonResponse({ node_role: 'client' })
      return jsonResponse({ saved: [{ path: 'notes.md', vault_path: 'v/notes.md', original_path: 'v/notes.md' }] })
    }) as unknown as typeof fetch
    const { composer } = makeComposer({ fetchImpl })
    attachTextarea(composer, '')

    await composer.handleDrop(dropEvent([textFile('notes.md', '/Users/me/notes.md')]))

    expect(String((fetchImpl as unknown as ReturnType<typeof vi.fn>).mock.calls[1][0]))
      .toContain(`/api/chats/${CHAT_ID}/attachments`)
    expect(composer.draft.value).toBe('`v/notes.md` ')
  })

  it('reports an upload the project has no folder for', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ node_role: 'client' })) as unknown as typeof fetch
    const { composer, store } = makeComposer({ fetchImpl, vaultFolder: undefined })
    attachTextarea(composer, '')

    await composer.handleDrop(dropEvent([textFile('notes.md')]))

    expect(store.pushErrorToast).toHaveBeenCalledWith(
      'Could not attach file',
      'This project has no folder for uploaded files.',
    )
    expect(composer.draft.value).toBe('')
  })

  it('imports a native desktop drop as paths plus pending image refs', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({
      attachments: [{ original_path: '/Users/me/a.pdf', markdown_path: '/Users/me/a.md' }],
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

    expect(composer.draft.value).toBe('`/Users/me/a.pdf` `/Users/me/a.md` ')
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

    expect(store.uploadImages).toHaveBeenCalledWith(CHAT_ID, [file])
    expect(input.value).toBe('')
  })

  it('forwards a pending-image removal to the store', () => {
    const { composer, store } = makeComposer()
    composer.removePendingImage(2)
    expect(store.removePendingImage).toHaveBeenCalledWith(2)
  })
})
