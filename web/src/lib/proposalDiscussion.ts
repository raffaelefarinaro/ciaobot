// Naming and status for the "Discuss" chats a proposal row opens, and the
// short label an entry-level note edit is known by. Pure: no Vue, no store.

/** Longest proposal label a chat title carries before it is cut. */
const TITLE_LABEL_MAX = 60

/** Longest entry excerpt a row title shows before it is cut. */
const EXCERPT_MAX = 180

function clip(text: string, max: number): string {
  const flat = text.replace(/\s+/g, ' ').trim()
  if (flat.length <= max) return flat
  const cut = flat.slice(0, max - 1)
  const space = cut.lastIndexOf(' ')
  return `${(space > max * 0.6 ? cut.slice(0, space) : cut).replace(/[\s,;:.—-]+$/, '')}…`
}

/** The title of a chat opened to discuss one proposal, or a selection of them.
 *
 * Every discussion used to be called "Proposal review", so two of them in the
 * sidebar or on Home could not be told apart. */
export function discussionChatTitle(label: string, count = 1): string {
  if (count > 1) return `Discuss ${count} proposals`
  const name = clip(label, TITLE_LABEL_MAX)
  return name ? `Discuss: ${name}` : 'Discuss a proposal'
}

/** One vault bullet as a single readable line: the list marker, emphasis,
 * link targets and the trailing `[verified: …]` stamp are dropped, so what is
 * left is the fact a person recognises. */
export function entryExcerpt(entry: string): string {
  const first = entry.split('\n').find(line => line.trim()) ?? ''
  const text = first
    .replace(/^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?/, '')
    .replace(/\s*\[verified:[^\]]*\]\s*$/i, '')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/__([^_]+)__/g, '$1')
  return clip(text, EXCERPT_MAX)
}

export type DiscussionState = 'working' | 'needs-you' | 'reply' | 'idle'

/** Where a linked discussion chat stands, most urgent first. */
export function discussionState(signals: { working: boolean; needsYou: boolean; unread: boolean }): DiscussionState {
  if (signals.needsYou) return 'needs-you'
  if (signals.working) return 'working'
  if (signals.unread) return 'reply'
  return 'idle'
}

export const DISCUSSION_WORDS: Record<DiscussionState, string> = {
  working: 'Discussing now',
  'needs-you': 'Discussion waiting for you',
  reply: 'New reply in the discussion',
  idle: 'Discussed in a chat',
}
