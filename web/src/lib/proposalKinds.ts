/** What the review queue knows about each kind of proposal, in one place.
 *
 * The panel used to dispatch on `row.kind` in seventeen separate conditionals —
 * the chip label, the destination line, whether accept is offered, whether a
 * refused accept falls back to a chat, which of four near-identical merge
 * functions ran, and how `discuss` named the row. Adding a kind meant finding
 * every one of them, and the UI encoded backend semantics in each.
 *
 * A descriptor answers all of those questions for one kind, so a new kind is one
 * entry here. `descriptorFor` never returns undefined: an unknown kind (a server
 * newer than the client) falls back to `GENERIC`, which shows the region form and
 * offers a plain accept with no chat fallback — the behaviour the old chain of
 * conditionals ended in.
 *
 * Everything here is a pure function of the row, so it is testable without
 * mounting the panel, which is the other half of the point.
 */

import type { ProposalRow } from './types'

/** How a re-home row is presented.
 *
 * Never a pre-filled one-click accept for a destination no tag backs: a single
 * clean signal is a plain accept, multiple candidates are a picker, and no
 * signal is a question.
 */
export function rehomeMode(row: ProposalRow): 'accept' | 'picker' | 'question' {
  const sig = row.rehome
  if (!sig) return 'question'
  if (sig.candidates.length > 1) return 'picker'
  if (sig.justified) return 'accept'
  return 'question'
}

/** What a failed direct accept does instead of surfacing the error. */
export interface ProposalMergeFallback {
  /** Title of the background chat that performs the merge. */
  chatTitle: (row: ProposalRow) => string
  /** Detail line under the "Merging in background" toast. */
  toastDetail: (row: ProposalRow) => string
  /** The prompt that chat is seeded with. */
  prompt: (row: ProposalRow, errorMsg: string) => string
  /**
   * Whether *this* failure is the expected one.
   *
   * Only `people` narrows on the message: its direct accept is create-only, so
   * "already exists" is the merge path and anything else is a real error. The
   * rest fall back on any refusal, because their guards (fold, cap, region) all
   * mean the same thing — a human-shaped merge is needed.
   */
  when: (errorMsg: string) => boolean
}

export interface ProposalKindDescriptor {
  /** Chip text on the row. */
  label: string
  /** The line under the title: where an accept writes. */
  destination: (row: ProposalRow) => string
  /** Whether an accept can do what the button says. */
  canAccept: (row: ProposalRow) => boolean
  /** What a refused accept falls back to; null surfaces the error instead. */
  fallback: ProposalMergeFallback | null
  /**
   * How `discuss` refers to the row when asking for a decision.
   *
   * Row-valued because `GENERIC` has to name a kind it does not know: before
   * the registry the panel interpolated `row.kind` directly, so an unknown kind
   * still reached the agent by name. A constant string here would ask it to
   * place a fact without saying what the server called it.
   */
  discussLabel: (row: ProposalRow) => string
}

// ---- merge prompts ---------------------------------------------------------
//
// Every prompt interpolates `row.text`, which is arbitrary user prose. The
// dismissal at the end of each therefore names `--text-file`, never a quoted
// shell argument: a fact containing `$(...)`, a backtick or a quote would be
// run or mangled on its way to the command. `memory-proposal-add` has kept the
// fact out of argv for that reason all along; the dismissal gained the same
// door in the CLI, and these are its main interactive callers.

function peopleMergePrompt(row: ProposalRow): string {
  const target = row.target || '?'
  const where = `queued in the ${row.workspace} workspace`
  return (
    `A \`[people ${target}]\` proposal is queued (${where}) but \`People/${target}.md\` already exists, so a direct accept correctly refused to overwrite it. Work in this chat only; do not delegate this helper task.\n\n` +
    `Fact to merge: ${row.text}\n\n` +
    `Read \`People/${target}.md\` in the ${row.workspace} vault, merge this fact into it without duplicating anything already there (keep \`tags: [person]\`, add a sentence under the heading or appropriate section), and write it back.\n\n` +
    `After the note is updated, dismiss the queued proposal that contains this exact text by running \`ciao memory-proposal-dismiss --text-file <file> --promoted\` so it disappears from Review (the flag records the promotion; do not delete the bullet from the file directly — that skips the outcome log). ` +
    `If the fact is already present verbatim, just dismiss the proposal. Leave other proposals untouched. Nothing is broken – this is the expected merge path for existing person notes.`
  )
}

function projectMergePrompt(row: ProposalRow, errorMsg: string): string {
  const target = row.target || 'the project doc'
  const where = `queued in the ${row.workspace} workspace`
  return (
    `A \`[project ${target}]\` proposal is queued (${where}) but direct accept refused: ${errorMsg}\n\nWork in this chat only; do not delegate this helper task.\n\n` +
    `Fact to merge: ${row.text}\n\n` +
    `Read \`${target}\` (vault-relative, in the ${row.workspace} workspace), merge this fact into the appropriate section without duplicating existing content, and write it back. Keep frontmatter and structure intact.\n\n` +
    `After the doc is updated, dismiss the queued proposal that contains this exact text by running \`ciao memory-proposal-dismiss --text-file <file> --promoted\`. If the fact is already covered, run the same command without \`--promoted\`. Do not delete the bullet from the file directly — that skips the outcome log. Leave other proposals untouched. Nothing is broken – this is the expected merge path when the fold guards refuse.`
  )
}

function memoryMergePrompt(row: ProposalRow, errorMsg: string): string {
  const region = row.region || row.kind
  const where = `queued in the ${row.workspace} workspace`
  return (
    `A \`[${row.kind}]\` proposal is queued (${where}) for region \`${region}\` but direct accept refused: ${errorMsg}\n\nWork in this chat only; do not delegate this helper task.\n\n` +
    `Fact to merge: ${row.text}\n\n` +
    `Read \`${row.workspace}/CLAUDE.md\` bounded region \`${region}\`, merge this fact there without duplication and within the char limit – curate/consolidate nearby bullets if needed to make room, never exceed the cap.\n\n` +
    `After the region is updated, dismiss the queued proposal that contains this exact text by running \`ciao memory-proposal-dismiss --text-file <file> --promoted\` (do not delete the bullet from the file directly — that skips the outcome log). If the fact is already present verbatim, just dismiss. Leave other proposals untouched. Nothing is broken – this is the expected path when the region is over cap or needs curation.`
  )
}

function learningsMergePrompt(row: ProposalRow, errorMsg: string): string {
  const where = `queued in the ${row.workspace} workspace`
  return (
    `A \`[learnings]\` proposal is queued (${where}) but direct accept refused: ${errorMsg}\n\nWork in this chat only; do not delegate this helper task.\n\n` +
    `Fact to merge: ${row.text}\n\n` +
    `Read \`Workspace/Learnings.md\` in the ${row.workspace} vault, append this fact under \`## Active\` without duplication, and write it back.\n\n` +
    `After the file is updated, dismiss the queued proposal that contains this exact text by running \`ciao memory-proposal-dismiss --text-file <file> --promoted\` (do not delete the bullet from the file directly — that skips the outcome log). If the fact is already present, just dismiss. Leave other proposals untouched.`
  )
}

// ---- destinations ----------------------------------------------------------

/** The generic form: the ciao region an accept would write into. */
function regionDestination(row: ProposalRow): string {
  return `ciao:${row.region ?? row.kind}`
}

function rehomeDestination(row: ProposalRow): string {
  const sig = row.rehome
  const from = row.workspace
  if (sig?.candidates?.length && sig.candidates.length > 1) {
    return `${from} → ${sig.candidates.join(' or ')} · tags name more than one`
  }
  if (sig?.destination) {
    const to = sig.destination.split('/')[0]
    return sig.justified
      ? `${from} → ${to} · tags back this`
      : `${from} → ${to} · no tag backs it`
  }
  // A stale row is not asking anything: its cause is gone. Saying "needs a
  // decision" made litter look identical to a real question.
  if (sig?.stale) return `${from} · no longer applies · safe to dismiss`
  return `${from} · no destination, needs a decision`
}

// ---- the registry ----------------------------------------------------------

const ALWAYS = () => true

/** The three kinds that write a bounded region in the workspace guide. */
const REGION_FALLBACK: ProposalMergeFallback = {
  chatTitle: () => 'Merge memory fact',
  toastDetail: () => 'Memory fact — click to open the chat',
  prompt: memoryMergePrompt,
  when: ALWAYS,
}

function regionKind(label: string): ProposalKindDescriptor {
  return {
    label,
    destination: regionDestination,
    canAccept: ALWAYS,
    fallback: REGION_FALLBACK,
    discussLabel: () => `a \`${label}\` proposal`,
  }
}

/** Used for a kind this client does not know about. */
export const GENERIC: ProposalKindDescriptor = {
  label: '',
  destination: regionDestination,
  canAccept: ALWAYS,
  fallback: null,
  discussLabel: (row) => `a \`${row.kind}\` proposal`,
}

export const PROPOSAL_KINDS: Record<string, ProposalKindDescriptor> = {
  memory: regionKind('memory'),
  // `user` is the server's older spelling of the same region; both show as
  // "profile" so the queue does not appear to hold two different things.
  profile: regionKind('profile'),
  user: { ...regionKind('profile'), discussLabel: () => 'a `user` proposal' },

  people: {
    label: 'people',
    destination: (row) => `People/${row.target || '?'}.md`,
    canAccept: ALWAYS,
    fallback: {
      chatTitle: (row) => `Merge ${row.target || 'person'} fact`,
      toastDetail: (row) => `${row.target || 'Person'} fact — click to open the chat`,
      prompt: peopleMergePrompt,
      when: (msg) => /already exists/i.test(msg),
    },
    discussLabel: () => 'a `people` proposal',
  },

  project: {
    label: 'project',
    destination: (row) => row.target || 'no project doc named',
    canAccept: ALWAYS,
    fallback: {
      chatTitle: () => 'Merge project fact',
      toastDetail: () => 'Project fact — click to open the chat',
      prompt: projectMergePrompt,
      when: ALWAYS,
    },
    discussLabel: () => 'a `project` proposal',
  },

  learnings: {
    label: 'learnings',
    destination: () => 'Workspace/Learnings.md',
    canAccept: ALWAYS,
    fallback: {
      chatTitle: () => 'Merge learning',
      toastDetail: () => 'Learning — click to open the chat',
      prompt: learningsMergePrompt,
      when: ALWAYS,
    },
    discussLabel: () => 'a `learnings` proposal',
  },

  // A review row has no known destination at all, so accepting one would be a
  // guess wearing a button.
  review: {
    label: 'review',
    destination: () => 'no destination yet — decide what it is',
    canAccept: () => false,
    fallback: null,
    discussLabel: () => 'a fact with no decided destination',
  },

  // A re-home row with no backed destination has nowhere to go.
  rehome: {
    label: 're-home',
    destination: rehomeDestination,
    canAccept: (row) => rehomeMode(row) === 'accept',
    fallback: null,
    discussLabel: () => 'a re-home proposal',
  },

  // A skill row is a file, not a bullet, and `accept_for('skill')` raises on the
  // server — accepting a proposed skill means implementing it, which is a chat.
  // The path, not the words "a skill proposal file": the row's whole content is
  // in that file, and naming it is what makes "view" obviously the first thing
  // to press.
  skill: {
    label: 'skill',
    destination: (row) => row.path || 'a skill proposal file',
    canAccept: () => false,
    fallback: null,
    discussLabel: () => 'a `skill` proposal',
  },
}

export function descriptorFor(row: ProposalRow): ProposalKindDescriptor {
  return PROPOSAL_KINDS[row.kind] ?? GENERIC
}

/** Chip text for a kind, falling back to the raw kind for an unknown one. */
export function kindLabel(kind: string): string {
  return PROPOSAL_KINDS[kind]?.label || kind
}
