import { describe, expect, it } from 'vitest'
import { discussionChatTitle, discussionState, entryExcerpt } from './proposalDiscussion'

describe('discussionChatTitle', () => {
  it('names one proposal, cut at a word', () => {
    expect(discussionChatTitle('Remember the thing')).toBe('Discuss: Remember the thing')
    const long = discussionChatTitle('Schedule sched-d6630dc1 (I think therefore AI daily check): add to its Tools section')
    expect(long.startsWith('Discuss: Schedule sched-d6630dc1')).toBe(true)
    expect(long.endsWith('…')).toBe(true)
    expect(long.length).toBeLessThanOrEqual('Discuss: '.length + 60)
  })

  it('counts a selection, and never comes back empty', () => {
    expect(discussionChatTitle('', 3)).toBe('Discuss 3 proposals')
    expect(discussionChatTitle('   ')).toBe('Discuss a proposal')
  })
})

describe('entryExcerpt', () => {
  it('reads a bullet as its fact', () => {
    expect(entryExcerpt('- **SDC-1** ([Fix it](https://x.test)) is done. [verified: 2026-10-22]\n  more'))
      .toBe('SDC-1 (Fix it) is done.')
    expect(entryExcerpt('1. __Plain__ item')).toBe('Plain item')
    expect(entryExcerpt('')).toBe('')
  })
})

describe('discussionState', () => {
  it('ranks needs-you over working over a reply', () => {
    expect(discussionState({ working: true, needsYou: true, unread: true })).toBe('needs-you')
    expect(discussionState({ working: true, needsYou: false, unread: true })).toBe('working')
    expect(discussionState({ working: false, needsYou: false, unread: true })).toBe('reply')
    expect(discussionState({ working: false, needsYou: false, unread: false })).toBe('idle')
  })
})
