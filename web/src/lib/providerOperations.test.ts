import { describe, expect, it } from 'vitest'
import { providerMutation, providerRead, providerReadIsCurrent, waitForProviderMutation } from './providerOperations'

describe('provider mutation lock', () => {
  it('excludes mutations across owners and releases after failure', async () => {
    let reject!: (reason: Error) => void
    const first = providerMutation(() => new Promise<void>((_, fail) => { reject = fail }))!
    expect(providerMutation(async () => {})).toBeNull()
    const waiting = waitForProviderMutation()
    expect(waiting).not.toBeNull()
    reject(new Error('failed'))
    await expect(first).rejects.toThrow('failed')
    await waiting
    expect(await providerMutation(async () => 'done')).toBe('done')
  })

  it('coalesces concurrent reads and invalidates an old read as soon as a mutation starts', async () => {
    let finish!: (value: { connections: {} }) => void
    let calls = 0
    const read = () => { calls++; return new Promise<{ connections: {} }>(resolve => { finish = resolve }) }
    const first = providerRead(read)
    const second = providerRead(read)
    expect(calls).toBe(1)
    let release!: () => void
    const mutation = providerMutation(() => new Promise<void>(resolve => { release = resolve }))!
    finish({ connections: {} })
    const [a, b] = await Promise.all([first, second])
    expect(a).toEqual(b)
    expect(providerReadIsCurrent(a.generation)).toBe(false)
    release()
    await mutation
  })
})
