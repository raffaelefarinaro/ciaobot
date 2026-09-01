import { describe, expect, it } from 'vitest'
import {
  GENERIC,
  PROPOSAL_KINDS,
  descriptorFor,
  kindLabel,
  rehomeMode,
} from './proposalKinds'
import type { ProposalRow, RehomeSignal } from './types'

function row(over: Partial<ProposalRow> = {}): ProposalRow {
  return {
    id: 'p1',
    kind: 'memory',
    text: 'a durable fact',
    source: 'chat',
    workspace: 'work',
    path: '',
    line: 1,
    ...over,
  }
}

function signal(over: Partial<RehomeSignal> = {}): RehomeSignal {
  return {
    note: 'People/Mo.md',
    destination: '',
    candidates: [],
    justified: false,
    reason: 'tagged',
    ...over,
  }
}

describe('descriptorFor', () => {
  it('falls back to GENERIC for a kind this client does not know', () => {
    // A server newer than the client must not render a blank row or crash.
    expect(descriptorFor(row({ kind: 'something-new' }))).toBe(GENERIC)
    expect(descriptorFor(row({ kind: 'something-new', region: 'r' })).destination(
      row({ kind: 'something-new', region: 'r' }),
    )).toBe('ciao:r')
  })

  it('resolves every kind the server can send', () => {
    for (const kind of ['memory', 'profile', 'user', 'people', 'project', 'learnings', 'review', 'rehome', 'skill']) {
      expect(descriptorFor(row({ kind }))).not.toBe(GENERIC)
    }
  })
})

describe('kindLabel', () => {
  it('shows both spellings of the profile region as one thing', () => {
    // `user` is the server's older name for the same region.
    expect(kindLabel('profile')).toBe('profile')
    expect(kindLabel('user')).toBe('profile')
  })

  it('hyphenates re-home and passes an unknown kind through raw', () => {
    expect(kindLabel('rehome')).toBe('re-home')
    expect(kindLabel('something-new')).toBe('something-new')
  })
})

describe('destination', () => {
  it('names where an accept writes, per kind', () => {
    const cases: Array<[Partial<ProposalRow>, string]> = [
      [{ kind: 'memory', region: 'memory' }, 'ciao:memory'],
      [{ kind: 'people', target: 'Mo' }, 'People/Mo.md'],
      [{ kind: 'people' }, 'People/?.md'],
      [{ kind: 'project', target: 'Projects/Thing.md' }, 'Projects/Thing.md'],
      [{ kind: 'project' }, 'no project doc named'],
      [{ kind: 'learnings' }, 'Workspace/Learnings.md'],
      [{ kind: 'review' }, 'no destination yet — decide what it is'],
      [{ kind: 'skill', path: 'skills/thing/SKILL.md' }, 'skills/thing/SKILL.md'],
      [{ kind: 'skill' }, 'a skill proposal file'],
    ]
    for (const [over, expected] of cases) {
      const r = row(over)
      expect(descriptorFor(r).destination(r)).toBe(expected)
    }
  })

  it('reads a re-home row destination off its signal', () => {
    const justified = row({
      kind: 'rehome',
      rehome: signal({ destination: 'personal/People/Mo.md', justified: true, candidates: ['personal'] }),
    })
    expect(descriptorFor(justified).destination(justified)).toBe('work → personal · tags back this')

    const unbacked = row({
      kind: 'rehome',
      rehome: signal({ destination: 'personal/People/Mo.md', justified: false, candidates: [] }),
    })
    expect(descriptorFor(unbacked).destination(unbacked)).toBe('work → personal · no tag backs it')

    const ambiguous = row({
      kind: 'rehome',
      rehome: signal({ destination: '', justified: false, candidates: ['personal', 'side'] }),
    })
    expect(descriptorFor(ambiguous).destination(ambiguous)).toBe(
      'work → personal or side · tags name more than one',
    )

    // A stale row is litter, not a question — it must not read as one.
    const stale = row({
      kind: 'rehome',
      rehome: signal({ destination: '', justified: false, candidates: [], stale: true }),
    })
    expect(descriptorFor(stale).destination(stale)).toBe('work · no longer applies · safe to dismiss')
  })
})

describe('canAccept', () => {
  it('refuses the kinds whose accept would be a guess', () => {
    // A skill is a file (`accept_for('skill')` raises server-side) and a review
    // row has no decided destination at all.
    for (const kind of ['skill', 'review']) {
      const r = row({ kind })
      expect(descriptorFor(r).canAccept(r)).toBe(false)
    }
  })

  it('allows the destination and region kinds', () => {
    for (const kind of ['memory', 'profile', 'user', 'people', 'project', 'learnings']) {
      const r = row({ kind })
      expect(descriptorFor(r).canAccept(r)).toBe(true)
    }
  })

  it('allows a re-home accept only when the tags back the destination', () => {
    const backed = row({
      kind: 'rehome',
      rehome: signal({ destination: 'personal/n', justified: true, candidates: ['personal'] }),
    })
    expect(descriptorFor(backed).canAccept(backed)).toBe(true)

    for (const rehome of [
      signal({ destination: 'personal/n', justified: false, candidates: ['personal'] }),
      signal({ destination: '', justified: true, candidates: ['a', 'b'] }),
      undefined,
    ]) {
      const r = row({ kind: 'rehome', rehome })
      expect(descriptorFor(r).canAccept(r)).toBe(false)
    }
  })
})

describe('rehomeMode', () => {
  it('is a picker for several candidates, an accept only when justified', () => {
    expect(rehomeMode(row({ kind: 'rehome' }))).toBe('question')
    expect(rehomeMode(row({
      kind: 'rehome',
      rehome: signal({ destination: '', justified: true, candidates: ['a', 'b'] }),
    }))).toBe('picker')
    expect(rehomeMode(row({
      kind: 'rehome',
      rehome: signal({ destination: 'a/n', justified: true, candidates: ['a'] }),
    }))).toBe('accept')
    expect(rehomeMode(row({
      kind: 'rehome',
      rehome: signal({ destination: 'a/n', justified: false, candidates: ['a'] }),
    }))).toBe('question')
  })
})

describe('accept fallback', () => {
  it('only people narrows on the error message', () => {
    // A people accept is create-only, so "already exists" is the merge path and
    // anything else is a real error worth surfacing.
    const people = PROPOSAL_KINDS.people.fallback
    expect(people).not.toBeNull()
    expect(people!.when('People/Mo.md already exists')).toBe(true)
    expect(people!.when('permission denied')).toBe(false)
  })

  it('the fold/cap kinds fall back on any refusal', () => {
    for (const kind of ['project', 'memory', 'profile', 'user', 'learnings']) {
      const fallback = PROPOSAL_KINDS[kind].fallback
      expect(fallback, kind).not.toBeNull()
      expect(fallback!.when('anything at all')).toBe(true)
    }
  })

  it('has no fallback for the kinds that never offered an accept', () => {
    for (const kind of ['skill', 'review', 'rehome']) {
      expect(PROPOSAL_KINDS[kind].fallback, kind).toBeNull()
    }
    expect(GENERIC.fallback).toBeNull()
  })

  it('titles the merge chat and toast per kind', () => {
    const people = row({ kind: 'people', target: 'Mo' })
    expect(PROPOSAL_KINDS.people.fallback!.chatTitle(people)).toBe('Merge Mo fact')
    expect(PROPOSAL_KINDS.people.fallback!.toastDetail(people)).toBe('Mo fact — click to open the chat')

    const anon = row({ kind: 'people' })
    expect(PROPOSAL_KINDS.people.fallback!.chatTitle(anon)).toBe('Merge person fact')

    expect(PROPOSAL_KINDS.project.fallback!.chatTitle(row({ kind: 'project' }))).toBe('Merge project fact')
    expect(PROPOSAL_KINDS.learnings.fallback!.chatTitle(row({ kind: 'learnings' }))).toBe('Merge learning')
    expect(PROPOSAL_KINDS.memory.fallback!.chatTitle(row())).toBe('Merge memory fact')
  })

  it('seeds the merge prompt with the fact, the workspace and the refusal', () => {
    const r = row({ kind: 'project', target: 'Projects/Thing.md', text: 'the fact' })
    const prompt = PROPOSAL_KINDS.project.fallback!.prompt(r, 'fold guard refused')
    expect(prompt).toContain('the fact')
    expect(prompt).toContain('Projects/Thing.md')
    expect(prompt).toContain('fold guard refused')
    expect(prompt).toContain('work')
    // Every merge prompt must tell the agent to dismiss with --promoted rather
    // than editing the proposal file, which would skip the outcome log.
    expect(prompt).toContain('memory-proposal-dismiss')
    expect(prompt).toContain('--promoted')
  })

  it('every fallback prompt routes the dismissal through the outcome log', () => {
    for (const [kind, descriptor] of Object.entries(PROPOSAL_KINDS)) {
      if (!descriptor.fallback) continue
      const prompt = descriptor.fallback.prompt(row({ kind }), 'refused')
      expect(prompt, kind).toContain('memory-proposal-dismiss')
      // Capitalisation differs between the prompts; the instruction is what matters.
      expect(prompt.toLowerCase(), kind).toContain('do not delete the bullet from the file directly')
      // Never a quoted shell argument: `row.text` is arbitrary user prose, and
      // `$(...)`, a backtick or a quote in it would be run or mangled.
      expect(prompt, kind).toContain('--text-file')
      expect(prompt, kind).not.toContain('memory-proposal-dismiss "')
    }
  })
})

describe('discussLabel', () => {
  function labelFor(kind: string): string {
    const r = row({ kind })
    return descriptorFor(r).discussLabel(r)
  }

  it('does not call a review row a kind of proposal', () => {
    // It is a fact with nowhere decided to go; naming it "a `review` proposal"
    // asks the agent the wrong question.
    expect(labelFor('review')).toBe('a fact with no decided destination')
  })

  it('names the other kinds', () => {
    expect(labelFor('memory')).toBe('a `memory` proposal')
    expect(labelFor('rehome')).toBe('a re-home proposal')
  })

  it('still names an unknown kind, which is why the label is row-valued', () => {
    // Before the registry the panel interpolated `row.kind` directly, so a
    // server newer than the client still told the agent what it had sent. A
    // constant on GENERIC would have dropped that.
    expect(labelFor('something-new')).toBe('a `something-new` proposal')
  })
})
