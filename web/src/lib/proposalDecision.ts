/**
 * Approve and Reject for one proposal file, shared by the care page's
 * proposal review and Memory · Skills. Neither writes a skill, a guide or a
 * note:
 *
 * - Approve opens a new chat in General with the proposal pinned and
 *   `approveDraft` as an unsent draft; the change is made in that attended
 *   chat, with the person.
 * - Reject re-reads the engine's view and the file, writes nothing when either
 *   now says the proposal is decided, and otherwise changes only the file's
 *   `status:` line, saved through the file viewer's own path
 *   (`POST /api/workspace-file`).
 * - Undo of a Reject puts the previous status back the same way, only while
 *   the file still says `rejected`.
 */
import { startFileDiscussion } from './fileDiscussion'
import { approveDraft, parseProposal, proposalIsOpen, withProposalStatus, type Proposal } from './proposalReview'
import type { useProjectStore } from '../stores/projects'
import type { ChatInfo } from './types'

type ProjectStore = ReturnType<typeof useProjectStore>

export async function readProposalFile(openPath: string): Promise<string> {
  const resp = await fetch(`/api/workspace-file?exact=1&path=${encodeURIComponent(openPath)}`, { credentials: 'same-origin' })
  if (resp.status === 404) throw new Error('The proposal file is not there any more.')
  if (!resp.ok) throw new Error(`Could not read the proposal (HTTP ${resp.status}).`)
  return resp.text()
}

async function saveProposalFile(openPath: string, content: string): Promise<void> {
  const resp = await fetch('/api/workspace-file', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ path: openPath, content }),
  })
  if (!resp.ok) throw new Error(`Could not save the file (HTTP ${resp.status}).`)
}

/** Open the attended chat that makes the change; null when it could not be opened (a toast says why). */
export function approveProposal(
  store: ProjectStore,
  { openPath, proposal, title, workspace }: { openPath: string; proposal: Proposal; title: string; workspace: string },
): Promise<ChatInfo | null> {
  return startFileDiscussion(store, {
    path: openPath,
    seed: approveDraft(proposal.kind, openPath),
    title: `Approve: ${title.slice(0, 60)}`,
    workspace,
  })
}

export type RejectOutcome =
  | { kind: 'rejected'; previous: string }
  /** The engine or the file now says it was decided, or it changed: nothing was written. */
  | { kind: 'decided'; fresh: Proposal }
  /** The file is no longer in the proposal format: nothing was written. */
  | { kind: 'not-proposal' }

/**
 * Reject one proposal. `engineStillOpen` re-reads the engine's own view
 * (its read refuses a symlink) and says whether it still lists the proposal as
 * open; it runs before the file is read again. Throws on a read or save error.
 */
export async function rejectProposal(openPath: string, engineStillOpen: () => Promise<boolean>): Promise<RejectOutcome> {
  const stillOpen = await engineStillOpen()
  const current = await readProposalFile(openPath)
  const fresh = parseProposal(current)
  if (!fresh) return { kind: 'not-proposal' }
  if (!stillOpen || !proposalIsOpen(fresh.status)) return { kind: 'decided', fresh }
  const next = withProposalStatus(current, 'rejected')
  if (next === null) throw new Error('This file is not in the proposal format, so it was left as it is.')
  await saveProposalFile(openPath, next)
  return { kind: 'rejected', previous: fresh.status }
}

/**
 * Undo a Reject: put `previous` back, only while the file still says
 * `rejected`. Resolves '' when written, else a sentence for the person.
 */
export async function unrejectProposal(openPath: string, previous: string): Promise<string> {
  const status = previous === 'ready' ? 'ready' : 'draft'
  const current = await readProposalFile(openPath)
  const fresh = parseProposal(current)
  if (!fresh || fresh.status !== 'rejected') return 'The proposal file changed since, so nothing was undone.'
  const next = withProposalStatus(current, status)
  if (next === null) return 'The proposal file changed since, so nothing was undone.'
  await saveProposalFile(openPath, next)
  return ''
}
