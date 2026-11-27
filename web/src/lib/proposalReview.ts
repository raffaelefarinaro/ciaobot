/**
 * A proposal file (`<vault>/Workspace/Proposals/<slug>.md`) as the Workspace care
 * page reviews it. The care run writes these and never acts on them; a person
 * approves one (in an attended chat, which makes the change) or rejects it
 * (which only sets `status: rejected` in the file). Pure helpers: no Vue, no I/O.
 *
 * The fields are the ones `ciao proposal-check` requires (ciao/proposal_files.py).
 */
import { parseFrontmatter } from './markdownFrontmatter'

export type ProposalKind = 'skill-edit' | 'upstream-issue' | 'guide-edit' | 'note-retire'

export interface Proposal {
  kind: ProposalKind | ''
  target: string
  title: string
  summary: string
  rationale: string
  proposedChange: string
  evidence: string[]
  status: string
  created: string
  body: string
}

const KINDS: readonly string[] = ['skill-edit', 'upstream-issue', 'guide-edit', 'note-retire']

const KIND_WORDS: Record<ProposalKind, string> = {
  'skill-edit': 'Skill change',
  'guide-edit': 'Guide change',
  'upstream-issue': 'Issue to report upstream',
  'note-retire': 'Retire a note',
}

function text(value: unknown): string {
  if (Array.isArray(value)) return value.join('\n')
  return typeof value === 'string' ? value : ''
}

/** Read a proposal file's text; null when it has no front matter to read. */
export function parseProposal(source: string): Proposal | null {
  const { frontmatter, body } = parseFrontmatter(source)
  if (!frontmatter) return null
  const kind = text(frontmatter.type)
  const evidence = frontmatter.evidence
  return {
    kind: KINDS.includes(kind) ? (kind as ProposalKind) : '',
    target: text(frontmatter.target),
    title: text(frontmatter.title),
    summary: text(frontmatter.summary),
    rationale: text(frontmatter.rationale),
    proposedChange: text(frontmatter.proposed_change),
    evidence: Array.isArray(evidence) ? evidence.filter(Boolean) : evidence ? [evidence] : [],
    status: text(frontmatter.status),
    created: text(frontmatter.created),
    body: body.trim(),
  }
}

/** "Guide change", "Retire a note"…; '' for a kind the checker would refuse. */
export function proposalKindWords(kind: Proposal['kind']): string {
  return kind ? KIND_WORDS[kind] : ''
}

/** Whether the person can still approve or reject it. */
export function proposalIsOpen(status: string): boolean {
  return status !== 'applied' && status !== 'rejected'
}

/**
 * The file's text with its front matter `status:` set to `status`, everything
 * else byte for byte; null when the file has no front matter block to edit.
 */
export function withProposalStatus(source: string, status: 'rejected'): string | null {
  const open = /^\uFEFF?(?:[^\S\n]*\n)*---\r?\n/.exec(source)
  if (!open) return null
  const rest = source.slice(open[0].length)
  const close = /(?:^|\n)---[ \t]*(?:\r?\n|$)/.exec(rest)
  if (!close) return null
  const front = rest.slice(0, close.index)
  const after = rest.slice(close.index)
  const line = /^status:[^\r\n]*/m
  const next = line.test(front)
    ? front.replace(line, `status: ${status}`)
    : `${front}${front && !front.endsWith('\n') ? '\n' : ''}status: ${status}`
  return open[0] + next + after
}

/**
 * The draft an Approve opens a new chat with. Never sent: the person reads it
 * and presses Send. An upstream issue is shown before it is filed and filed
 * only on their yes; everything else is made in that chat, with them.
 */
export function approveDraft(kind: Proposal['kind'], path: string): string {
  if (kind === 'upstream-issue') {
    return `I approve this proposal: ${path}. Show me the issue text first and ask me before filing it; never file it without my yes. Once it is filed, set the proposal's status to applied, link the issue in its body, run ciao proposal-check on it, and tell me what changed.`
  }
  return `I approve this proposal: ${path}. Make exactly the change it describes, set its status to applied, run ciao proposal-check on it, and tell me what changed.`
}
