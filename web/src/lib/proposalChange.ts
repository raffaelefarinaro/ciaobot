/** What accepting a suggestion does to which file, read off its server preview.
 *
 * Every suggestion has to say, on the row and without opening anything,
 * whether it creates a note, adds to one, or changes one that is already
 * there. The server already works that out per row (`GET
 * /api/proposals/{id}/preview`, see `preview_row` in
 * `ciao/web/proposal_service.py`); this turns its `action` / `operation` /
 * `exact` / `before` into one change type, the verb for the row's button and
 * the short qualifier beside the destination.
 *
 *   operation  condition                                 type
 *   ---------  ----------------------------------------  --------
 *   add        a people note, or a file whose `before`   new      New note
 *              is empty (not a bounded region)
 *   add        otherwise                                 add      Add to a note
 *   update     exact (a learning's recurrence bump)      update   Update a line
 *   update     not exact (a model folds it in on accept) merge    Merge into a note
 *   move       —                                         move     Move a note
 *   none       can_accept                                none     Already saved
 *   none / ''  cannot be accepted as it stands           blocked  Cannot save yet
 *
 * Rows that never get a preview have their own types: a skill proposal is
 * built in a chat (`skill`), a row with nowhere to go yet needs a decision
 * (`decide`), and a preview still loading is `pending`.
 *
 * Pure and Vue-free, so the table is tested without mounting the panel.
 */
import type { ProposalPreview, ProposalRow } from './types'

export type ProposalChangeType =
  | 'new' | 'add' | 'update' | 'merge' | 'move' | 'none' | 'blocked'
  | 'skill' | 'decide' | 'pending'

export interface ProposalChange {
  type: ProposalChangeType
  /** The tag on the row: "New note", "Add to a note", … */
  label: string
  /** The row's button: "Create note", "Add line", "Merge", … Empty when the
   * row has no direct accept. */
  verb: string
  /** Where it goes, as shown in mono. */
  destination: string
  /** The muted words after the destination. */
  qualifier: string
}

/** Filter chips, in this order; a type with no rows gets no chip. */
export const CHANGE_FILTERS: { type: ProposalChangeType; label: string }[] = [
  { type: 'new', label: 'New note' },
  { type: 'add', label: 'Add to a note' },
  { type: 'merge', label: 'Merge' },
  { type: 'update', label: 'Update a line' },
  { type: 'move', label: 'Move' },
  { type: 'none', label: 'Already saved' },
  { type: 'blocked', label: 'Cannot save yet' },
  { type: 'skill', label: 'Skill' },
  { type: 'decide', label: 'Needs a decision' },
]

const REGION_NAMES: Record<string, string> = {
  memory: 'Agent memory',
  profile: 'User profile',
  user: 'User profile',
}

/** Whether the preview writes a bounded region of the workspace guide. */
export function isRegionPreview(preview: ProposalPreview): boolean {
  return preview.action === 'edit_region' || preview.destination.startsWith('ciao:')
}

function regionQualifier(preview: ProposalPreview): string {
  const region = preview.destination.replace(/^ciao:/, '')
  const name = REGION_NAMES[region] ?? `${region} region`
  return `${name}, always loaded`
}

/** Where a preview writes, as a path the user recognises. A region lives in
 * the workspace guide, so it is shown as that file rather than `ciao:memory`. */
function destinationOf(row: ProposalRow, preview: ProposalPreview): string {
  if (isRegionPreview(preview)) {
    const ws = preview.workspace || row.workspace
    return ws ? `${ws}/AGENTS.md` : 'AGENTS.md'
  }
  return preview.destination
}

export function changeFor(
  row: ProposalRow,
  preview: ProposalPreview | undefined,
  opts: { canAccept: boolean; fallbackQualifier?: string } = { canAccept: true },
): ProposalChange {
  const fallback = opts.fallbackQualifier ?? ''
  if (row.kind === 'skill') {
    return { type: 'skill', label: 'New skill', verb: '', destination: row.path || '', qualifier: 'built in a chat' }
  }
  if (!opts.canAccept) {
    return { type: 'decide', label: 'Needs a decision', verb: '', destination: '', qualifier: fallback }
  }
  if (!preview) {
    return { type: 'pending', label: '', verb: '', destination: '', qualifier: fallback }
  }
  const destination = destinationOf(row, preview)
  const region = isRegionPreview(preview)
  const op = preview.operation

  if (!preview.can_accept && op !== 'move') {
    return { type: 'blocked', label: 'Cannot save yet', verb: '', destination, qualifier: '' }
  }
  if (op === 'add') {
    const creates = preview.action === 'write_people_note' || (!region && !preview.before.trim())
    if (creates) {
      return { type: 'new', label: 'New note', verb: 'Create note', destination, qualifier: 'does not exist yet' }
    }
    return {
      type: 'add',
      label: 'Add to a note',
      verb: region ? 'Add entry' : 'Add line',
      destination,
      qualifier: region ? regionQualifier(preview) : '',
    }
  }
  if (op === 'update') {
    if (preview.exact) {
      return {
        type: 'update',
        label: 'Update a line',
        verb: 'Update line',
        destination,
        qualifier: region ? regionQualifier(preview) : 'the line is already there',
      }
    }
    return {
      type: 'merge',
      label: 'Merge into a note',
      verb: 'Merge',
      destination,
      qualifier: 'a model folds it in when you accept',
    }
  }
  if (op === 'move') {
    const to = (preview.destination || row.rehome?.destination || '').split('/')[0]
    return {
      type: 'move',
      label: 'Move a note',
      verb: to ? `Move to ${to}` : 'Move',
      destination: row.rehome?.note || preview.destination,
      qualifier: to ? `to the ${to} workspace` : '',
    }
  }
  if (op === 'none') {
    return {
      type: 'none',
      label: 'Already saved',
      verb: 'Clear',
      destination,
      qualifier: region ? regionQualifier(preview) : '',
    }
  }
  return { type: 'blocked', label: 'Cannot save yet', verb: '', destination, qualifier: '' }
}
