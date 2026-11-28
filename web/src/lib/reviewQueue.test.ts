// @vitest-environment jsdom

import { describe, expect, it } from 'vitest'
import { keyBelongsElsewhere, queueKey, resolveUnder, sourceKey } from './reviewQueue'

describe('reviewQueue', () => {
  it('maps the queue keys and ignores chords, IME composition and keys already taken', () => {
    const key = (init: KeyboardEventInit & { keyCode?: number }) => {
      const event = new KeyboardEvent('keydown', { cancelable: true, ...init })
      if (init.keyCode) Object.defineProperty(event, 'keyCode', { value: init.keyCode })
      return event
    }
    expect(queueKey(key({ key: 'a' }))).toBe('approve')
    expect(queueKey(key({ key: 'A' }))).toBe('approve')
    expect(queueKey(key({ key: 'd' }))).toBe('dismiss')
    expect(queueKey(key({ key: 's' }))).toBe('skip')
    expect(queueKey(key({ key: 'ArrowDown' }))).toBe('next')
    expect(queueKey(key({ key: 'ArrowUp' }))).toBe('previous')
    expect(queueKey(key({ key: 'x' }))).toBeNull()
    for (const mod of ['metaKey', 'ctrlKey', 'altKey', 'shiftKey'] as const) expect(queueKey(key({ key: 'a', [mod]: true }))).toBeNull()
    expect(queueKey(key({ key: 'a', isComposing: true }))).toBeNull()
    expect(queueKey(key({ key: 'a', keyCode: 229 }))).toBeNull()
    const taken = key({ key: 'a' })
    taken.preventDefault()
    expect(queueKey(taken)).toBeNull()
  })

  it('leaves a key to a text field, a menu or a dialog, but not to the queue\'s own list', () => {
    document.body.innerHTML = '<div class="chat-split-side"><button id="s">s</button></div><div id="q"><ul role="listbox"><li id="o">x</li></ul></div><input id="i"><div role="menu"><button id="m">m</button></div><div contenteditable="true"><span id="c">c</span></div><button id="b">b</button>'
    const queue = document.getElementById('q')
    const at = (id: string) => document.getElementById(id)
    expect(keyBelongsElsewhere(at('i'), queue)).toBe(true)
    expect(keyBelongsElsewhere(at('m'), queue)).toBe(true)
    expect(keyBelongsElsewhere(at('c'), queue)).toBe(true)
    expect(keyBelongsElsewhere(at('o'), queue)).toBe(false)
    // A chat or terminal in the slot beside the page.
    expect(keyBelongsElsewhere(at('s'), queue)).toBe(true)
    expect(keyBelongsElsewhere(at('b'), queue)).toBe(false)
    expect(keyBelongsElsewhere(document.body, queue)).toBe(false)
    document.body.innerHTML = ''
  })

  it('keeps a resolved path inside the vault', () => {
    expect(resolveUnder('v', 'v', 'People/Mo.md')).toBe('v/People/Mo.md')
    expect(resolveUnder('v', 'v/Workspace', '../People/Mo.md')).toBe('v/People/Mo.md')
    expect(resolveUnder('v', 'v', '../../etc/passwd')).toBe('')
    expect(resolveUnder('v', 'v', '/etc/passwd')).toBe('')
    expect(resolveUnder('v', 'v', 'javascript:alert(1)')).toBe('')
  })

  it('names every kind of source by its own identity', () => {
    expect(sourceKey({ kind: 'chat', id: 'chat-0123abcd' })).toBe('chat-0123abcd')
    expect(sourceKey({ kind: 'note', path: 'People/Mo.md' })).toBe('People/Mo.md')
    expect(sourceKey({ kind: 'link', url: 'https://example.com/' })).toBe('https://example.com/')
    expect(sourceKey({ kind: 'text', label: 'care run' })).toBe('care run')
  })
})
