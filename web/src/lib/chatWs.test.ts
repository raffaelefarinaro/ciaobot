import { describe, expect, test } from 'vitest'
import {
  chatWsReconnectDelayMs,
  isHostConnectionUnavailableMessage,
  shouldReconnectActiveChatOnStreamingStarted,
} from './chatWs'
import { setListIndex } from './safeList'

describe('shouldReconnectActiveChatOnStreamingStarted', () => {
  test('reconnects when there is no socket at all', () => {
    expect(shouldReconnectActiveChatOnStreamingStarted(undefined)).toBe(true)
  })

  test('leaves a CONNECTING or OPEN socket alone', () => {
    expect(shouldReconnectActiveChatOnStreamingStarted({ readyState: 0 })).toBe(false)
    expect(shouldReconnectActiveChatOnStreamingStarted({ readyState: 1 })).toBe(false)
  })

  test('reconnects a CLOSING or CLOSED socket', () => {
    expect(shouldReconnectActiveChatOnStreamingStarted({ readyState: 2 })).toBe(true)
    expect(shouldReconnectActiveChatOnStreamingStarted({ readyState: 3 })).toBe(true)
  })
})

describe('chatWsReconnectDelayMs', () => {
  test('starts fast and doubles', () => {
    expect(chatWsReconnectDelayMs(1)).toBe(50)
    expect(chatWsReconnectDelayMs(2)).toBe(100)
    expect(chatWsReconnectDelayMs(3)).toBe(200)
  })

  test('caps at two seconds', () => {
    expect(chatWsReconnectDelayMs(20)).toBe(2000)
  })

  test('treats a zero or negative attempt as the first', () => {
    expect(chatWsReconnectDelayMs(0)).toBe(50)
  })
})

describe('isHostConnectionUnavailableMessage', () => {
  test('matches the legacy proxy message regardless of case and padding', () => {
    expect(isHostConnectionUnavailableMessage('  Host WS unreachable: timeout')).toBe(true)
  })

  test('does not match an ordinary error', () => {
    expect(isHostConnectionUnavailableMessage('Error: something else')).toBe(false)
  })
})

describe('setListIndex', () => {
  test('replaces in place', () => {
    const list = ['a', 'b', 'c']
    setListIndex(list, 1, 'B')
    expect(list).toEqual(['a', 'B', 'c'])
  })

  test('accepts a numeric string index', () => {
    const list = ['a', 'b']
    setListIndex(list, '0', 'A')
    expect(list).toEqual(['A', 'b'])
  })

  test('ignores prototype-hazardous keys', () => {
    const list = ['a']
    setListIndex(list, '__proto__', 'x')
    setListIndex(list, 'constructor', 'x')
    setListIndex(list, 'prototype', 'x')
    expect(list).toEqual(['a'])
    expect(Object.prototype).not.toHaveProperty('0', 'x')
  })

  test('ignores out-of-range, fractional and non-numeric indices', () => {
    const list = ['a']
    setListIndex(list, 5, 'x')
    setListIndex(list, -1, 'x')
    setListIndex(list, 0.5, 'x')
    setListIndex(list, 'nope', 'x')
    expect(list).toEqual(['a'])
  })
})
