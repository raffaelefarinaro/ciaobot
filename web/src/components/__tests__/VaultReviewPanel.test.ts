// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount, type DOMWrapper, type VueWrapper } from '@vue/test-utils'
import VaultReviewPanel from '../VaultReviewPanel.vue'
import { useVaultReviewStore } from '../../stores/vaultReview'
import { useProposalsStore } from '../../stores/proposals'
import { useProjectStore } from '../../stores/projects'
import { useFileViewerStore } from '../../stores/fileViewer'
import { pendingConfirm } from '../../lib/confirm'
import type { ChatInfo, ProjectInfo, VaultReviewCandidate, VaultTrashedNote } from '../../lib/types'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())

vi.mock('../../lib/api', () => ({
  api: { get: apiGet, post: apiPost, patch: vi.fn(), del: vi.fn() },
}))

function candidate(overrides: Partial<VaultReviewCandidate> = {}): VaultReviewCandidate {
  return {
    candidate_id: 'abc123abc123abc123abc123',
    workspace: 'personal',
    path: 'memory-vault/People/Mo.md',
    content_hash: 'deadbeef',
    signals: ['unlinked', 'weak_provenance'],
    priority: 1,
    evidence: {
      backlinks: [],
      outbound_links: [],
      bridge: false,
      duplicate_group: [],
      last_update: '',
      type: 'note',
      age_days: null,
    },
    status: 'candidate',
    disposition: '',
    deferred_until: '',
    completable: false,
    completion_moves_folder: false,
    ...overrides,
  }
}

/** A project the backend marked completable: the panel offers Complete on it. */
function projectCandidate(
  overrides: Partial<VaultReviewCandidate> = {},
): VaultReviewCandidate {
  return candidate({
    candidate_id: 'proj123proj123proj123proj1',
    path: 'memory-vault/projects/active/faraman/Faraman-Calendar.md',
    completable: true,
    // A folder project: completing any note under it closes the whole folder,
    // so the backend says so and the confirm asks about the folder.
    completion_moves_folder: true,
    evidence: { ...candidate().evidence, type: 'project' },
    ...overrides,
  })
}

function generalProject(): ProjectInfo {
  return {
    project_id: 'p-general',
    name: 'General',
    workspace: 'personal',
    context: '',
    created_at: '2026-09-01T00:00:00Z',
    order: 0,
    vault_folder: '',
    is_auto: true,
  }
}

function trashed(overrides: Partial<VaultTrashedNote> = {}): VaultTrashedNote {
  return {
    candidate_id: 'def456def456def456def456',
    workspace: 'personal',
    original_path: 'memory-vault/People/Old.md',
    content_hash: 'cafef00d',
    trashed_at: '2026-09-01T00:00:00Z',
    ...overrides,
  }
}

function buttonByText(wrapper: VueWrapper | DOMWrapper<Element>, text: string) {
  const found = wrapper.findAll('button').find(b => b.text() === text)
  if (!found) throw new Error(`button "${text}" not found`)
  return found
}

describe('VaultReviewPanel', () => {
  let pinia: ReturnType<typeof createPinia>

  beforeEach(() => {
    pinia = createPinia()
    setActivePinia(pinia)
    apiGet.mockReset()
    apiPost.mockReset()
    apiPost.mockResolvedValue({ ok: true })
  })

  afterEach(() => {
    document.body.innerHTML = ''
    pendingConfirm.value?.resolve(false)
    vi.restoreAllMocks()
  })

  it('renders candidates with plain-language reasons from a mocked GET', async () => {
    apiGet.mockResolvedValue({ candidates: [candidate()], trashed: [] })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    expect(apiGet).toHaveBeenCalledWith('/api/vault/review?workspace=personal&include=trashed,cleared')
    expect(wrapper.findAll('.vr-row')).toHaveLength(1)
    expect(wrapper.text()).toContain('Mo')
    // type · reason · reason · backlinks, one short line.
    const why = wrapper.get('.vr-why').text()
    expect(why).toContain('note')
    expect(why).toContain('nothing links to it')
    expect(why).toContain('no date or tags')
    expect(why).toContain('no backlinks')
    wrapper.unmount()
  })

  it('shows an empty state when nothing is flagged', async () => {
    apiGet.mockResolvedValue({ candidates: [], trashed: [] })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    expect(wrapper.text()).toContain('Nothing to revisit')
    wrapper.unmount()
  })

  it('opens with a heading and one sentence, with no refresh or how-to disclosure', async () => {
    apiGet.mockResolvedValue({ candidates: [candidate()], trashed: [] })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    expect(wrapper.get('h2').text()).toBe('1 to revisit')
    const lede = wrapper.get('.vr-lede').text()
    expect(lede).toContain('Still true marks a note checked today')
    // A project is closed, not retired, and the lede has to say so before the
    // row swaps the button out from under the reader.
    expect(lede).toContain('A project offers Complete in its place')
    expect(lede).toContain('moves it to projects/completed/')
    // Retire is scoped to every OTHER note. Promising the trash "for a note
    // that is wrong or abandoned" read as also covering a wrong project, which
    // is the one thing a completable row cannot do — the lede must not offer
    // a button the row does not have.
    expect(lede).toContain('Retire covers every other note')
    expect(lede).toContain('moving it to Retired, where it can be restored')
    expect(lede).not.toContain('wrong or abandoned')
    expect(wrapper.find('.vr-how').exists()).toBe(false)
    expect(wrapper.findAll('button').some(b => b.text() === 'Refresh')).toBe(false)
    wrapper.unmount()
  })

  it('keeps the Still true promise qualified, wherever the copy moves', async () => {
    // "Still true" reports honestly when a note has no frontmatter to stamp.
    // Shortening the surface copy may not shorten that into a flat claim.
    apiGet.mockResolvedValue({ candidates: [candidate()], trashed: [] })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    expect(buttonByText(wrapper, 'Still true').attributes('title'))
      .toContain('when it has frontmatter to stamp')
    wrapper.unmount()
  })

  it('gives every row the same neutral choice, with no pink primary', async () => {
    apiGet.mockResolvedValue({
      candidates: [candidate(), candidate({ candidate_id: 'other', path: 'memory-vault/People/Bo.md' })],
      trashed: [],
    })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    expect(wrapper.findAll('.btn-primary')).toHaveLength(0)
    const actions = wrapper.findAll('.vr-row')[0].findAll('.vr-actions button').map(b => b.text())
    expect(actions).toEqual(['Still true', 'Retire', 'Discuss'])
    expect(wrapper.findAll('.vr-row')[0].find('.vr-actions .mr-link').text()).toBe('Discuss')
    wrapper.unmount()
  })

  it('filters by reason, one chip per reason present, zeros hidden', async () => {
    apiGet.mockResolvedValue({
      candidates: [
        candidate({ candidate_id: 'a', signals: ['unverified'] }),
        candidate({ candidate_id: 'b', path: 'memory-vault/People/Bo.md', signals: ['unverified', 'superseded_language'] }),
        candidate({ candidate_id: 'c', path: 'memory-vault/People/Cy.md', signals: ['unlinked'] }),
      ],
      trashed: [],
    })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const chips = wrapper.findAll('.vr-chips button')
    expect(chips.map(c => c.text())).toEqual([
      'All 3', 'Says it was superseded 1', 'Unchecked too long 2', 'Nothing links to it 1',
    ])
    expect(chips.map(c => c.text()).join(' ')).not.toContain('Possible duplicate')
    expect(chips[0].attributes('aria-pressed')).toBe('true')

    await chips[2].trigger('click')
    expect(wrapper.findAll('.vr-row').map(r => r.find('.vr-title').text())).toEqual(['Mo', 'Bo'])
    expect(wrapper.findAll('.vr-chips button')[2].attributes('aria-pressed')).toBe('true')

    await wrapper.findAll('.vr-chips button')[0].trigger('click')
    expect(wrapper.findAll('.vr-row')).toHaveLength(3)
    wrapper.unmount()
  })

  it('names the age and the limit, and opens where the date came from', async () => {
    apiGet.mockResolvedValue({
      candidates: [
        candidate({
          candidate_id: 'fm',
          signals: ['unverified'],
          evidence: {
            ...candidate().evidence,
            type: 'person',
            backlinks: ['a.md', 'b.md', 'c.md'],
            unverified: { age_days: 95, threshold_days: 90, last_verified: '2026-06-22', source: 'frontmatter' },
          },
        }),
        candidate({
          candidate_id: 'mt',
          path: 'memory-vault/People/Juan.md',
          signals: ['unverified'],
          evidence: {
            ...candidate().evidence,
            type: 'person',
            unverified: { age_days: 102, threshold_days: 90, last_verified: '2026-06-15', source: 'mtime' },
          },
        }),
      ],
      trashed: [],
    })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const [first, second] = wrapper.findAll('.vr-row')
    expect(first.get('.vr-why').text()).toContain('unchecked 3 months (limit 90 days)')
    expect(first.get('.vr-why').text()).toContain('3 backlinks')
    const flag = first.get('.vr-flag')
    expect(flag.attributes('aria-expanded')).toBe('false')
    const box = first.get(`#${flag.attributes('aria-controls')}`)
    expect((box.element as HTMLElement).style.display).toBe('none')

    await flag.trigger('click')
    expect(flag.attributes('aria-expanded')).toBe('true')
    expect((box.element as HTMLElement).style.display).toBe('')
    expect(box.text()).toContain('Last checked 2026-06-22, from the note\'s updated: field.')
    expect(box.text()).toContain('Person notes are due every 90 days.')

    await second.get('.vr-flag').trigger('click')
    expect(second.get('.vr-evidence').text())
      .toContain('No updated: field, so the file\'s modified date is used: 2026-06-15.')
    wrapper.unmount()
  })

  it('quotes the superseded line with its neighbours and opens the note at it', async () => {
    apiGet.mockResolvedValue({
      candidates: [candidate({
        signals: ['superseded_language'],
        evidence: {
          ...candidate().evidence,
          type: 'project',
          superseded: {
            line: 7,
            text: 'Scope change: order numbers moved to their own page.',
            match: 'moved to',
            where: 'lead',
            before: { line: 6, text: '# LVMH P6 OCR trials' },
            after: { line: 8, text: 'Sources: visit 8 Sep.' },
          },
        },
      })],
      trashed: [],
    })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()
    const viewer = useFileViewerStore()
    const open = vi.spyOn(viewer, 'open').mockResolvedValue(true)

    expect(wrapper.get('.vr-why').text()).toContain('says it was superseded')
    await wrapper.get('.vr-flag').trigger('click')
    const box = wrapper.get('.vr-evidence')
    expect(box.text()).toContain('Where it says so · lead paragraph, line 7')
    const lines = box.findAll('.mr-box-line')
    expect(lines.map(l => l.get('.mr-box-num').text())).toEqual(['6', '7', '8'])
    expect(lines[1].classes()).toContain('mr-box-line--hit')
    expect(lines[1].get('mark').text()).toBe('moved to')
    expect(box.text()).toContain('Only the frontmatter and the opening paragraph count.')

    await box.findAll('button').find(b => b.text() === 'Open at line 7')!.trigger('click')
    expect(open).toHaveBeenCalledWith('memory-vault/People/Mo.md', 7)
    wrapper.unmount()
  })

  it('sends keep and trash through their own actions', async () => {
    apiGet.mockResolvedValue({ candidates: [candidate({ candidate_id: 'cid-keep' })], trashed: [] })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    await buttonByText(wrapper, 'Still true').trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/api/vault/review?workspace=personal', {
      action: 'decide',
      candidate_id: 'cid-keep',
      disposition: 'keep',
    })

    await buttonByText(wrapper, 'Retire').trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/api/vault/review?workspace=personal', {
      action: 'trash',
      candidate_id: 'cid-keep',
    })
    wrapper.unmount()
  })

  it('renders the trash inventory with restore and a confirmed permanent delete', async () => {
    apiGet.mockResolvedValue({ candidates: [], trashed: [trashed()] })
    // The trash is its own tab now; the parent picks the section, so the test
    // mounts the one it is about rather than scrolling past the queue.
    const wrapper = mount(VaultReviewPanel, {
      props: { section: 'trash' },
      global: { plugins: [pinia] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('1 retired note')
    expect(wrapper.text()).toContain('Old')

    await buttonByText(wrapper, 'Restore').trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/api/vault/review?workspace=personal', {
      action: 'restore',
      candidate_id: 'def456def456def456def456',
    })

    // Permanent deletion waits on the explicit confirm first.
    const deleteClicked = buttonByText(wrapper, 'Delete forever').trigger('click')
    await flushPromises()
    expect(apiPost).not.toHaveBeenCalledWith(
      '/api/vault/review?workspace=personal',
      expect.objectContaining({ action: 'delete' }),
    )
    pendingConfirm.value?.resolve(true)
    await deleteClicked
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/api/vault/review?workspace=personal', {
      action: 'delete',
      candidate_id: 'def456def456def456def456',
      confirm: 'def456def456def456def456',
    })
    wrapper.unmount()
  })

  it('shows the server excerpt inline and opens the note from its path', async () => {
    apiGet.mockResolvedValue({
      candidates: [candidate({ evidence: { ...candidate().evidence, excerpt: 'Product owner on the OEM side.' } })],
      trashed: [],
    })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()
    const open = vi.spyOn(useFileViewerStore(), 'open').mockResolvedValue(true)

    expect(wrapper.get('.vr-excerpt-inline').text()).toBe('Product owner on the OEM side.')
    await wrapper.get('.vr-path').trigger('click')
    expect(open).toHaveBeenCalledWith('memory-vault/People/Mo.md')
    wrapper.unmount()
  })

  it('opens a seeded discussion chat with the note pinned, and decides nothing', async () => {
    // "Talk about it" is the only action here that must NOT touch the queue:
    // the candidate stays flagged until the user picks a disposition, and the
    // prompt is a draft so their own question can replace it before sending.
    apiGet.mockResolvedValue({ candidates: [candidate()], trashed: [] })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()
    const projects = useProjectStore()
    projects.projects = [generalProject()]
    const createChat = vi
      .spyOn(projects, 'createChat')
      .mockResolvedValue({ chat_id: 'c-new' } as ChatInfo)
    const pinFile = vi.spyOn(projects, 'pinFile').mockResolvedValue(undefined)
    apiPost.mockClear()

    await buttonByText(wrapper, 'Discuss').trigger('click')
    await flushPromises()

    expect(createChat).toHaveBeenCalledTimes(1)
    const [projectId, title, seed] = createChat.mock.calls[0]
    expect(projectId).toBe('p-general')
    expect(title).toBe('Link or retire Mo?')
    expect(seed).toContain('memory-vault/People/Mo.md')
    expect(seed).toContain('no other note links to it')
    expect(seed).toContain('I will pick Still true or Retire myself')
    expect(pinFile).toHaveBeenCalledWith('c-new', 'memory-vault/People/Mo.md')
    // No disposition was recorded: the row is still in the queue.
    expect(apiPost).not.toHaveBeenCalled()
    expect(wrapper.findAll('.vr-row')).toHaveLength(1)
    wrapper.unmount()
  })

  it('offers a way back from a keep, and hides the section when nothing was cleared', async () => {
    // A keep is suppressed by content hash, so without this the only route
    // back into the queue was editing the note.
    apiGet.mockResolvedValue({ candidates: [candidate()], trashed: [], cleared: [] })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()
    expect(wrapper.find('.vr-cleared').exists()).toBe(false)

    const store = useVaultReviewStore()
    store.cleared = [{
      candidate_id: 'cleared01cleared01cleared',
      workspace: 'personal',
      path: 'memory-vault/People/Mo.md',
      content_hash: 'deadbeef',
      decided_at: '2026-09-18T10:00:00+00:00',
    }]
    await flushPromises()

    expect(wrapper.find('.vr-cleared').exists()).toBe(true)
    expect(wrapper.find('.vr-cleared').text()).toContain('cleared 2026-09-18')
    apiPost.mockClear()
    apiPost.mockResolvedValue({ ok: true, candidates: [], trashed: [], cleared: [] })
    await buttonByText(wrapper, 'Add back').trigger('click')
    await flushPromises()

    expect(apiPost).toHaveBeenCalledWith('/api/vault/review?workspace=personal', {
      action: 'reopen',
      candidate_id: 'cleared01cleared01cleared',
    })
    wrapper.unmount()
  })

  it('no longer promises a 30-day retention the engine does not enforce', async () => {
    apiGet.mockResolvedValue({ candidates: [], trashed: [], cleared: [] })
    const wrapper = mount(VaultReviewPanel, {
      props: { section: 'trash' },
      global: { plugins: [pinia] },
    })
    await flushPromises()

    const text = wrapper.text()
    expect(text).not.toContain('30 days')
    expect(text).toContain('nothing is removed on a timer')
    wrapper.unmount()
  })

  it('seeds an unlinked row with a hunt for the notes that should link to it', async () => {
    // The repair for `unlinked` is a link, and a link lives in another note —
    // so the seed sends the agent after those notes instead of only weighing
    // the keep-or-retire question the row asks. `Link fixed` used to claim
    // this repair and perform none of it.
    apiGet.mockResolvedValue({ candidates: [candidate({ signals: ['unlinked'] })], trashed: [] })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()
    const projects = useProjectStore()
    projects.projects = [generalProject()]
    const createChat = vi
      .spyOn(projects, 'createChat')
      .mockResolvedValue({ chat_id: 'c-new' } as ChatInfo)
    vi.spyOn(projects, 'pinFile').mockResolvedValue(undefined)

    await buttonByText(wrapper, 'Discuss').trigger('click')
    await flushPromises()

    const [, title, seed] = createChat.mock.calls[0]
    expect(title).toBe('Link or retire Mo?')
    expect(seed).toContain('search the vault for the notes that should link to it')
    // The approval gate leads. Buried mid-paragraph it was one clause among
    // five, and a model that misses it writes links the user never approved —
    // which, because backlinks are counted live, also clears the row.
    expect((seed as string).split('\n\n').at(-1)).toMatch(/^Write nothing until I say so\./)
    expect(seed).toContain('wait for my go-ahead on each before writing it')
    // The note under review is still not the agent's to change.
    expect(seed).toContain('Do not edit, move, or delete the note itself')
    wrapper.unmount()
  })

  it('keeps the seed read-only when nothing flagged the note as unlinked', async () => {
    apiGet.mockResolvedValue({
      candidates: [candidate({ signals: ['weak_provenance'] })],
      trashed: [],
    })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()
    const projects = useProjectStore()
    projects.projects = [generalProject()]
    const createChat = vi
      .spyOn(projects, 'createChat')
      .mockResolvedValue({ chat_id: 'c-new' } as ChatInfo)
    vi.spyOn(projects, 'pinFile').mockResolvedValue(undefined)

    await buttonByText(wrapper, 'Discuss').trigger('click')
    await flushPromises()

    const [, title, seed] = createChat.mock.calls[0]
    expect(title).toBe('Retire Mo?')
    expect(seed).toContain('Do not edit, move, or delete anything')
    expect(seed).not.toContain('should link to it')
    wrapper.unmount()
  })

  it('shows a retryable load error instead of claiming the queue is empty', async () => {
    apiGet
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce({ candidates: [], trashed: [], cleared: [] })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const alert = wrapper.get('[role="alert"]')
    expect(alert.text()).toContain('Could not load notes to revisit')
    expect(alert.text()).toContain('offline')
    expect(wrapper.text()).not.toContain('Nothing to revisit')

    await alert.get('button').trigger('click')
    await flushPromises()
    expect(apiGet).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('Nothing to revisit')
    wrapper.unmount()
  })

  it('keeps the last successful rows visible when a refresh fails', async () => {
    apiGet
      .mockResolvedValueOnce({ candidates: [candidate()], trashed: [], cleared: [] })
      .mockRejectedValueOnce(new Error('still offline'))
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    await useVaultReviewStore().fetch('personal', { force: true })
    await flushPromises()

    expect(wrapper.findAll('.vr-row')).toHaveLength(1)
    expect(wrapper.get('[role="status"]').text()).toContain('Showing the last successful load')
    expect(wrapper.text()).not.toContain('Nothing to revisit')
    wrapper.unmount()
  })

  it('marks the store busy while a decision is in flight', async () => {
    apiGet.mockResolvedValue({ candidates: [candidate()], trashed: [] })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()
    const store = useVaultReviewStore()

    let release!: () => void
    apiPost.mockImplementationOnce(() => new Promise(resolve => { release = () => resolve({ ok: true }) }))
    const clicked = buttonByText(wrapper, 'Still true').trigger('click')
    await flushPromises()
    expect(store.isBusy('abc123abc123abc123abc123')).toBe(true)
    release!()
    await clicked
    await flushPromises()
    expect(store.isBusy('abc123abc123abc123abc123')).toBe(false)
    wrapper.unmount()
  })

  // ── the "nothing to stamp" notice reaches the user ────────────────────

  it('toasts a keep that cleared the row without stamping the note', async () => {
    apiGet.mockResolvedValue({ candidates: [candidate()], trashed: [] })
    apiPost.mockResolvedValue({
      ok: true,
      candidates: [],
      trashed: [],
      result: {
        candidate_id: 'after',
        previous_candidate_id: 'abc123abc123abc123abc123',
        stamped: false,
        stamp_status: 'no_frontmatter',
      },
    })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()
    const projects = useProjectStore()
    const store = useVaultReviewStore()

    await buttonByText(wrapper, 'Still true').trigger('click')
    await flushPromises()

    const toast = projects.toasts.find(t => t.title === 'Nothing to stamp')
    expect(toast?.body).toContain('no frontmatter to stamp')
    expect(toast?.variant).toBe('info')
    // Drained, so a later remount cannot re-announce the same one.
    expect(store.notice).toBe('')
    wrapper.unmount()
  })

  it('drains a notice raised while the panel was unmounted', async () => {
    // The panel is a `v-if` sibling: flipping to Proposals while the POST is
    // in flight stops the watcher before the notice is set, and a lazy
    // watcher registered fresh on remount never fires for the value already
    // sitting in the ref.
    apiGet.mockResolvedValue({ candidates: [], trashed: [] })
    const store = useVaultReviewStore()
    const projects = useProjectStore()
    store.notice = 'The row is cleared, but this note has no frontmatter to stamp.'

    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    expect(projects.toasts.some(t => t.title === 'Nothing to stamp')).toBe(true)
    expect(store.notice).toBe('')
    wrapper.unmount()
  })

  // ── Complete in place of Retire ───────────────────────────────────────

  it('offers Complete instead of Retire on a project the backend marked completable', async () => {
    apiGet.mockResolvedValue({ candidates: [projectCandidate()], trashed: [] })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const actions = wrapper.findAll('.vr-row')[0].findAll('.vr-actions button').map(b => b.text())
    expect(actions).toEqual(['Still true', 'Complete', 'Discuss'])
    // Never both on one row: two ways out of the same decision, one of which
    // the user has to know about.
    expect(wrapper.findAll('button').some(b => b.text() === 'Retire')).toBe(false)
    // Still the neutral row button, not a pink bar — every row is the same
    // routine choice and the row's one emphasis is the terminal action.
    expect(wrapper.findAll('.vr-row')[0].findAll('.btn-primary')).toHaveLength(0)
    wrapper.unmount()
  })

  it('keeps Retire, and no Complete, on a candidate the backend did not mark', async () => {
    // A project the queue cannot complete into — already under
    // `projects/completed/`, filed outside `projects/`, or with its
    // `projects/completed/` destination already occupied. The panel reads the
    // flag, it does not re-derive it.
    apiGet.mockResolvedValue({
      candidates: [candidate({
        path: 'memory-vault/projects/completed/Faraman-Calendar.md',
        evidence: { ...candidate().evidence, type: 'project' },
        completable: false,
      })],
      trashed: [],
    })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const actions = wrapper.findAll('.vr-row')[0].findAll('.vr-actions button').map(b => b.text())
    expect(actions).toEqual(['Still true', 'Retire', 'Discuss'])
    expect(wrapper.findAll('button').some(b => b.text() === 'Complete')).toBe(false)

    // And the working action is really the trash, with no confirm spent on it:
    // the confirm belongs to the one nothing here can take back.
    await buttonByText(wrapper, 'Retire').trigger('click')
    await flushPromises()
    expect(pendingConfirm.value).toBe(null)
    expect(apiPost).toHaveBeenCalledWith('/api/vault/review?workspace=personal', {
      action: 'trash',
      candidate_id: 'abc123abc123abc123abc123',
    })
    wrapper.unmount()
  })

  it('sends Complete through its own action and drops the row from the queue', async () => {
    apiGet.mockResolvedValue({ candidates: [projectCandidate()], trashed: [] })
    // The POST answers with the queue it had to rebuild: the completed project
    // has left `active/`, so it is not in it.
    apiPost.mockResolvedValue({ ok: true, candidates: [], trashed: [], cleared: [], result: {} })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const clicked = buttonByText(wrapper, 'Complete').trigger('click')
    await flushPromises()
    // The click only arms the confirm; nothing is posted until it is answered.
    expect(apiPost).not.toHaveBeenCalled()
    pendingConfirm.value?.resolve(true)
    await clicked
    await flushPromises()

    expect(apiPost).toHaveBeenCalledWith('/api/vault/review?workspace=personal', {
      action: 'complete',
      candidate_id: 'proj123proj123proj123proj1',
    })
    // Never the trash action as well — the two are alternatives.
    expect(apiPost).not.toHaveBeenCalledWith(
      '/api/vault/review?workspace=personal',
      expect.objectContaining({ action: 'trash' }),
    )
    expect(wrapper.text()).toContain('Nothing to revisit')
    wrapper.unmount()
  })

  it('asks before completing, because nothing here can undo it', async () => {
    // The confirm is the only guard: `restoreCompleted` has no caller, and a
    // completed note is in neither the candidate list nor the trash, so there
    // is no row left to hang an undo on. A misclick rewrites every note that
    // links to the project.
    apiGet.mockResolvedValue({ candidates: [projectCandidate()], trashed: [] })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const clicked = buttonByText(wrapper, 'Complete').trigger('click')
    await flushPromises()

    const ask = pendingConfirm.value
    expect(ask).not.toBe(null)
    // The dialog says what the action does and that it cannot be taken back,
    // rather than leaving "cannot be undone from here" implied by the button.
    expect(ask?.message).toContain('Complete "Faraman-Calendar"?')
    expect(ask?.message).toContain('every note that links to it is rewritten')
    expect(ask?.message).toContain('This cannot be undone from here.')
    // And it names the unit of the move. A folder project takes the plan, the
    // notes and the attachments beside it, so "it moves" would read as a smaller
    // act than the one being confirmed.
    expect(ask?.message).toContain('The whole project folder moves to projects/completed/')
    expect(ask?.destructive).toBe(true)
    expect(ask?.confirmLabel).toBe('Complete')
    expect(ask?.cancelLabel).toBe('Cancel')

    // Declining really does decline.
    pendingConfirm.value?.resolve(false)
    await clicked
    await flushPromises()
    expect(apiPost).not.toHaveBeenCalled()
    expect(wrapper.findAll('.vr-row')).toHaveLength(1)
    wrapper.unmount()
  })

  it('confirms a flat project by the note, since nothing else travels with it', async () => {
    // `projects/<name>.md` has no folder of its own, so completing it moves
    // exactly the file the row names. Asking about a folder there would describe
    // an act that is not happening.
    apiGet.mockResolvedValue({
      candidates: [projectCandidate({
        candidate_id: 'flat123flat123flat123flat',
        path: 'memory-vault/projects/Solo.md',
        completion_moves_folder: false,
      })],
      trashed: [],
    })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const clicked = buttonByText(wrapper, 'Complete').trigger('click')
    await flushPromises()

    const ask = pendingConfirm.value
    expect(ask?.message).toContain('Complete "Solo"?')
    expect(ask?.message).toContain('It moves to projects/completed/')
    expect(ask?.message).not.toContain('whole project folder')

    pendingConfirm.value?.resolve(true)
    await clicked
    await flushPromises()
    wrapper.unmount()
  })

  it('names Complete in the Discuss seed and the chat title for a project', async () => {
    apiGet.mockResolvedValue({
      candidates: [projectCandidate({ signals: ['weak_provenance'] })],
      trashed: [],
    })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()
    const projects = useProjectStore()
    projects.projects = [generalProject()]
    const createChat = vi
      .spyOn(projects, 'createChat')
      .mockResolvedValue({ chat_id: 'c-new' } as ChatInfo)
    vi.spyOn(projects, 'pinFile').mockResolvedValue(undefined)

    await buttonByText(wrapper, 'Discuss').trigger('click')
    await flushPromises()

    const [, title, seed] = createChat.mock.calls[0]
    // The chat is named after the question it opens, and the question is
    // whether the project finished, not whether it should be thrown away.
    expect(title).toBe('Complete Faraman-Calendar?')
    expect(seed).toContain('whether this project has actually finished')
    // Only the buttons the row carries. This row has no Retire, so a seed
    // naming one is weighing an option the user cannot pick.
    expect(seed).toContain('I will pick Still true or Complete myself')
    expect(seed).not.toContain('Retire')
    // And it is still a draft about a note the user has not moved.
    expect(seed).toContain('Do not edit, move, or delete anything')
    wrapper.unmount()
  })

  it('keeps an unlinked project seed on Complete, in the link-hunting branch too', async () => {
    // The unlinked branch is a different paragraph, and it is where the two
    // findings landed: it named Retire in the closing line AND told the agent
    // that nothing linking to a project was "an argument for retiring it" —
    // aiming the answer at a verdict the row cannot deliver.
    apiGet.mockResolvedValue({ candidates: [projectCandidate()], trashed: [] })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()
    const projects = useProjectStore()
    projects.projects = [generalProject()]
    const createChat = vi
      .spyOn(projects, 'createChat')
      .mockResolvedValue({ chat_id: 'c-new' } as ChatInfo)
    vi.spyOn(projects, 'pinFile').mockResolvedValue(undefined)

    await buttonByText(wrapper, 'Discuss').trigger('click')
    await flushPromises()

    const [, title, seed] = createChat.mock.calls[0]
    expect(title).toBe('Link or complete Faraman-Calendar?')
    expect(seed).toContain('search the vault for the notes that should link to it')
    expect(seed).toContain('that is an argument for completing it')
    expect(seed).toContain('I will pick Still true or Complete myself')
    // The row has no Retire button, so the seed must not offer one — not in
    // the closing line and not as the fallback verdict.
    expect(seed).not.toContain('Retire')
    // Still read-only about the note under review, and the approval gate
    // still leads: neither may be lost to the reworded tail.
    expect(seed).toContain('Do not edit, move, or delete the note itself')
    expect((seed as string).split('\n\n').at(-1)).toMatch(/^Write nothing until I say so\./)
    wrapper.unmount()
  })

  it('keeps the retire phrasing for a non-project Discuss seed', async () => {
    apiGet.mockResolvedValue({ candidates: [candidate()], trashed: [] })
    const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()
    const projects = useProjectStore()
    projects.projects = [generalProject()]
    const createChat = vi
      .spyOn(projects, 'createChat')
      .mockResolvedValue({ chat_id: 'c-new' } as ChatInfo)
    vi.spyOn(projects, 'pinFile').mockResolvedValue(undefined)

    await buttonByText(wrapper, 'Discuss').trigger('click')
    await flushPromises()

    const [, title, seed] = createChat.mock.calls[0]
    expect(title).toBe('Link or retire Mo?')
    expect(seed).toContain('I will pick Still true or Retire myself')
    expect(seed).toContain('that is an argument for retiring it')
    expect(seed).not.toContain('Complete')
    wrapper.unmount()
  })
})

// ── A note the verification pass is holding for a person ───────────────────
//
// The queue used to offer a second, independent Still true / Retire on the very
// revision a proposal was already filed about. One question, asked in two
// places, with the two answers free to disagree.

function check(overrides: Record<string, unknown> = {}) {
  return {
    outcome: 'retire',
    checked_at: '2026-09-28',
    retry_after: '2026-10-28',
    coverage: 'complete',
    reason: 'the office moved to a new site',
    citations: 2,
    receipt_id: '',
    revision: 'rev-1',
    pending: true,
    conflicted: false,
    proposal_id: 'p-9',
    ...overrides,
  }
}

function pendingCandidate(overrides: Partial<VaultReviewCandidate> = {}): VaultReviewCandidate {
  const state = check(overrides.evidence?.verification ? {} : {})
  return candidate({
    signals: ['unverified'],
    retirement_offered: false,
    evidence: { ...candidate().evidence, verification: state as never },
    pending_verification: {
      proposal_id: 'p-9',
      note_edit_id: 'side-9',
      outcome: state.outcome,
      checked_at: state.checked_at,
      retry_after: state.retry_after,
      coverage: state.coverage,
      reason: state.reason,
      citations: state.citations,
    },
    ...overrides,
  })
}

/** A note checked and settled with nothing pending — the "asked, and here is
 * the answer" state, which is not the same as "never looked at". */
function checkedCandidate(overrides: Partial<VaultReviewCandidate> = {}): VaultReviewCandidate {
  return candidate({
    signals: ['unlinked'],
    evidence: {
      ...candidate().evidence,
      unverified: { age_days: 640, threshold_days: 180, last_verified: '2024-11-02', source: 'frontmatter' },
      verification: check({ outcome: 'unverified', pending: false, proposal_id: '', citations: 0, reason: 'no source this pass reached could support it' }) as never,
    },
    ...overrides,
  })
}

async function mountWithRouter(c: VaultReviewCandidate, pinia: ReturnType<typeof createPinia>) {
  const { createMemoryHistory, createRouter } = await import('vue-router')
  apiGet.mockResolvedValue({ candidates: [c], trashed: [] })
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/memory/:section', component: { template: '<div />' } },
    ],
  })
  await router.push('/')
  await router.isReady()
  const wrapper = mount(VaultReviewPanel, { global: { plugins: [pinia, router] } })
  await flushPromises()
  return { wrapper, router }
}

describe('a pending verification proposal', () => {
  let pinia: ReturnType<typeof createPinia>

  beforeEach(() => {
    pinia = createPinia()
    setActivePinia(pinia)
    apiGet.mockReset()
    apiPost.mockReset()
    apiPost.mockResolvedValue({ ok: true })
  })

  afterEach(() => {
    document.body.innerHTML = ''
    pendingConfirm.value?.resolve(false)
    vi.restoreAllMocks()
  })

  it('links the proposal instead of asking the same question again', async () => {
    const { wrapper } = await mountWithRouter(pendingCandidate(), pinia)

    const text = wrapper.get('.vr-pending').text()
    expect(text).toContain('Verification proposal pending')
    expect(text).toContain('2026-09-28')
    expect(text).toContain('the note should be retired')
    expect(text).toContain('covering the whole note')
    expect(text).toContain('2 citations')
    // The reason is the pass's own, quoted: it is why the question exists.
    expect(text).toContain('the office moved to a new site')
    // The note stays in the list; the row says the decision lives elsewhere.
    expect(wrapper.find('.vr-row').exists()).toBe(true)
    expect(wrapper.get('.vr-pending-hint').text())
      .toContain('The decision is on the proposal, not here')
    wrapper.unmount()
  })

  it('withdraws the queue’s own Still true and Retire on that revision', async () => {
    const { wrapper } = await mountWithRouter(pendingCandidate(), pinia)

    // Only Discuss. Still true would stamp the note "verified today" — a claim
    // about text the pass has already said is wrong.
    expect(wrapper.findAll('.vr-actions button').map(b => b.text())).toEqual(['Discuss'])
    wrapper.unmount()
  })

  it('keeps Retire when another signal is an independent finding', async () => {
    // Unlinked is a real reason to retire the note, and the pass reaching the
    // same note first must not take the queue's own strongest signal away.
    const { wrapper } = await mountWithRouter(pendingCandidate({
      signals: ['unlinked', 'unverified'],
      retirement_offered: true,
    }), pinia)

    expect(wrapper.findAll('.vr-actions button').map(b => b.text())).toEqual(['Retire', 'Discuss'])
    expect(wrapper.find('.vr-pending').exists()).toBe(true)
    wrapper.unmount()
  })

  it('says a partial check covered part of the note, not all of it', async () => {
    // A re-stamp claims the WHOLE note is still true, so the pass only applies
    // one from complete coverage. "Checked" alone would overclaim.
    const { wrapper } = await mountWithRouter(pendingCandidate({
      evidence: { ...pendingCandidate().evidence, verification: check({ coverage: 'partial' }) as never },
      pending_verification: { ...pendingCandidate().pending_verification!, coverage: 'partial' },
    }), pinia)

    expect(wrapper.get('.vr-pending').text()).toContain('covering part of the note')
    wrapper.unmount()
  })

  it('navigates to the queue and names the row when the link is followed', async () => {
    const { wrapper, router } = await mountWithRouter(pendingCandidate(), pinia)

    await wrapper.get('.vr-pending-link').trigger('click')
    await flushPromises()

    expect(useProposalsStore().revealedRowId).toBe('p-9')
    expect(router.currentRoute.value.fullPath).toContain('show=suggested')
    wrapper.unmount()
  })

  it('clears a filter that would have hidden the linked row', async () => {
    const { wrapper } = await mountWithRouter(pendingCandidate(), pinia)
    const store = useProposalsStore()
    store.kindFilter = 'skill'
    store.search = 'nothing matches this'

    await wrapper.get('.vr-pending-link').trigger('click')
    await flushPromises()

    // A link that lands on a list the row is not in is the one thing it must
    // not do, and the kind chip and search box are the two things that hide it.
    expect(store.kindFilter).toBe('all')
    expect(store.search).toBe('')
    wrapper.unmount()
  })
})

describe('a settled check with nothing pending', () => {
  let pinia: ReturnType<typeof createPinia>

  beforeEach(() => {
    pinia = createPinia()
    setActivePinia(pinia)
    apiGet.mockReset()
    apiPost.mockReset()
    apiPost.mockResolvedValue({ ok: true })
  })

  afterEach(() => {
    document.body.innerHTML = ''
    pendingConfirm.value?.resolve(false)
    vi.restoreAllMocks()
  })

  it('reports the verdict inside the unchecked evidence box', async () => {
    // A verdict that came back unverified writes nothing, so the note keeps an
    // old `updated:` while the check is recent. Both dates are shown, because
    // one date for two claims reads as a contradiction rather than a verdict.
    const row = checkedCandidate({ signals: ['unlinked', 'unverified'] })
    const { wrapper } = await mountWithRouter(row, pinia)

    const box = wrapper.get('.vr-evidence--unverified').text()
    expect(box).toContain('2024-11-02')
    expect(box).toContain('2026-09-28')
    expect(box).toContain('could not be confirmed')
    expect(box).toContain('no source this pass reached could support it')
    // No proposal, so the row's own actions are untouched.
    expect(wrapper.find('.vr-pending').exists()).toBe(false)
    expect(wrapper.findAll('.vr-actions button').map(b => b.text()))
      .toEqual(['Still true', 'Retire', 'Discuss'])
    wrapper.unmount()
  })

  it('says a proposal pinned to an older revision is dead, and acts', async () => {
    // Its accept would refuse as a conflict, so withdrawing this row would leave
    // the note with nobody asking about it: the proposal cannot be applied and
    // the queue had stepped aside.
    const { wrapper } = await mountWithRouter(candidate({
      signals: ['unverified'],
      evidence: {
        ...candidate().evidence,
        verification: check({ pending: false, conflicted: true }) as never,
      },
    }), pinia)

    expect(wrapper.get('.vr-conflict').text())
      .toContain('filed for an earlier version of this note')
    expect(wrapper.findAll('.vr-actions button').map(b => b.text()))
      .toEqual(['Still true', 'Retire', 'Discuss'])
    wrapper.unmount()
  })
})
