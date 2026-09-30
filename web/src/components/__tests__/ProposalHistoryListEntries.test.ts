// @vitest-environment jsdom

/** An *entry*-scope decision in History, at the entry's own scale.
 *
 * The three `*_entry` operations write one list item and nothing else, so a
 * history row that showed only the whole-note before/after would present two
 * images differing by a single line — and for a retirement, a line that is
 * simply gone. This pins the unit, the reversible claim, and the two ways the
 * entry diff can legitimately be withheld.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import ProposalHistoryList from '../ProposalHistoryList.vue'
import type { ProposalHistoryNoteEdit, ProposalHistoryRow } from '../../lib/types'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())

vi.mock('../../lib/api', () => ({
  api: { get: apiGet, post: apiPost, patch: vi.fn(), del: vi.fn() },
}))

const ENTRY = '- Landlord is Mr Silva [verified: 2019-05-01]\n'
const NOTE_BEFORE =
  '---\ntype: person\n---\n\n# Alice\n\n' +
  '- Lives in Porto [verified: 2026-09-28]\n' +
  ENTRY
const NOTE_AFTER =
  '---\ntype: person\n---\n\n# Alice\n\n- Lives in Porto [verified: 2026-09-28]\n'

function entryEdit(over: Partial<ProposalHistoryNoteEdit> = {}): ProposalHistoryNoteEdit {
  return {
    id: 'side-1',
    relative_path: 'People/Alice.md',
    operation: 'retire_entry',
    outcome: 'retire',
    coverage: 'complete',
    before: NOTE_BEFORE,
    after: NOTE_AFTER,
    reason: "the employer's name is not this person any more",
    evidence: [{
      source_type: 'url', source_ref: 'https://example.test/registry',
      quoted: 'No such company on file.', supports: 'People/Alice.md',
    }],
    settled: '2026-09-30T10:00:00Z',
    accepted: true,
    receipt_id: 'mrcpt_entry',
    pending: false,
    scope: 'entry',
    entry_identity: 'a'.repeat(64),
    entry_fingerprint: 'b'.repeat(64),
    entry_span: [NOTE_BEFORE.length - ENTRY.length, NOTE_BEFORE.length],
    entry_before: ENTRY,
    entry_after: '',
    entry_removed: true,
    entry_recovery_error: '',
    ...over,
  }
}

function row(over: Partial<ProposalHistoryRow> = {}): ProposalHistoryRow {
  return {
    id: 'h1', ts: '2026-09-30T10:00:00+00:00', action: 'accepted', via: 'pwa',
    kind: 'note_edit',
    text: 'People/Alice.md — retire_entry: the employer is not this person any more',
    source: '', workspace: 'personal', destination: 'People/Alice.md',
    outcome: 'written', proposal_id: 'p1',
    note_edit: entryEdit(over.note_edit ?? {}),
    ...over,
  }
}

describe('an entry-scope decision in History', () => {
  let pinia: ReturnType<typeof createPinia>

  beforeEach(() => {
    pinia = createPinia()
    setActivePinia(pinia)
    apiGet.mockReset()
    apiPost.mockReset()
  })

  afterEach(() => {
    document.body.innerHTML = ''
    vi.restoreAllMocks()
  })

  async function render(rows: ProposalHistoryRow[]) {
    apiGet.mockResolvedValue({ rows, total: rows.length, truncated: false })
    const wrapper = mount(ProposalHistoryList, { global: { plugins: [pinia] } })
    await flushPromises()
    return wrapper
  }

  it('names the operation at the entry scale and shows that one fact, not the file', async () => {
    const wrapper = await render([row()])
    const details = wrapper.get('.ph-verify')
    // Past tense, because a history row records a decision that already happened,
    // and the unit is in the label: "would rewrite the whole note" about an accept
    // that changes a single bullet is the exact claim this row exists to avoid.
    expect(details.text()).toContain('removed one fact from the note')
    expect(details.text()).toContain('Landlord is Mr Silva')
    // Coverage is about the fact, not the file, for the same reason.
    expect(details.text()).toContain('Covering all of the fact')
    // The whole note is not reprinted as if it were the change.
    expect(wrapper.findAll('.ph-verify-text')).toHaveLength(1)
    wrapper.unmount()
  })

  it('says a removed fact is reversible, and does not claim the note was rewritten', async () => {
    const wrapper = await render([row()])
    const text = wrapper.get('.ph-verify').text()
    expect(text).toContain('Removed')
    expect(text).toContain('every other fact in the note is untouched')
    expect(text).toContain('Undo below restores the file exactly as it was')
    wrapper.unmount()
  })

  it('shows both images for a rewrite of one fact', async () => {
    const wrapper = await render([
      row({
        note_edit: entryEdit({
          operation: 'replace_entry',
          outcome: 'update',
          after: NOTE_AFTER.replace(
            '- Lives in Porto [verified: 2026-09-28]',
            '- Lives in Lisbon [verified: 2026-09-30]\n- Landlord is Mr Costa [verified: 2026-09-30]',
          ),
          entry_after: '- Landlord is Mr Costa [verified: 2026-09-30]',
          entry_removed: false,
        }),
      }),
    ])
    const blocks = wrapper.findAll('.ph-verify-text--entry')
    expect(blocks).toHaveLength(2)
    expect(blocks[0].text()).toContain('Mr Silva')
    expect(blocks[1].text()).toContain('Mr Costa')
    expect(wrapper.get('.ph-verify').text()).toContain('rewrote one fact inside the note')
    wrapper.unmount()
  })

  it('withholds the entry diff when the recorded splice could not be read back', async () => {
    // "Removed" and "we could not work out what it would have been" are very
    // different things to show where a diff would be, so a record that cannot be
    // inverted says so instead of rendering an empty after-image as if it were
    // the new text.
    const wrapper = await render([
      row({
        note_edit: entryEdit({
          entry_after: '', entry_removed: false,
          entry_recovery_error: 'the recorded after image is not this edit',
        }),
      }),
    ])
    const text = wrapper.get('.ph-verify').text()
    expect(text).toContain('could not be read back')
    expect(text).not.toContain('Removed.')
    expect(wrapper.findAll('.ph-verify-text--entry')).toHaveLength(1)
    wrapper.unmount()
  })

  it('still shows the whole note for a server that sends no scope', async () => {
    // A server older than this client has no entry operations to have recorded,
    // and absent means `note`.
    const legacy = entryEdit()
    delete legacy.scope
    delete legacy.entry_before
    delete legacy.entry_after
    delete legacy.entry_removed
    const wrapper = await render([row({ note_edit: legacy })])
    expect(wrapper.get('.ph-verify').text()).toContain('Covering all of the note')
    expect(wrapper.findAll('.ph-verify-text--entry')).toHaveLength(0)
    expect(wrapper.findAll('.ph-verify-text').length).toBeGreaterThanOrEqual(2)
    wrapper.unmount()
  })
})
