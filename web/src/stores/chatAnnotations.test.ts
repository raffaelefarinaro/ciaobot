// The annotations slice is a plain factory, so this file exercises it with a
// bare ref for the active chat: no Pinia, no router, no component mount.
import { beforeEach, describe, expect, test, vi } from 'vitest'
import { ref } from 'vue'
import { createChatAnnotations } from './chatAnnotations'

let stored: Record<string, string> = {}

beforeEach(() => {
  stored = {}
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => stored[k] ?? null,
    setItem: (k: string, v: string) => { stored[k] = v },
    removeItem: (k: string) => { delete stored[k] },
    clear: () => { stored = {} },
  })
})

function make(chatId: string | null = 'c1') {
  const activeChatId = ref<string | null>(chatId)
  return { activeChatId, a: createChatAnnotations({ activeChatId }) }
}

describe('pending images', () => {
  test('adds, persists and removes by index', () => {
    const { a } = make()
    a.addPendingImageRefs('c1', ['one.png', 'two.png'])
    expect(a.pendingImages.value).toEqual(['one.png', 'two.png'])
    expect(JSON.parse(stored['ciao-pending-images'])).toEqual({ c1: ['one.png', 'two.png'] })
    a.removePendingImage(0)
    expect(a.pendingImages.value).toEqual(['two.png'])
    a.clearPendingImages()
    expect(a.pendingImages.value).toEqual([])
    expect(stored['ciao-pending-images']).toBe('{}')
  })

  test('an empty add is a no-op', () => {
    const { a } = make()
    a.addPendingImageRefs('c1', [])
    expect(stored['ciao-pending-images']).toBeUndefined()
  })

  test('buckets are per chat and follow the active chat', () => {
    const { activeChatId, a } = make()
    a.addPendingImageRefs('c1', ['a.png'])
    a.addPendingImageRefs('c2', ['b.png'])
    expect(a.pendingImages.value).toEqual(['a.png'])
    activeChatId.value = 'c2'
    expect(a.pendingImages.value).toEqual(['b.png'])
  })

  test('with no active chat there is nothing staged', () => {
    const { a } = make(null)
    expect(a.pendingImages.value).toEqual([])
    a.removePendingImage(0)
    a.clearPendingImages()
    expect(stored['ciao-pending-images']).toBeUndefined()
  })
})

describe('file comments', () => {
  test('a pending comment is also stored durably per file', () => {
    const { a } = make()
    const id = a.addPendingComment({ path: 'notes.md', selection: 'sel', comment: 'why?' })
    expect(a.pendingComments.value.map(c => c.id)).toEqual([id])
    expect(a.fileCommentsFor('notes.md').map(c => c.id)).toEqual([id])
    expect(a.fileCommentsFor('notes.md')[0].createdAt).toBeTruthy()
    expect(JSON.parse(stored['ciao-file-comments'])['notes.md']).toHaveLength(1)
  })

  test('line range defaults to the start line and anchors default to null', () => {
    const { a } = make()
    a.addPendingComment({ path: 'a.md', selection: 's', comment: 'c', lineStart: 4 })
    const entry = a.pendingComments.value[0]
    expect(entry.lineStart).toBe(4)
    expect(entry.lineEnd).toBe(4)
    expect(entry.artifactSelector).toBeNull()
    expect(entry.artifactWholeElement).toBe(false)
  })

  test('removing drops it from both the durable store and the pending queue', () => {
    const { a } = make()
    const id = a.addPendingComment({ path: 'a.md', selection: 's', comment: 'c' })
    a.removeFileComment('a.md', id)
    expect(a.fileCommentsFor('a.md')).toEqual([])
    expect(a.pendingComments.value).toEqual([])
    expect(JSON.parse(stored['ciao-file-comments'])).toEqual({})
  })

  test('editing updates both copies', () => {
    const { a } = make()
    const id = a.addPendingComment({ path: 'a.md', selection: 's', comment: 'first' })
    a.updateFileComment('a.md', id, 'second')
    expect(a.fileCommentsFor('a.md')[0].comment).toBe('second')
    expect(a.pendingComments.value[0].comment).toBe('second')
  })

  test('images sync from the durable copy to the pending one', () => {
    const { a } = make()
    const id = a.addPendingComment({ path: 'a.md', selection: 's', comment: 'c' })
    a.addFileCommentImage('a.md', id, 'img1')
    expect(a.pendingComments.value[0].images).toEqual(['img1'])
    a.addFileCommentImage('a.md', id, 'img1')
    expect(a.fileCommentsFor('a.md')[0].images).toEqual(['img1'])
    a.removeFileCommentImage('a.md', id, 'img1')
    expect(a.fileCommentsFor('a.md')[0].images).toBeUndefined()
    expect(a.pendingComments.value[0].images).toBeUndefined()
  })

  test('unknown paths and ids are ignored', () => {
    const { a } = make()
    a.updateFileComment('nope.md', 'x', 'c')
    a.addFileCommentImage('nope.md', 'x', 'i')
    a.removeFileCommentImage('nope.md', 'x', 'i')
    a.removeFileComment('nope.md', 'x')
    expect(a.pendingComments.value).toEqual([])
  })
})

describe('chat comments', () => {
  test('add, edit, image add/remove and clear', () => {
    const { a } = make()
    const id = a.addPendingChatComment({ selection: 'quoted', comment: 'note' })
    expect(a.pendingChatComments.value).toHaveLength(1)
    a.updatePendingChatComment(id, 'edited')
    expect(a.pendingChatComments.value[0].comment).toBe('edited')
    a.addPendingChatCommentImage(id, 'i1')
    expect(a.pendingChatComments.value[0].images).toEqual(['i1'])
    a.removePendingChatCommentImage(id, 'i1')
    expect(a.pendingChatComments.value[0].images).toBeUndefined()
    a.removePendingChatComment(id)
    expect(a.pendingChatComments.value).toEqual([])
  })

  test('nothing is staged when no chat is active', () => {
    const { a } = make(null)
    a.addPendingChatComment({ selection: 's', comment: 'c' })
    expect(a.pendingChatComments.value).toEqual([])
  })
})

describe('pinned files', () => {
  test('pin, read back and persist', () => {
    const { a } = make()
    a.pinFile('c1', 'docs/a.md')
    expect(a.pinnedFileFor('c1')).toBe('docs/a.md')
    expect(JSON.parse(stored['ciao-pinned-files'])).toEqual({ c1: 'docs/a.md' })
  })

  test('closing a pin records the dismissal for that path only', () => {
    const { a } = make()
    a.pinFile('c1', 'docs/a.md')
    a.unpinFile('c1')
    expect(a.pinnedFileFor('c1')).toBeUndefined()
    expect(a.isAutoPinDismissed('c1', 'docs/a.md')).toBe(true)
    expect(a.isAutoPinDismissed('c1', 'docs/b.md')).toBe(false)
  })

  test('re-pinning the same path clears its dismissal', () => {
    const { a } = make()
    a.pinFile('c1', 'docs/a.md')
    a.unpinFile('c1')
    a.pinFile('c1', 'docs/a.md')
    expect(a.isAutoPinDismissed('c1', 'docs/a.md')).toBe(false)
    expect(JSON.parse(stored['ciao-dismissed-auto-pins'])).toEqual({})
  })

  test('unpinning nothing records nothing', () => {
    const { a } = make()
    a.unpinFile('c1')
    expect(stored['ciao-dismissed-auto-pins']).toBeUndefined()
  })
})

describe('restoreFromStorage', () => {
  test('reads every key back', () => {
    stored['ciao-file-comments'] = JSON.stringify({ 'a.md': [{ id: '1', path: 'a.md', selection: 's', comment: 'c', createdAt: 'T' }] })
    stored['ciao-pinned-files'] = JSON.stringify({ c1: 'a.md' })
    stored['ciao-dismissed-auto-pins'] = JSON.stringify({ c1: ['b.md'] })
    stored['ciao-pending-images'] = JSON.stringify({ c1: ['i.png'] })
    stored['ciao-pending-comments'] = JSON.stringify({ c1: [{ id: '1', path: 'a.md', selection: 's', comment: 'c' }] })
    stored['ciao-pending-chat-comments'] = JSON.stringify({ c1: [{ id: '2', selection: 's', comment: 'c' }] })
    const { a } = make()
    a.restoreFromStorage()
    expect(a.fileCommentsFor('a.md')).toHaveLength(1)
    expect(a.pinnedFileFor('c1')).toBe('a.md')
    expect(a.isAutoPinDismissed('c1', 'b.md')).toBe(true)
    expect(a.pendingImages.value).toEqual(['i.png'])
    expect(a.pendingComments.value).toHaveLength(1)
    expect(a.pendingChatComments.value).toHaveLength(1)
  })

  test('a legacy chat-wide dismissal map is dropped rather than translated', () => {
    stored['ciao-dismissed-auto-pins'] = JSON.stringify({ c1: true, c2: ['x.md'] })
    const { a } = make()
    a.restoreFromStorage()
    expect(a.isAutoPinDismissed('c1', 'anything')).toBe(false)
    expect(a.isAutoPinDismissed('c2', 'x.md')).toBe(true)
  })

  test('a legacy flat pending array folds onto the active chat', () => {
    stored['ciao-pending-images'] = JSON.stringify(['legacy.png'])
    const { a } = make()
    a.restoreFromStorage()
    expect(a.pendingImages.value).toEqual(['legacy.png'])
  })

  test('throws on malformed JSON so the caller can abort the rest of its restore', () => {
    stored['ciao-file-comments'] = '{not json'
    const { a } = make()
    expect(() => a.restoreFromStorage()).toThrow()
  })
})

describe('prepare and consume', () => {
  test('reference blocks lead, the typed prompt follows', () => {
    const { a } = make()
    a.addPendingComment({ path: 'a.md', selection: 'quoted', comment: 'why?' })
    a.addPendingChatComment({ selection: 'said', comment: 'expand' })
    const prepared = a.prepareMessage('c1', '  do it  ')
    expect(prepared.composed.endsWith('do it')).toBe(true)
    expect(prepared.composed.indexOf('quoted')).toBeLessThan(prepared.composed.indexOf('do it'))
    expect(prepared.fileComments).toHaveLength(1)
    expect(prepared.chatComments).toHaveLength(1)
  })

  test('with nothing staged the composed text is just the prompt', () => {
    const { a } = make()
    expect(a.prepareMessage('c1', 'hello')).toEqual({
      composed: 'hello', imageRefs: undefined, fileComments: [], chatComments: [],
    })
  })

  test('image refs are the union of staged images and comment images', () => {
    const { a } = make()
    a.addPendingImageRefs('c1', ['shot.png'])
    const id = a.addPendingComment({ path: 'a.md', selection: 's', comment: 'c' })
    a.addFileCommentImage('a.md', id, 'shot.png')
    a.addPendingChatComment({ selection: 's', comment: 'c', images: ['other.png'] })
    expect(a.prepareMessage('c1', 'go').imageRefs?.sort()).toEqual(['other.png', 'shot.png'])
  })

  test('consuming clears the buckets and the sent durable comments', () => {
    const { a } = make()
    a.addPendingImageRefs('c1', ['shot.png'])
    a.addPendingComment({ path: 'a.md', selection: 's', comment: 'c' })
    a.addPendingChatComment({ selection: 's', comment: 'c' })
    const prepared = a.prepareMessage('c1', 'go')
    a.consumePreparedAttachments('c1', prepared)
    expect(a.pendingImages.value).toEqual([])
    expect(a.pendingComments.value).toEqual([])
    expect(a.pendingChatComments.value).toEqual([])
    expect(a.fileCommentsFor('a.md')).toEqual([])
  })

  test('an unsent comment on the same file survives the send of another', () => {
    const { a } = make()
    a.addPendingComment({ path: 'a.md', selection: 's1', comment: 'c1' })
    const prepared = a.prepareMessage('c1', 'go')
    const kept = a.addPendingComment({ path: 'a.md', selection: 's2', comment: 'c2' })
    a.consumePreparedAttachments('c1', prepared)
    expect(a.fileCommentsFor('a.md').map(c => c.id)).toEqual([kept])
  })
})

describe('hasStagedAttachments', () => {
  test('true for any of the three buckets, false when all are empty', () => {
    const { a } = make()
    expect(a.hasStagedAttachments('c1')).toBe(false)
    a.addPendingImageRefs('c1', ['i.png'])
    expect(a.hasStagedAttachments('c1')).toBe(true)
    a.clearPendingImages()
    expect(a.hasStagedAttachments('c1')).toBe(false)
    a.addPendingComment({ path: 'a.md', selection: 's', comment: 'c' })
    expect(a.hasStagedAttachments('c1')).toBe(true)
    a.clearPendingComments()
    a.addPendingChatComment({ selection: 's', comment: 'c' })
    expect(a.hasStagedAttachments('c1')).toBe(true)
    a.clearPendingChatComments()
    expect(a.hasStagedAttachments('c1')).toBe(false)
  })

  test("another chat's staged material does not count", () => {
    const { a } = make()
    a.addPendingImageRefs('c2', ['i.png'])
    expect(a.hasStagedAttachments('c1')).toBe(false)
    expect(a.hasStagedAttachments('c2')).toBe(true)
  })
})
