import { beforeEach, describe, expect, test } from 'vitest'
import { askConfirm, pendingConfirm } from './confirm'

describe('askConfirm', () => {
  beforeEach(() => {
    pendingConfirm.value = null
  })

  test('resolves true only when the request is accepted', async () => {
    const answer = askConfirm('Archive this chat?')
    expect(pendingConfirm.value?.message).toBe('Archive this chat?')
    pendingConfirm.value!.resolve(true)
    await expect(answer).resolves.toBe(true)
    // The dialog clears itself so it does not linger on screen.
    expect(pendingConfirm.value).toBeNull()
  })

  test('resolves false when cancelled', async () => {
    const answer = askConfirm('Delete everything?')
    pendingConfirm.value!.resolve(false)
    await expect(answer).resolves.toBe(false)
  })

  test('applies labels and the destructive flag', () => {
    void askConfirm('Delete workspace?', {
      title: 'Delete workspace',
      confirmLabel: 'Delete',
      cancelLabel: 'Keep',
      destructive: true,
    })
    expect(pendingConfirm.value).toMatchObject({
      title: 'Delete workspace',
      confirmLabel: 'Delete',
      cancelLabel: 'Keep',
      destructive: true,
    })
  })

  test('defaults are non-destructive', () => {
    void askConfirm('Proceed?')
    expect(pendingConfirm.value).toMatchObject({
      title: 'Are you sure?',
      confirmLabel: 'Confirm',
      cancelLabel: 'Cancel',
      destructive: false,
    })
  })

  test('a second request cancels the first instead of orphaning its promise', async () => {
    const first = askConfirm('First?')
    const second = askConfirm('Second?')
    // Without this the first caller would await forever behind the new dialog.
    await expect(first).resolves.toBe(false)
    expect(pendingConfirm.value?.message).toBe('Second?')
    pendingConfirm.value!.resolve(true)
    await expect(second).resolves.toBe(true)
  })

  test('resolving twice is ignored', async () => {
    const answer = askConfirm('Once?')
    const request = pendingConfirm.value!
    request.resolve(true)
    request.resolve(false)
    await expect(answer).resolves.toBe(true)
  })
})
