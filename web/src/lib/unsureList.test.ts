import { describe, expect, it } from 'vitest'
import { appliesUnattended, discussSeed, kindLabel, needsChatReason, type UnsureItem } from './unsureList'

function item(over: Partial<UnsureItem> = {}): UnsureItem {
  return {
    id: 'a'.repeat(16), checked: false, kind: '', qualifier: '', title: 'Mo works at Acme now?',
    text: 'Mo works at Acme now? He said so once.', reason: 'only one chat says it',
    source: 'chat-7', date: '2026-11-20',
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
})
