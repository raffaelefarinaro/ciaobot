// @vitest-environment jsdom

/** The entry-level block on a Vault Review row.
 *
 * A note is not the unit anybody keeps current, and this row is where a person
 * finds out which fact in one needs a look. What is pinned here is that the row
 * names the exact bullet, states the reason it is on the list once per reason
 * rather than once per fact, keeps the per-fact age where it is per fact,
 * reports a coverage gap plainly, and *links* the pending entry proposal rather
 * than offering a second decision of its own.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import VaultReviewPanel from '../VaultReviewPanel.vue'
import { useProjectStore } from '../../stores/projects'
import { pendingConfirm } from '../../lib/confirm'
import type {
  ProjectInfo, VaultReviewCandidate, VaultReviewEntryCoverage,
} from '../../lib/types'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
vi.mock('../../lib/api', () => ({
  api: { get: apiGet, post: apiPost, patch: vi.fn(), del: vi.fn() },
}))

const push = vi.hoisted(() => vi.fn())
vi.mock('vue-router', () => ({
  useRouter: () => ({ push }),
  useRoute: () => ({ params: {}, query: {} }),
  RouterLink: { template: '<a><slot /></a>' },
}))

function coverage(over: Partial<VaultReviewEntryCoverage> = {}): VaultReviewEntryCoverage {
  return {
    entries: 3, checked: 3, exempt: 0, unverified: 1, uncovered: 0, stale: 1,
    coverage_ratio: 0.8, fully_verified: false,
    stale_entries: [], more_stale_entries: 0, proposals: [], more_proposals: 0,
    ...over,
  }
}

const IDENTITY = 'a'.repeat(64)

function staleEntry(over = {}) {
  return {
    identity: IDENTITY, line_number: 8, section: 'Alice',
    // The engine strips markdown from the excerpt, so the panel never sees a
    // leading `-` or `**` — it renders plain prose. The context is the nearest
    // non-fact line, which for a dense list is the note's heading, not the
    // sibling bullet (that used to be reprinted here and read as a second fact).
    excerpt: 'Landlord is Mr Silva [verified: 2019-05-01]',
    context: ['# Alice'],
    reason: 'aged', detail: 'unverified for 2698d against a 90d horizon',
    age_days: 2698, last_verified: '2019-05-01', own_date: true, supported: true,
    ...over,
  }
}

function candidate(overrides: Partial<VaultReviewCandidate> = {}): VaultReviewCandidate {
  return {
    candidate_id: 'abc123abc123abc123abc123',
    workspace: 'personal',
    path: 'memory-vault/People/Alice.md',
    content_hash: 'deadbeef',
    signals: ['unverified_entries'],
    priority: 0,
    evidence: {
      backlinks: [], outbound_links: [], bridge: false, duplicate_group: [],
      last_update: '2026-09-29', type: 'person', age_days: 1,
      entry_verification: coverage({ stale_entries: [staleEntry()] }),
    },
    status: 'candidate', disposition: '', deferred_until: '',
    completable: false, completion_moves_folder: false,
    // The engine's own answer, and the client must not re-derive it: a note
    // whose only finding is an overdue fact gets no Retire button.
    retirement_offered: false,
    ...overrides,
  }
}

function generalProject(): ProjectInfo {
  return {
    project_id: 'p-general', name: 'General', workspace: 'personal', context: '',
    created_at: '2026-09-01T00:00:00Z', order: 0, vault_folder: '', is_auto: true,
  }
}

describe('VaultReviewPanel — the facts inside a note', () => {
  let pinia: ReturnType<typeof createPinia>

  beforeEach(() => {
    pinia = createPinia()
    setActivePinia(pinia)
    apiGet.mockReset()
    apiPost.mockReset()
    apiPost.mockResolvedValue({ ok: true })
    push.mockReset()
    push.mockResolvedValue(undefined)
    const projects = useProjectStore()
    projects.projects = [generalProject()]
    projects.activeWorkspace = 'personal'
  })

  afterEach(() => {
    document.body.innerHTML = ''
    pendingConfirm.value?.resolve(false)
    vi.restoreAllMocks()
  })

  async function mountWith(rows: VaultReviewCandidate[]): Promise<VueWrapper> {
    apiGet.mockResolvedValue({ candidates: rows, trashed: [] })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()
    return wrapper
  }

  it('names the exact fact, its section, and the line around it', async () => {
    const wrapper = await mountWith([candidate()])
    const block = wrapper.get('.vr-entries')
    expect(block.text()).toContain('Facts inside this note')
    expect(block.text()).toContain('3 facts in the note')
    expect(wrapper.get('.vr-entry-text').text()).toContain('Landlord is Mr Silva')
    expect(wrapper.get('.vr-entry-section').text()).toContain('Alice')
    // The context is the point of the block: a reader deciding whether a bullet
    // is current needs the neighbouring line, not just the sentence.
    expect(wrapper.get('.vr-entry-context').text()).toContain('# Alice')
    wrapper.unmount()
  })

  it('renders a markdown fact as prose and does not repeat its sibling as context', async () => {
    // The boxed fact is plain text: the engine strips the markers before the
    // panel sees them, so a bold, ordered list item reads "Learn. People…"
    // rather than "1. **Learn.** People…". And the grey context line is a
    // neighbour that is *not* itself a fact — a sibling bullet reprinted here
    // read as the same claim made twice.
    const wrapper = await mountWith([
      candidate({
        evidence: {
          ...candidate().evidence,
          entry_verification: coverage({
            stale_entries: [
              staleEntry({
                excerpt: 'Learn. People leave each session knowing more.',
                context: ['# Sessions'],
              }),
            ],
          }),
        },
      }),
    ])
    const text = wrapper.get('.vr-entry-text').text()
    expect(text).toContain('Learn. People leave each session knowing more.')
    expect(text).not.toContain('**')
    expect(text).not.toContain('1.')
    const context = wrapper.get('.vr-entry-context').text()
    expect(context).toContain('# Sessions')
    expect(context).not.toContain('Learn. People')
    wrapper.unmount()
  })

  it('states a reason once for a note whose facts all share it', async () => {
    // An unstamped fact inherits the note's date, and saying "checked
    // 2026-09-29" without saying whose date that is would read as a claim about
    // the fact that was never made. What a fact with no stamp of its own means
    // is the same for every such fact, though: a note with five of them used to
    // say it five times, so the reason is stated once and the rows carry only
    // what differs.
    const wrapper = await mountWith([
      candidate({
        evidence: {
          ...candidate().evidence,
          entry_verification: coverage({
            stale_entries: [
              staleEntry({
                reason: 'no-stamp', own_date: false, last_verified: '2026-09-29',
                age_days: 1, detail: "nobody has recorded a [verified:] check on this entry",
              }),
              staleEntry({
                identity: 'c'.repeat(64), line_number: 14, section: 'Address',
                excerpt: '- Flat above the bakery', context: [],
                reason: 'no-stamp', own_date: false, last_verified: '2026-09-29',
                age_days: 1, detail: "nobody has recorded a [verified:] check on this entry",
              }),
            ],
          }),
        },
      }),
    ])
    // One head for the one reason, not one per fact.
    const heads = wrapper.findAll('.vr-entry-reason-head')
    expect(heads).toHaveLength(1)
    expect(heads[0].text()).toContain('Never checked')
    expect(heads[0].text()).toContain('carries the note’s date')
    // Both facts are still named in full: grouping is about the shared sentence,
    // not about dropping rows.
    expect(wrapper.findAll('.vr-entry')).toHaveLength(2)
    expect(wrapper.findAll('.vr-entry-text')[1].text()).toContain('Flat above the bakery')
    // The per-row restatement of that same sentence is gone, along with the row
    // that spelled out the note's date once per bullet.
    expect(wrapper.text()).not.toContain('this fact carries no stamp')
    expect(wrapper.find('.vr-entry-dates').exists()).toBe(false)
    wrapper.unmount()
  })

  it('gives each reason its own head, and keeps the age on the fact it belongs to', async () => {
    // `age_days` is per fact and cannot live in a group's sentence — a figure
    // above two facts would read as a claim about both — so the one row whose
    // reason carries a number keeps it, and the row that does not has none.
    const wrapper = await mountWith([
      candidate({
        evidence: {
          ...candidate().evidence,
          entry_verification: coverage({
            stale_entries: [
              staleEntry(),
              staleEntry({
                identity: 'd'.repeat(64), line_number: 14, section: 'Address',
                excerpt: '- Flat above the bakery', context: [],
                reason: 'no-stamp', own_date: false, last_verified: '2026-09-29',
                age_days: 1, detail: "nobody has recorded a [verified:] check on this entry",
              }),
            ],
          }),
        },
      }),
    ])
    const heads = wrapper.findAll('.vr-entry-reason-head')
    // Two reasons, so two heads — the count follows the reasons, not the facts.
    expect(heads).toHaveLength(2)
    expect(heads[0].text()).toContain('Last checked too long ago')
    expect(heads[0].text()).toContain('older than the review horizon')
    expect(heads[1].text()).toContain('Never checked')
    const rows = wrapper.findAll('.vr-entry')
    expect(rows).toHaveLength(2)
    expect(rows[0].get('.vr-entry-age').text()).toBe('unverified for 2698d')
    expect(rows[1].find('.vr-entry-age').exists()).toBe(false)
    wrapper.unmount()
  })

  it('says plainly when part of the note is prose nothing here can read as a fact', async () => {
    const wrapper = await mountWith([
      candidate({
        evidence: {
          ...candidate().evidence,
          entry_verification: coverage({ uncovered: 2 }),
        },
      }),
    ])
    const head = wrapper.get('.vr-entries-head').text()
    expect(head).toContain('2 blocks of prose not read as facts')
    // "Never counted as verified" is the whole point: an unmeasured note is not a
    // clean one, and a badge that hid this would be the bug the count exists for.
    expect(head).toContain('never counted as verified')
    wrapper.unmount()
  })

  it('links a pending entry proposal rather than offering a second accept', async () => {
    const wrapper = await mountWith([
      candidate({
        evidence: {
          ...candidate().evidence,
          entry_verification: coverage({
            stale_entries: [staleEntry()],
            proposals: [{
              identity: IDENTITY, proposal_id: 'p-1', operation: 'retire_entry',
              outcome: 'retire', checked_at: '2026-09-30', retry_after: '2026-10-30',
              coverage: 'complete', reason: 'the employer is not this person any more',
              citations: 2, receipt_id: '', conflicted: false,
            }],
          }),
        },
      }),
    ])
    const decision = wrapper.get('.vr-entry-decision').text()
    // The operation is named because the three do genuinely different things to
    // the file, and the qualifiers say the rest of the note is untouched.
    expect(decision).toContain('Removes this one fact')
    expect(decision).toContain('checked')
    expect(wrapper.get('.vr-entry-decision .vr-pending-link').text()).toBe('Open the proposal')
    wrapper.unmount()
  })

  it('reports an entry proposal the note has left instead of linking it', async () => {
    const wrapper = await mountWith([
      candidate({
        evidence: {
          ...candidate().evidence,
          entry_verification: coverage({
            stale_entries: [staleEntry()],
            proposals: [{
              identity: IDENTITY, proposal_id: 'p-1', operation: 'replace_entry',
              outcome: 'update', checked_at: '2026-09-30', retry_after: '2026-10-30',
              coverage: 'complete', reason: '', citations: 1, receipt_id: '',
              conflicted: true,
            }],
          }),
        },
      }),
    ])
    expect(wrapper.get('.vr-entry-conflict').text()).toContain('no longer')
    expect(wrapper.get('.vr-entry-conflict').text()).toContain('cannot be applied')
    // Still linked — the reader needs to reach the record — but never described
    // as a decision waiting on them.
    expect(wrapper.text()).not.toContain('Waiting in Suggested')
    wrapper.unmount()
  })

  it('says every fact is current on a note where they all are', async () => {
    const wrapper = await mountWith([
      candidate({
        evidence: {
          ...candidate().evidence,
          entry_verification: coverage({
            entries: 2, checked: 2, stale: 0, unverified: 0, fully_verified: true,
            stale_entries: [],
          }),
        },
      }),
    ])
    expect(wrapper.get('.vr-entries').text()).toContain('Every fact written as a list item')
    expect(wrapper.find('.vr-entry').exists()).toBe(false)
    wrapper.unmount()
  })

  it('lists a decision waiting on a fact that is not on the overdue list', async () => {
    // A `restamp_entry` filed yesterday whose entry has since been re-stamped
    // by a whole-note verdict is real and still waiting for an answer. Dropping
    // it because the fact is no longer overdue would leave a question in the
    // queue with nothing on this surface saying it is there — the exact failure
    // the "link the decision, do not duplicate it" rule exists to prevent.
    const wrapper = await mountWith([
      candidate({
        evidence: {
          ...candidate().evidence,
          entry_verification: coverage({
            stale_entries: [],
            proposals: [{
              identity: 'b'.repeat(64), proposal_id: 'p-2', operation: 'restamp_entry',
              outcome: 'still_valid', checked_at: '2026-09-20', retry_after: '2026-10-20',
              coverage: 'complete', reason: 're-stamped and ready to apply', citations: 1,
              receipt_id: '', conflicted: false,
            }],
          }),
        },
      }),
    ])
    const waiting = wrapper.get('.vr-entries-head--waiting')
    expect(waiting.text()).toContain('Decisions waiting on you')
    expect(waiting.text()).toContain('1 fact')
    expect(waiting.text()).toContain('not on the overdue list')
    expect(wrapper.get('.vr-entry-decision').text()).toContain('Marks this one fact as checked again')
    wrapper.unmount()
  })

  it('says nothing is waiting when every proposal is attached to a named fact', async () => {
    const wrapper = await mountWith([
      candidate({
        evidence: {
          ...candidate().evidence,
          entry_verification: coverage({
            stale_entries: [staleEntry()],
            proposals: [{
              identity: IDENTITY, proposal_id: 'p-1', operation: 'replace_entry',
              outcome: 'update', checked_at: '2026-09-30', retry_after: '2026-10-30',
              coverage: 'complete', reason: '', citations: 1, receipt_id: '',
              conflicted: false,
            }],
          }),
        },
      }),
    ])
    // A second "waiting on you" list beside a decision already shown under its
    // fact is the duplication this whole arrangement exists to avoid.
    expect(wrapper.find('.vr-entries-head--waiting').exists()).toBe(false)
    expect(wrapper.findAll('.vr-entry-decision')).toHaveLength(1)
    wrapper.unmount()
  })

  it('renders a note from a server that sends no entry block at all', async () => {
    // A server older than this client has no entry operations to describe, so
    // absent means absent — never a default object of zeroes, which would read
    // as "read nothing, found nothing wrong".
    const row = candidate()
    delete (row.evidence as { entry_verification?: unknown }).entry_verification
    const wrapper = await mountWith([row])
    expect(wrapper.find('.vr-entries').exists()).toBe(false)
    expect(wrapper.findAll('.vr-row')).toHaveLength(1)
    wrapper.unmount()
  })

  it('does not offer Retire on a note whose only finding is an overdue fact', async () => {
    const wrapper = await mountWith([candidate()])
    // The engine's `retirement_offered: false` is what the panel reads, and the
    // claim under test is that a re-read is not a disposal: no Retire, and no
    // Complete in its place either.
    const labels = wrapper.findAll('button').map(b => b.text())
    expect(labels).not.toContain('Retire')
    expect(labels).not.toContain('Complete')
    wrapper.unmount()
  })
})
