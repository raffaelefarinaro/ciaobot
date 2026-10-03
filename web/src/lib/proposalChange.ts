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
 *   add_category a new category for a note cluster       category New category
 *   note_edit  scope `entry`, restamp_entry              restamp  Check one fact again
 *   note_edit  scope `entry`, otherwise                  editFact Change one fact
 *   note_edit  a whole note rewritten by its verification edit     Update a note
 *   retire_note the note moved to the review trash       retire   Retire a note
 *   none       can_accept                                none     Already saved
 *   none / ''  cannot be accepted as it stands           blocked  Cannot save yet
 *
 * `note_edit` and `retire_note` are separate types, and not `update`/`move`,
 * because both of those borrowed words mean something narrower here: this is the
 * WHOLE note, not a line, and a retirement is a move to a trash somebody can
 * restore from, not a move between workspaces. A row whose accept would rewrite
 * a note must not read as a region edit.
 *
 * The three entry operations get their own three types for the same reason one
 * level down. `replace_entry`, `restamp_entry` and `retire_entry` all report the
 * `note_edit` operation — they are the same write path, scoped to one list item
 * — and they differ in what a person is agreeing to: restamp it, change it, or
 * take it out. "Update a note" is wrong for all three, and worst for the one
 * that removes something, because it is the row where a reader has to be sure
 * the rest of the file survives.
 *
 * Rows that never get a preview have their own types: a skill proposal is
 * built in a chat (`skill`), a row with nowhere to go yet needs a decision
 * (`decide`), and a preview still loading is `pending`.
 *
 * Pure and Vue-free, so the table is tested without mounting the panel.
 */
import type { ProposalPreview, ProposalRow } from './types'

export type ProposalChangeType =
  | 'new' | 'add' | 'update' | 'merge' | 'move' | 'category' | 'edit' | 'retire'
  | 'editFact' | 'restampFact' | 'retireFact'
  | 'none' | 'blocked' | 'skill' | 'decide' | 'pending'

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
  { type: 'category', label: 'New category' },
  { type: 'edit', label: 'Update a note' },
  { type: 'retire', label: 'Retire a note' },
  { type: 'editFact', label: 'Change one fact' },
  { type: 'restampFact', label: 'Check one fact again' },
  { type: 'retireFact', label: 'Retire one fact' },
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
    // "Improve", never "New": the skill the finding is about already exists, so
    // "New skill" described a task the proposal does not ask for.
    return {
      type: 'skill',
      label: 'Improve skill',
      verb: '',
      destination: row.canonical_path || row.path || '',
      qualifier: 'improved in a chat',
    }
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
  if (op === 'add_category') {
    // A new category for a cluster of notes: nothing is moved, so the
    // destination is the id the accept would add to the registry and the
    // qualifier is the half of the change the id does not say. The verb is
    // what puts the row's own accept button on it — without one the row read as
    // "cannot be saved" even though the accept works.
    return {
      type: 'category',
      label: 'New category',
      verb: 'Add category',
      destination,
      qualifier: 'retypes the notes it came from',
    }
  }
  if (op === 'note_edit') {
    // Three entry operations share this one `operation` value and differ only in
    // scope, so the scope decides the row. The qualifier carries the two facts
    // the verb does not: the change is one line of the file rather than the
    // file, and it is reversible — which is the part that matters most on the
    // retirement, where a reader has to be sure the other facts in the note are
    // not going anywhere.
    if (preview.scope === 'entry') {
      if (preview.entry_removed) {
        return {
          type: 'retireFact',
          label: 'Retire one fact',
          verb: 'Retire fact',
          destination,
          qualifier: 'one line of the note; the rest is untouched, undoable from History',
        }
      }
      if (preview.entry_operation === 'restamp_entry') {
        return {
          type: 'restampFact',
          label: 'Check one fact again',
          verb: 'Mark fact checked',
          destination,
          qualifier: 'stamps this one line as checked today; the rest is untouched',
        }
      }
      return {
        type: 'editFact',
        label: 'Change one fact',
        verb: 'Change fact',
        destination,
        qualifier: 'one line of the note; the rest is untouched, undoable from History',
      }
    }
    // A whole note, rewritten from the verification's exact replacement — so the
    // qualifier carries the two facts the verb does not: the change is the whole
    // file rather than an entry in it, and it is reversible.
    return {
      type: 'edit',
      label: 'Update a note',
      verb: 'Update note',
      destination,
      qualifier: 'the whole note; undoable from History',
    }
  }
  if (op === 'retire_note') {
    // A retirement is the one accept that removes something, so the copy says
    // the two things that make it safe: it is a move into a trash, and it can
    // be put back.
    return {
      type: 'retire',
      label: 'Retire a note',
      verb: 'Retire note',
      destination,
      qualifier: 'moved to the review trash, where it can be restored',
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
