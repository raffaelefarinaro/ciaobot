import { beforeEach, describe, expect, it } from 'vitest'
import { resetHomeNoticeWindows, useHomeNoticeWindows } from './useHomeNoticeWindows'

describe('useHomeNoticeWindows reveal', () => {
  beforeEach(() => resetHomeNoticeWindows())

  it('reopen_reopens_only_the_named_key', () => {
    const w = useHomeNoticeWindows()
    w.setAvailable('o', ['a', 'b'])
    w.close('a')
    w.close('b')
    expect(w.closedCount.value).toBe(2)
    expect(w.reopen('a')).toBe(true)
    expect(w.isClosed('a')).toBe(false)
    expect(w.isClosed('b')).toBe(true)
    expect(w.closedCount.value).toBe(1)
    w.setAvailable('o', ['a', 'b', 'c'])
    expect(w.closedCount.value).toBe(1)
    expect(w.reopen('a')).toBe(false)
    expect(w.reopen('unknown')).toBe(false)
    expect(w.isClosed('b')).toBe(true)
    expect(w.closedCount.value).toBe(1)
  })

  it('reveal_request_roundtrip_and_reset', () => {
    const w = useHomeNoticeWindows()
    expect(w.pendingReveal.value).toBe('')
    w.requestReveal('action:shared:x')
    expect(w.pendingReveal.value).toBe('action:shared:x')
    w.clearReveal()
    expect(w.pendingReveal.value).toBe('')
    w.requestReveal('action:shared:y')
    resetHomeNoticeWindows()
    expect(w.pendingReveal.value).toBe('')
  })
})
