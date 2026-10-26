import { beforeEach, describe, expect, test } from 'vitest'
import { askConfirm, askConfirmReason, pendingConfirm } from './confirm'

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

describe('askConfirmReason', () => {
  beforeEach(() => {
    pendingConfirm.value = null
  })

  test('resolves the confirmed flag with the typed reason', async () => {
    const answer = askConfirmReason('Delete it?')
    expect(pendingConfirm.value?.withReason).toBe(true)
    pendingConfirm.value!.reason = '  not mine  '
    pendingConfirm.value!.resolve(true)
    await expect(answer).resolves.toEqual({ confirmed: true, reason: 'not mine' })
  })

  test('cancel resolves with no reason, even if one was typed', async () => {
    const answer = askConfirmReason('Delete it?')
    pendingConfirm.value!.reason = 'typed'
    pendingConfirm.value!.resolve(false)
    await expect(answer).resolves.toEqual({ confirmed: false, reason: '' })
  })

  test('a plain askConfirm has no reason field and still returns a boolean', async () => {
    const answer = askConfirm('Plain?')
    expect(pendingConfirm.value).toMatchObject({ withReason: false, reason: '' })
    pendingConfirm.value!.resolve(true)
    await expect(answer).resolves.toBe(true)
  })
})
