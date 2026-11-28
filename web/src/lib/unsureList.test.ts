// @vitest-environment jsdom

import { describe, expect, it } from 'vitest'
import {
  appliesUnattended, approveEffect, discussSeed, foundDate, keyBelongsElsewhere, kindLabel, needsChatReason, queueKey,
  reasonParts, ruleChange, unsureChatCount, unsureMore, unsureSources, vaultFile, type UnsureItem,
} from './unsureList'

function item(over: Partial<UnsureItem> = {}): UnsureItem {
  return {
    id: 'a'.repeat(16), checked: false, kind: '', qualifier: '', title: 'Mo works at Acme now?',
    text: 'Mo works at Acme now? He said so once.', reason: 'only one chat says it',
    source: 'chat-7', date: '2026-11-20', destination: '', destination_ok: false, section: '', proposal: '',
    raw: '- [ ] Mo works at Acme now? He said so once. — unsure because only one chat says it; source: chat-7; 2026-11-20',
    malformed: false, ...over,
  }
}

describe('unsureList', () => {
  it('names every kind with a short plain tag', () => {
    expect(kindLabel('')).toBe('fact')
    expect(kindLabel('observation')).toBe('pattern')
    expect(kindLabel('drop')).toBe('trim')
    expect(kindLabel('restore')).toBe('restore')
    expect(kindLabel('repo')).toBe('repo')
    expect(kindLabel('rule')).toBe('rule')
    expect(kindLabel('category')).toBe('category')
  })

  it('leaves categories, repository facts, rule flips and malformed lines to a chat', () => {
    for (const kind of ['', 'observation', 'drop', 'restore'] as const) {
      expect(appliesUnattended({ kind, malformed: false })).toBe(true)
      expect(needsChatReason({ kind, malformed: false })).toBe('')
    }
    for (const kind of ['category', 'repo', 'rule'] as const) {
      expect(appliesUnattended({ kind, malformed: false })).toBe(false)
      expect(needsChatReason({ kind, malformed: false })).not.toBe('')
    }
    expect(appliesUnattended({ kind: '', malformed: true })).toBe(false)
  })

  it('seeds a chat with the full line, without its tick box, and its source', () => {
    const seed = discussSeed(item(), 'memory-vault/work/Workspace/Unsure.md')
    expect(seed).toContain('`memory-vault/work/Workspace/Unsure.md`')
    expect(seed).toContain('> Mo works at Acme now? He said so once. — unsure because only one chat says it; source: chat-7; 2026-11-20')
    expect(seed).not.toContain('[ ]')
    expect(seed).toContain('It came from chat-7.')
  })

  it('reads where an item came from: chat ids, vault notes and plain words, and an observation\'s chats', () => {
    expect(unsureSources(item({ source: 'chat-30605c3a and chat-7e11530b' }))).toEqual([
      { kind: 'chat', id: 'chat-30605c3a' }, { kind: 'chat', id: 'chat-7e11530b' },
    ])
    expect(unsureSources(item({ source: 'People/Sam Rivera.md, care run' }))).toEqual([
      { kind: 'note', path: 'People/Sam Rivera.md' }, { kind: 'text', label: 'care run' },
    ])
    // A path that climbs out or is absolute is only words.
    expect(unsureSources(item({ source: '../secret.md' }))).toEqual([{ kind: 'text', label: '../secret.md' }])
    const seen = item({
      kind: 'observation', source: 'chat-5e6f7a8b',
      text: 'You prefer short updates. — seen in: chat-1a2b3c4d, chat-5e6f7a8b',
    })
    expect(unsureSources(seen).map(s => (s as { id: string }).id)).toEqual(['chat-5e6f7a8b', 'chat-1a2b3c4d'])
    expect(unsureChatCount(seen)).toBe(2)
  })

  it('keeps the words after the first sentence without what the pane shows elsewhere', () => {
    expect(unsureMore(item())).toBe('He said so once.')
    expect(unsureMore(item({ text: 'Mo works at Acme now? He said. "Mo works at Acme."', proposal: 'Mo works at Acme.' }))).toBe('He said.')
    expect(unsureMore(item({ kind: 'observation', title: 'You prefer short updates.', text: 'You prefer short updates. — seen in: chat-1a2b3c4d' }))).toBe('')
    const rule = item({ kind: 'rule', title: 'Re-check every 45 days?', text: 'Re-check every 45 days? 30 -> 45' })
    expect(unsureMore(rule)).toBe('')
    expect(ruleChange(rule)).toEqual({ from: '30', to: '45' })
    expect(ruleChange(item())).toBeNull()
  })

  it('turns a Markdown link in the reason into its text and the vault note it points at', () => {
    const list = 'memory-vault/work/Workspace/Unsure.md'
    expect(reasonParts('the [Northwind note](../projects/Northwind.md#audio) disagrees', list)).toEqual([
      { text: 'the ' }, { text: 'Northwind note', path: 'memory-vault/work/projects/Northwind.md' }, { text: ' disagrees' },
    ])
    // A link out of the vault, to the web or to something not Markdown keeps only its words.
    expect(reasonParts('[x](../../../etc/passwd.md)', list)).toEqual([{ text: 'x' }])
    expect(reasonParts('[x](https://example.com/a.md)', list)).toEqual([{ text: 'x' }])
    expect(reasonParts('plain', list)).toEqual([{ text: 'plain' }])
    expect(vaultFile(list, 'People/Mo.md')).toBe('memory-vault/work/People/Mo.md')
    expect(vaultFile(list, '../Mo.md')).toBe('')
    expect(vaultFile(list, '/etc/Mo.md')).toBe('')
  })

  it('writes the found date short, with the year only when it is not this one', () => {
    const now = new Date(2026, 10, 28)
    expect(foundDate('2026-11-27', now)).toBe('Nov 27')
    expect(foundDate('2025-01-03', now)).toBe('Jan 3, 2025')
    expect(foundDate('soon', now)).toBe('')
  })

  it('says what approving does from the line itself, and never invents a line or a destination', () => {
    const plain = approveEffect(item())
    expect(plain).toMatchObject({ chat: false, line: '', where: '' })
    expect(plain.how).toContain('This line does not say where.')
    const full = approveEffect(item({ proposal: 'Mo works at Acme.', destination: 'People/Mo.md', destination_ok: true }))
    expect(full).toMatchObject({ chat: false, line: 'Mo works at Acme.', lineRole: 'write', where: 'People/Mo.md', whereIsNote: true })
    expect(full.how).toBe('The next Workspace care run writes it there and takes the line off this list.')
    expect(approveEffect(item({ destination: 'profile', destination_ok: true }))).toMatchObject({ where: 'profile', whereIsNote: false, blocked: false })
    // A destination the engine refused is shown as blocked and never as a note to open.
    for (const destination of ['../../.ssh/authorized_keys', 'Workspace/Care-Report.md', 'automations/x/PROMPT.md']) {
      const refused = approveEffect(item({ destination, destination_ok: false, proposal: 'x' }))
      expect(refused).toMatchObject({ blocked: true, whereIsNote: false, where: destination })
      expect(approveEffect(item({ kind: 'observation', destination, destination_ok: false })).blocked).toBe(true)
    }
    const drop = approveEffect(item({ kind: 'drop', qualifier: 'memory', proposal: 'Rotate on Fridays' }))
    expect(drop).toMatchObject({ lineRole: 'remove', where: 'memory', line: 'Rotate on Fridays' })
    expect(drop.how).toContain('Workspace/Memory-Consolidations.md')
    expect(approveEffect(item({ kind: 'restore', qualifier: 'profile' })).lineRole).toBe('restore')
    expect(approveEffect(item({ kind: 'observation' })).how).toContain('your profile or the person\'s note')
    for (const kind of ['category', 'repo', 'rule'] as const) expect(approveEffect(item({ kind })).chat).toBe(true)
    expect(approveEffect(item({ kind: 'repo', qualifier: '~/repos/app/' })).where).toBe('~/repos/app/AGENTS.md')
    expect(approveEffect(item({ malformed: true })).chat).toBe(true)
  })

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
})
