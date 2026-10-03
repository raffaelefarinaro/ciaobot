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
    for (const kind of ['memory', 'profile', 'user', 'people', 'project', 'learnings', 'review', 'rehome', 'skill', 'category', 'note_edit']) {
      expect(descriptorFor(row({ kind }))).not.toBe(GENERIC)
    }
  })

  it('names a category row by the id its accept would add', () => {
    // The id is the bullet's payload, and it is also the registry id the accept
    // appends — so the destination is the category itself, not a path.
    const r = row({ kind: 'category', target: 'recipe-book' })
    expect(PROPOSAL_KINDS.category.label).toBe('category')
    expect(descriptorFor(r).destination(r)).toBe('recipe-book')
    expect(descriptorFor(row({ kind: 'category' })).destination(row({ kind: 'category' })))
      .toBe('no category named')
    expect(PROPOSAL_KINDS.category.canAccept(r)).toBe(true)
    // There is no prose to reconcile: the decision is which notes get retyped,
    // so there is no merge chat to fall back to.
    expect(PROPOSAL_KINDS.category.fallback).toBeNull()
  })

  it('names a note_edit row by the note it would rewrite', () => {
    // The queue bullet's payload is a sidecar id — a digest says nothing to a
    // reviewer — so the server resolves it and the row's `target` is the note.
    const r = row({ kind: 'note_edit', target: 'notes/office.md' })
    expect(PROPOSAL_KINDS.note_edit.label).toBe('note edit')
    expect(descriptorFor(r).destination(r)).toBe('notes/office.md')
    const unnamed = row({ kind: 'note_edit' })
    expect(descriptorFor(unnamed).destination(unnamed)).toBe('no note named')
  })
})

describe('note_edit', () => {
  function editRow(over: Partial<ProposalRow> = {}): ProposalRow {
    return row({
      kind: 'note_edit',
      target: 'notes/office.md',
      text: 'notes/office.md — replace: the third floor no longer exists',
      note_edit: {
        id: '4f2a91c0b7d3e6a5',
        operation: 'replace',
        outcome: 'update',
        settled: '',
        receipt_id: '',
        can_accept: true,
        reason: 'the third floor no longer exists',
      },
      ...over,
    })
  }

  it('offers the accept only when the server says the operation can be applied', () => {
    // The server asks the same question the accept does: a note that moved
    // since the proposal was filed, or a re-stamp with no frontmatter to stamp,
    // has a button that could only ever refuse. A row with no `note_edit` block
    // at all predates the field and stays acceptable.
    expect(descriptorFor(editRow()).canAccept(editRow())).toBe(true)
    const blocked = editRow({
      note_edit: { ...editRow().note_edit!, can_accept: false, reason: 'the note changed' },
    })
    expect(descriptorFor(blocked).canAccept(blocked)).toBe(false)
    const bare = row({ kind: 'note_edit' })
    expect(descriptorFor(bare).canAccept(bare)).toBe(true)
  })

  it('separates the three operations in the consequence', () => {
    // A retirement is the one operation that removes something, so its copy has
    // to say what happened to the note rather than describing a rewrite.
    for (const [operation, expected] of [
      ['replace', 'Rewrites this whole note with the verified text'],
      ['restamp', 'Marks this note as checked again today'],
      ['retire', 'Moves this note to the review trash, where it can be restored'],
      // The entry operations change ONE list item and leave the rest of the file
      // alone. Describing them as rewriting the whole note is the one thing a
      // reviewer must not be told about a write they are about to click.
      ['replace_entry', 'Rewrites this one entry with the verified text'],
      ['restamp_entry', 'Marks this one entry as checked again today'],
      ['retire_entry', 'Removes this one entry from the note; the note itself is kept'],
    ] as const) {
      const r = editRow({ note_edit: { ...editRow().note_edit!, operation } })
      expect(descriptorFor(r).consequence(r), operation).toBe(expected)
    }
  })

  it('never describes an entry operation as a whole-note write', () => {
    // The default branch is the fallback for a kind this version does not know,
    // so it names the note. An entry operation must never fall through to it.
    for (const operation of ['replace_entry', 'restamp_entry', 'retire_entry'] as const) {
      const r = editRow({ note_edit: { ...editRow().note_edit!, operation } })
      expect(descriptorFor(r).consequence(r), operation).not.toContain('whole note')
      expect(descriptorFor(r).consequence(r), operation).not.toContain('trash')
    }
  })

  it('asks the agent about the decision, not about a region entry', () => {
    const r = editRow()
    expect(descriptorFor(r).discussLabel(r))
      .toBe('a note edit a verification could not settle')
  })

  it('is the one kind whose wording cannot be edited', () => {
    // The accept reads the replacement from the sidecar and the preview ignores
    // the wording, so an editor would re-preview text the write never sees.
    // Every other kind leaves the flag unset, which means editable.
    expect(PROPOSAL_KINDS.note_edit.editable).toBe(false)
    for (const kind of Object.keys(PROPOSAL_KINDS)) {
      if (kind === 'note_edit') continue
      expect(PROPOSAL_KINDS[kind as keyof typeof PROPOSAL_KINDS].editable, kind)
        .not.toBe(false)
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
      [{ kind: 'note_edit', target: 'notes/office.md' }, 'notes/office.md'],
      [{ kind: 'note_edit' }, 'no note named'],
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
    for (const kind of ['memory', 'profile', 'user', 'people', 'project', 'learnings', 'category', 'note_edit']) {
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
    // An existing note is folded at accept time, so a refused fold is the
    // merge path and anything else is a real error worth surfacing.
    const people = PROPOSAL_KINDS.people.fallback
    expect(people).not.toBeNull()
    expect(people!.when('the fold reported no changes to People/Mo.md')).toBe(true)
    expect(people!.when('fold failed: TimeoutError')).toBe(true)
    expect(people!.when('permission denied')).toBe(false)
  })

  it('the fold/cap kinds fall back on any refusal', () => {
    for (const kind of ['project', 'memory', 'profile', 'user', 'learnings']) {
      const fallback = PROPOSAL_KINDS[kind].fallback
      expect(fallback, kind).not.toBeNull()
      expect(fallback!.when('anything at all')).toBe(true)
    }
  })

  it('has no fallback for the kinds with nothing a merge chat could resolve', () => {
    // `note_edit` DOES offer an accept — but the replacement text is the
    // verification's exact verdict, so a chat asked to merge one would have to
    // invent it, and its two real refusals (a note that moved, a record that
    // cannot be read) are not merge problems.
    for (const kind of ['skill', 'review', 'rehome', 'note_edit']) {
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

/** What accepting a row does, said without a path.
 *
 * The row used to show `destination` — `ciao:memory`, `Workspace/Learnings.md`
 * — which is the same shape of string for every kind and says nothing about
 * the difference between them. The path is still the tooltip and the details
 * line; this is what the row itself reads.
 */
describe('consequence', () => {
  function consequenceFor(over: Partial<ProposalRow> = {}): string {
    const r = row(over)
    return descriptorFor(r).consequence(r)
  }

  it('answers for every kind the server can send, naming no file', () => {
    for (const kind of ['memory', 'profile', 'user', 'people', 'project', 'learnings', 'review', 'rehome', 'skill', 'category', 'note_edit']) {
      const text = consequenceFor({ kind, target: 'Mo', path: 'skills/x.md' })
      expect(text, kind).toBeTruthy()
      expect(text, kind).not.toMatch(/\.md\b/)
      expect(text, kind).not.toContain('ciao:')
      expect(text, kind).not.toContain('/')
    }
  })

  it('names the person a people row would be filed under', () => {
    expect(consequenceFor({ kind: 'people', target: 'Mo' }))
      .toBe('Merged into the note about Mo, creating it if there is none')
  })

  it('separates the two region kinds, which share one destination form', () => {
    expect(consequenceFor({ kind: 'memory' })).toBe('Kept as a standing fact for this workspace')
    expect(consequenceFor({ kind: 'profile' })).toBe('Kept as a standing fact about you')
    expect(consequenceFor({ kind: 'user' })).toBe('Kept as a standing fact about you')
  })

  it('says a review row has nowhere to go rather than offering one', () => {
    expect(consequenceFor({ kind: 'review' })).toContain('needs your decision')
  })

  it('distinguishes the three re-home cases without an arrow', () => {
    const backed = consequenceFor({
      kind: 'rehome',
      rehome: signal({ destination: 'work/People/Mo.md', justified: true }),
    })
    expect(backed).toBe('Moves this note to the work workspace')

    const unbacked = consequenceFor({
      kind: 'rehome',
      rehome: signal({ destination: 'work/People/Mo.md', justified: false }),
    })
    expect(unbacked).toContain('no tag backs that')

    const stale = consequenceFor({ kind: 'rehome', rehome: signal({ stale: true }) })
    expect(stale).toBe('No longer applies — safe to dismiss')
  })

  it('answers for a kind this client does not know', () => {
    expect(consequenceFor({ kind: 'something-new' })).toBe('Kept as a standing note for this workspace')
  })
})
