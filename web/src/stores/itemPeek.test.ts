// @vitest-environment jsdom

/**
 * The peek store holds a target, nothing more.
 *
 * The task surface that a target opens owns the read, the editor, resolution
 * and delegation; a store read beside it was a second authority that could
 * disagree with what the user was editing. So this covers the contract the host
 * relies on: a target records the workspace it opened in, a same-id open is a
 * no-op (a self-link must not reset the open editor), a different id replaces
 * it, and a workspace change that bypassed the switch guard closes it.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick, ref } from 'vue'

vi.mock('../lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn() },
}))
vi.mock('./projects', async () => {
  const { ref: makeRef } = await import('vue')
  const activeWorkspace = makeRef('personal')
  return {
    useProjectStore: () => ({
      get activeWorkspace() {
        return activeWorkspace.value
      },
    }),
    __activeWorkspace: activeWorkspace,
  }
})

import { api } from '../lib/api'
import * as projects from './projects'
import { useItemPeekStore } from './itemPeek'

const get = vi.mocked(api.get)
const activeWorkspace = (projects as unknown as { __activeWorkspace: ReturnType<typeof ref<string>> }).__activeWorkspace

beforeEach(() => {
  setActivePinia(createPinia())
  get.mockReset()
  activeWorkspace.value = 'personal'
})

describe('itemPeek store', () => {
  it('records the target and the workspace it opened in', () => {
    const peek = useItemPeekStore()
    expect(peek.isOpen).toBe(false)

    peek.openTask('ship')

    expect(peek.isOpen).toBe(true)
    expect(peek.target).toEqual({ kind: 'task', id: 'ship', workspace: 'personal' })
  })

  it('reads nothing: the surface owns the task', async () => {
    const peek = useItemPeekStore()
    peek.openTask('ship')
    await nextTick()
    await Promise.resolve()
    expect(get).not.toHaveBeenCalled()
  })

  it('does nothing when the same task is already the target', () => {
    const peek = useItemPeekStore()
    peek.openTask('ship')
    const first = peek.target

    // A task linking to itself, or a second click on its own link, must leave
    // the editor the user is typing in exactly where it is.
    peek.openTask('ship')

    expect(peek.target).toBe(first)
  })

  it('replaces the target when another task is opened', () => {
    const peek = useItemPeekStore()
    peek.openTask('ship')
    peek.openTask('doing')

    expect(peek.target).toEqual({ kind: 'task', id: 'doing', workspace: 'personal' })
  })

  it('close clears the target', () => {
    const peek = useItemPeekStore()
    peek.openTask('ship')
    peek.close()

    expect(peek.isOpen).toBe(false)
    expect(peek.target).toBeNull()
  })

  it('a workspace change that bypassed the guard closes the target', async () => {
    const peek = useItemPeekStore()
    peek.openTask('ship')

    // Rename, delete or an external change: the guarded switch is the normal
    // path, and this is the cleanup for the ones that are not.
    activeWorkspace.value = 'work'
    await nextTick()

    expect(peek.isOpen).toBe(false)
  })
})
