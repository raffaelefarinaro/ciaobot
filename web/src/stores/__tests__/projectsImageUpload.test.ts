// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

const post = vi.hoisted(() => vi.fn())
vi.mock('../../lib/chatAttachments', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../lib/chatAttachments')>()),
  postFormWithProgress: post,
}))

import { useProjectStore } from '../projects'

const REF = `drop_${'e'.repeat(32)}`

// A HEIC photo the engine could not convert comes back from /images as a file
// reference with a notice: the store stages it as a file and tells the user.
describe('projects store uploadImages', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    post.mockReset()
  })

  it('stages converted images, keeps an unconverted HEIC as a file and says so', async () => {
    post.mockResolvedValue({
      status: 200,
      body: [
        { ref: 'web_a.jpg', mime_type: 'image/jpeg', filename: 'IMG_1.HEIC' },
        { filename: 'IMG_2.HEIC', file_ref: { ref: REF, name: 'IMG_2.HEIC' }, notice: 'The agent can read it by path but cannot see it as an image: no converter.' },
      ],
    })
    const store = useProjectStore()
    const photos = [new File(['a'], 'IMG_1.HEIC'), new File(['b'], 'IMG_2.HEIC')]

    const refs = await store.uploadImages('c1', photos)

    expect(post.mock.calls[0][0]).toBe('/api/chats/c1/images')
    expect(refs).toEqual(['web_a.jpg'])
    expect(store.pendingImagesFor('c1')).toEqual(['web_a.jpg'])
    expect(store.pendingFilesFor('c1')).toEqual([{ ref: REF, name: 'IMG_2.HEIC' }])
    expect(store.toasts.map(t => [t.title, t.body, t.variant])).toEqual([
      ['IMG_2.HEIC attached as a file', 'The agent can read it by path but cannot see it as an image: no converter.', undefined],
    ])
  })

  it('tells a comment upload that a HEIC photo it could not convert is not attached', async () => {
    post.mockResolvedValue({
      status: 200,
      body: [{ filename: 'IMG_2.HEIC', file_ref: { ref: REF, name: 'IMG_2.HEIC' }, notice: 'no converter' }],
    })
    const store = useProjectStore()

    // A chat of its own: staged files persist in localStorage across cases.
    const refs = await store.uploadImageRefs('c2', [new File(['b'], 'IMG_2.HEIC')])

    expect(refs).toEqual([])
    expect(store.pendingFilesFor('c2')).toEqual([])
    expect(store.toasts.map(t => t.title)).toEqual(['IMG_2.HEIC not attached'])
    expect(store.toasts[0].body).toContain('Comments take images only')
  })
})
