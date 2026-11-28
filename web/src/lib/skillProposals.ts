/**
 * Memory · Skills: the open skill-edit proposals as
 * `GET /api/memory/skill-proposals` answers them (`ciao/skill_proposals.py`).
 * Pure helpers, no Vue.
 */
import { proposalSources, type Proposal } from './proposalReview'

export interface SkillProposalItem {
  /** Vault-relative: `Workspace/Proposals/<slug>.md`. */
  path: string
  /** The file as the file viewer opens it. */
  open_path: string
  /** The file holds credential-shaped text: only its path, status and date are sent. */
  withheld: boolean
  type: 'skill-edit'
  target: string
  title: string
  summary: string
  rationale: string
  proposed_change: string
  evidence: string[]
  created: string
  status: 'draft' | 'ready'
  body: string
  /** What `ciao proposal-check` finds wrong with the file; empty when it is valid. */
  problems: string[]
}

export interface SkillProposalList {
  workspace: string
  found: boolean
  items: SkillProposalItem[]
  counts: { waiting: number }
  /** Proposal-named files that could not be read safely (a link, too large). */
  skipped: number
  truncated: boolean
}

/** The item as the shared proposal detail reads it. */
export function skillProposal(item: SkillProposalItem): Proposal {
  return {
    kind: 'skill-edit',
    target: item.target,
    title: item.title,
    summary: item.summary,
    rationale: item.rationale,
    proposedChange: item.proposed_change,
    evidence: item.evidence,
    status: item.status,
    created: item.created,
    body: item.body,
  }
}

/** The row's title: the proposal's own, else its file name. */
export function skillTitle(item: SkillProposalItem): string {
  return item.title || item.path.split('/').pop() || item.path
}

/** The row's muted line: the skill, and how many chats the evidence names. */
export function skillRowSub(item: SkillProposalItem): string {
  if (item.withheld) return 'Holds credential-shaped text: not shown'
  const chats = proposalSources(item.evidence).filter(s => s.kind === 'chat').length
  return [item.target, chats ? `from ${chats} ${chats === 1 ? 'chat' : 'chats'}` : ''].filter(Boolean).join(' · ')
}
