// @vitest-environment jsdom

/**
 * Memory → Import conversations: the whole journey (#1029, C5; completed by
 * #1041, C8).
 *
 * The panel's job is consent, so what is asserted is the consent surface rather
 * than the rendering: the five load states stay distinct (a failed or pending
 * GET must never read as "there is nothing here"), Ciaobot's own and unreadable
 * rows are shown with their reason and cannot be selected, nothing is selected
 * for the reader, every control clears the 44px touch minimum, and the
 * confirmation states what would be processed — the rows, the provider/model
 * that would receive the text, an estimate and the batch cap — before anything
 * runs.
 *
 * C8's half is asserted the same way: the confirmation is the only thing that can
 * file a batch, filing is the only thing the panel writes, the extraction is
 * started from the runs list and not from the consent screen, a cancel keeps
 * what was filed and says it cannot unsend provider input, the retention copy
 * is the store's own window rather than a number typed here, and the five run
 * states cannot be confused with each other or with "there is nothing here".
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount, RouterLinkStub } from '@vue/test-utils'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import ImportSources from '../ImportSources.vue'
import { useProjectStore } from '../../stores/projects'

const apiGet = vi.fn()
const apiPost = vi.fn()
const apiDel = vi.fn()

vi.mock('../../lib/api', () => ({
  api: {
    get: (...args: unknown[]) => apiGet(...args),
    post: (...args: unknown[]) => apiPost(...args),
    patch: vi.fn(),
    del: (...args: unknown[]) => apiDel(...args),
  },
}))

function source(id: string, provider = 'claude_code'): Record<string, unknown> {
  // No `path`: the engine does not send one to the browser (nothing here shows
  // it), so a row is an id and its weakest locator.
  return { provider, source_id: id, project_hint: '-tmp-workspace' }
}

function listing(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    available: [source('sess-a'), source('sess-b')],
    excluded: [{ ref: source('chat-deadbeef'), reason: 'ciaobot_own', message: 'Created or driven by Ciaobot; it is not your own history.' }],
    unsupported: [{ provider: 'opencode', reason: 'unsupported_version', message: 'Unsupported version, export a file instead.' }],
    truncated: { claude_code: false, opencode: false },
    ...over,
  }
}

const EMPTY_RUNS = { workspace: 'personal', batches: [], retention_days: 30 }

function previewPayload(): Record<string, unknown> {
  return {
    preview: {
      workspace: 'personal',
      conversations: [
        {
          source: source('sess-a'), state: 'ready', classification: 'external',
          message_count: 12, estimated_chars: 4800, first_date: '2026-02-01',
          omitted: { isSidechain: 3 }, already_imported: false, reason: '', message: '',
        },
      ],
      provider: 'claude', model: 'sonnet', estimated_chars: 4800, estimated_messages: 12,
      batch_cap: 10, destination: '/tmp/p/memory-vault/personal',
    },
  }
}

/** One batch as C6 stores it, at the status the test needs. */
function batch(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    batch_id: 'batch-1',
    workspace: 'personal',
    destination: '/tmp/p/memory-vault/personal',
    status: 'queued',
    extraction_revision: 1,
    sources: [{ provider: 'claude_code', source_id: 'sess-a', content_digest: '', status: 'pending' }],
    progress: {
      total_sources: 2, completed_sources: 0, proposals_filed: 0,
      skipped: 0, current_source_id: '',
    },
    provenance: [],
    created_at: '2026-10-04T09:00:00+00:00',
    updated_at: '2026-10-04T09:00:00+00:00',
    error: '',
    ...over,
  }
}

/** The list read the runs section makes on mount, and every read after it. */
function runList(...batches: Record<string, unknown>[]) {
  return { workspace: 'personal', batches, retention_days: 30 }
}

async function mountPanel() {
  const wrapper = mount(ImportSources, {
    attachTo: document.body,
    global: { stubs: { RouterLink: RouterLinkStub } },
  })
  await flushPromises()
  return wrapper
}

/** Click Find and settle. */
async function find(wrapper: Awaited<ReturnType<typeof mountPanel>>) {
  await wrapper.get('.import-head button.btn-primary').trigger('click')
  await flushPromises()
}

/**
 * The `beforeEach` answer, kept so a test can answer the run list from it while
 * replacing the scan. Non-optional because `beforeEach` always sets it, and a
 * type that admits `undefined` here would only push the check into every call.
 */
const RUNS_READ = async (_path?: string): Promise<unknown> => EMPTY_RUNS

describe('ImportSources', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    const store = useProjectStore()
    store.workspaces = [
      { name: 'personal', vault_root: '/tmp/p', default_provider: 'claude', gws_profile: '' },
      { name: 'work', vault_root: '/tmp/w', default_provider: 'claude', gws_profile: '' },
    ]
    store.activeWorkspace = 'personal'
    apiGet.mockReset().mockImplementation(async (path: string) => (
      String(path).startsWith('/api/import/batches') ? EMPTY_RUNS : { workspace: 'personal', sources: listing() }
    ))
    apiPost.mockReset().mockImplementation(async (path: string) => (
      String(path) === '/api/import/preview' ? previewPayload() : { batch: batch() }
    ))
    apiDel.mockReset().mockResolvedValue(undefined)
  })

  afterEach(() => {
    document.body.innerHTML = ''
    vi.restoreAllMocks()
  })

  it('lists nothing until the reader asks: discovery is opt-in', async () => {
    const wrapper = await mountPanel()

    // The runs list is Ciaobot's own state and is read on mount — the point is
    // that the *scan* is the reader's decision, so the assertion is about the
    // sources route and nothing else.
    expect(apiGet.mock.calls.map(([path]) => path)).not.toContain('/api/import/sources?workspace=personal')
    expect(wrapper.text()).toContain('Import conversations')
    expect(wrapper.findAll('input[type="checkbox"]')).toHaveLength(0)

    await find(wrapper)
    expect(apiGet).toHaveBeenCalledWith('/api/import/sources?workspace=personal')
    expect(wrapper.findAll('input[type="checkbox"]')).toHaveLength(2)
  })

  it('says the first listing is in flight, and never that there is nothing', async () => {
    const runs = RUNS_READ
    let release: (value: unknown) => void = () => {}
    apiGet.mockImplementation(async (path: string) => {
      if (String(path).startsWith('/api/import/batches')) return runs(path)
      return new Promise(resolve => { release = resolve })
    })
    const wrapper = await mountPanel()

    await wrapper.get('.import-head button.btn-primary').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Looking for conversations')
    // Not an empty-state claim while the request is still open.
    expect(wrapper.text()).not.toContain('No past conversations found')

    release({ workspace: 'personal', sources: listing({ available: [], excluded: [], unsupported: [] }) })
    await flushPromises()
    expect(wrapper.text()).toContain('No past conversations found')
  })

  it('a failed first listing is an error with a Retry, not an empty list', async () => {
    const runs = RUNS_READ
    apiGet.mockImplementation(async (path: string) => {
      if (String(path).startsWith('/api/import/batches')) return runs(path)
      throw new Error('cannot list that directory')
    })
    const wrapper = await mountPanel()

    await find(wrapper)
    expect(wrapper.text()).toContain('cannot list that directory')
    expect(wrapper.text()).not.toContain('No past conversations found')

    await wrapper.get('.import-head button.btn-primary').trigger('click')
    await flushPromises()
    const scans = apiGet.mock.calls.filter(([path]) => String(path).startsWith('/api/import/sources'))
    expect(scans).toHaveLength(2)
  })

  it('a failed refresh keeps the rows on screen and marks them stale', async () => {
    const wrapper = await mountPanel()
    await find(wrapper)
    expect(wrapper.text()).toContain('sess-a')

    const runs = RUNS_READ
    apiGet.mockImplementationOnce(async () => { throw new Error('the engine went away') })
    await wrapper.get('.import-head button.btn-primary').trigger('click')
    await flushPromises()
    apiGet.mockImplementation(async (path: string) => {
      if (String(path).startsWith('/api/import/batches')) return runs(path)
      return { workspace: 'personal', sources: listing() }
    })

    // The rows are still there, and the failure is stated beside them: a
    // refresh that cleared the list would claim the conversations are gone.
    expect(wrapper.text()).toContain('sess-a')
    expect(wrapper.text()).toContain('the engine went away')
    expect(wrapper.text()).toContain('Retry')
  })

  it('shows excluded and unsupported rows with their reason, and cannot select them', async () => {
    const wrapper = await mountPanel()
    await find(wrapper)

    expect(wrapper.text()).toContain('Created or driven by Ciaobot')
    expect(wrapper.text()).toContain('Unsupported version, export a file instead')
    // The only checkboxes are the two offered conversations: an excluded row and
    // an unsupported source are drawn and read, never ticked.
    expect(wrapper.findAll('input[type="checkbox"]')).toHaveLength(2)
    const aside = wrapper.findAll('details')
    expect(aside).toHaveLength(2)
    expect(aside[0].findAll('input')).toHaveLength(0)
    expect(aside[1].findAll('input')).toHaveLength(0)
    expect(aside[0].text()).toContain('Not importable')
    expect(aside[1].text()).toContain('Sources unavailable')
  })

  it('never selects anything for the reader', async () => {
    const wrapper = await mountPanel()
    await find(wrapper)

    expect(wrapper.findAll('input[type="checkbox"]:checked')).toHaveLength(0)
    expect(wrapper.get('.import-actions button.btn-primary').attributes('disabled')).toBeDefined()

    await wrapper.findAll('input[type="checkbox"]')[0].setValue(true)
    await flushPromises()
    expect(wrapper.findAll('input[type="checkbox"]:checked')).toHaveLength(1)
    expect(wrapper.text()).toContain('Review 1 selected')
  })

  it('drops a listing that answers after the workspace moved, and leaves Find usable', async () => {
    const runs = RUNS_READ
    let release: (value: unknown) => void = () => {}
    apiGet.mockImplementation(async (path: string) => {
      if (String(path).startsWith('/api/import/batches')) return runs(path)
      return new Promise(resolve => { release = resolve })
    })
    const wrapper = await mountPanel()

    await wrapper.get('.import-head button.btn-primary').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Looking for conversations')

    // The `1`–`9` shortcuts move the pane while the scan is open.
    useProjectStore().activeWorkspace = 'work'
    await flushPromises()

    release({ workspace: 'personal', sources: listing() })
    await flushPromises()

    // `personal`'s rows never appear under `work`. They would be checkable, and
    // filing them sends `workspace=work` with another workspace's conversation
    // ids — a create checks that the ids are Ciaobot's own, not whose history
    // they name.
    expect(wrapper.text()).not.toContain('sess-a')
    expect(wrapper.findAll('input[type="checkbox"]')).toHaveLength(0)
    // And Find is not held behind a request this workspace never made.
    const findAgain = wrapper.get('.import-head button.btn-primary')
    expect(findAgain.attributes('disabled')).toBeUndefined()
    expect(findAgain.text()).toContain('Find conversations')
    expect(wrapper.text()).not.toContain('Looking for conversations')
    wrapper.unmount()
  })

  it('drops a consent preview that answers after the workspace moved', async () => {
    const runs = RUNS_READ
    let release: (value: unknown) => void = () => {}
    apiGet.mockImplementation(async (path: string) => {
      if (String(path).startsWith('/api/import/batches')) return runs(path)
      if (String(path).includes('workspace=work')) {
        return { workspace: 'work', sources: listing({ available: [source('sess-work', 'opencode')] }) }
      }
      return { workspace: 'personal', sources: listing() }
    })
    apiPost.mockImplementation(async (path: string) => {
      if (String(path) !== '/api/import/preview') return { batch: batch() }
      return new Promise(resolve => { release = resolve })
    })
    const wrapper = await mountPanel()
    await find(wrapper)
    await wrapper.findAll('input[type="checkbox"]')[0].setValue(true)
    await wrapper.get('.import-actions button.btn-primary').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Reading…')

    useProjectStore().activeWorkspace = 'work'
    await flushPromises()
    // The new workspace scans and lists its own rows.
    await find(wrapper)
    expect(wrapper.text()).toContain('sess-work')

    release(previewPayload())
    await flushPromises()

    // The confirmation is one workspace's consent over one workspace's rows, with
    // that workspace's model and destination on it. A late answer must not put
    // `personal`'s numbers in `work`'s panel, where the one button under them
    // files with `workspace=work`.
    expect(wrapper.find('.import-confirm').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('Before anything runs')
    expect(wrapper.text()).not.toContain('/tmp/p/memory-vault/personal')
    expect(wrapper.text()).not.toContain('Reading…')
    wrapper.unmount()
  })

  it('states what would be processed, on whose account, before anything runs', async () => {
    const wrapper = await mountPanel()
    await find(wrapper)
    await wrapper.findAll('input[type="checkbox"]')[0].setValue(true)
    await wrapper.get('.import-actions button.btn-primary').trigger('click')
    await flushPromises()

    // Only the selected refs are sent, as ids.
    expect(apiPost).toHaveBeenCalledWith('/api/import/preview', {
      workspace: 'personal',
      sources: [{ provider: 'claude_code', source_id: 'sess-a' }],
    })

    const text = wrapper.text()
    expect(text).toContain('Before anything runs')
    expect(text).toContain('12 turns')
    expect(text).toContain('2026-02-01')
    expect(text).toContain('isSidechain ×3')
    expect(text).toContain('claude / sonnet')
    expect(text).toContain('4,800')
    expect(text).toContain('at most 10 conversations per batch')
    // The destination, and a cancel.
    expect(text).toContain('/tmp/p/memory-vault/personal')
    expect(wrapper.text()).toContain('Cancel')
    // Nothing is read yet: the only thing the confirmation can do is file the
    // batch, and the extraction is started from the runs list, not from here.
    expect(apiPost).not.toHaveBeenCalledWith(
      expect.stringContaining('/run'),
      expect.anything(),
    )
    expect(wrapper.findAll('.import-confirm-actions button.btn-primary')).toHaveLength(1)
    expect(wrapper.get('.import-confirm-actions button.btn-primary').text()).toMatch(/^File an import of 1 conversation$/)
  })

  it('does not promise the preview model is the one that will read the conversation', async () => {
    // C7 resolves the model from the per-provider insights configuration, which
    // is not the workspace default the preview reports. The screen may list that
    // default — it is the effective answer for the workspace's own provider — but
    // it must not present it as what will read each selected conversation.
    const wrapper = await mountPanel()
    await find(wrapper)
    await wrapper.findAll('input[type="checkbox"]')[0].setValue(true)
    await wrapper.get('.import-actions button.btn-primary').trigger('click')
    await flushPromises()

    const confirm = wrapper.get('.import-confirm').text()
    expect(confirm).toContain('the model Ciaobot uses for its own insights')
    expect(confirm).toContain('can differ from the workspace default')
    // And nothing is a memory write yet: every fact waits as a proposal.
    expect(confirm).toContain('the import writes nothing into your memory by itself')
  })

  it('files a batch over the processable rows only, and never from an empty consent', async () => {
    const wrapper = await mountPanel()
    await find(wrapper)
    await wrapper.findAll('input[type="checkbox"]')[0].setValue(true)
    await wrapper.get('.import-actions button.btn-primary').trigger('click')
    await flushPromises()

    await wrapper.get('.import-confirm-actions button.btn-primary').trigger('click')
    await flushPromises()

    // Ids, never paths, and only the rows the preview called ready.
    expect(apiPost).toHaveBeenCalledWith('/api/import/batches', {
      workspace: 'personal',
      sources: [{ provider: 'claude_code', source_id: 'sess-a' }],
    })
    // The consent screen is gone, so it cannot be filed twice.
    expect(wrapper.find('.import-confirm').exists()).toBe(false)
    wrapper.unmount()
  })

  it('keeps a refused create beside the button instead of clearing the consent', async () => {
    apiPost.mockImplementation(async (path: string) => {
      if (String(path) === '/api/import/preview') return previewPayload()
      throw new Error('another import is still open for personal')
    })
    const wrapper = await mountPanel()
    await find(wrapper)
    await wrapper.findAll('input[type="checkbox"]')[0].setValue(true)
    await wrapper.get('.import-actions button.btn-primary').trigger('click')
    await flushPromises()

    await wrapper.get('.import-confirm-actions button.btn-primary').trigger('click')
    await flushPromises()

    // The server's own sentence, next to the button, and the confirmation the
    // reader was looking at is still there to try again from.
    expect(wrapper.get('.import-confirm').text()).toContain('another import is still open for personal')
    expect(wrapper.find('.import-confirm').exists()).toBe(true)
    // The runs list must not print the same sentence a second time.
    expect(wrapper.get('.import-runs').text()).not.toContain('another import is still open')
    wrapper.unmount()
  })

  it('runs, watches and cancels: cancel keeps what was filed and cannot unsend input', async () => {
    const running = batch({
      status: 'running',
      progress: {
        total_sources: 2, completed_sources: 1, proposals_filed: 3,
        skipped: 0, current_source_id: 'sess-b',
      },
    })
    const cancelled = batch({
      status: 'cancelled',
      progress: {
        total_sources: 2, completed_sources: 1, proposals_filed: 3,
        skipped: 0, current_source_id: '',
      },
      provenance: [
        { provider: 'claude_code', source_id: 'sess-a', anchor: 'm-1', destination: 'personal', accepted: false, note: 'filed as a review proposal' },
      ],
      updated_at: '2026-10-04T09:05:00+00:00',
    })
    apiPost.mockImplementation(async (path: string) => {
      if (String(path) === '/api/import/preview') return previewPayload()
      if (String(path).endsWith('/run')) return { batch: running }
      if (String(path).endsWith('/cancel')) return { batch: cancelled }
      return { batch: batch() }
    })
    const wrapper = await mountPanel()
    await find(wrapper)
    await wrapper.findAll('input[type="checkbox"]')[0].setValue(true)
    await wrapper.get('.import-actions button.btn-primary').trigger('click')
    await flushPromises()
    await wrapper.get('.import-confirm-actions button.btn-primary').trigger('click')
    await flushPromises()

    // Filed and waiting: nothing extracted yet, so the only forward action is the
    // run, and it is here rather than on the consent screen.
    let runs = wrapper.get('.import-runs')
    expect(runs.text()).toContain('Filed, waiting to start')
    expect(runs.text()).toContain('Nothing starts on its own')
    // The note beside Cancel is per status: a queued batch has sent nothing, so
    // warning that provider input cannot be unsent describes a loss that has not
    // happened and makes a still-free cancel sound destructive.
    expect(runs.text()).toContain('Nothing has been read or sent yet')
    expect(runs.text()).not.toContain('cannot be unsent')

    await runs.get('.import-run-actions button.btn-primary').trigger('click')
    await flushPromises()

    // The run route takes the workspace and nothing else: no model, no provider,
    // no selection — those are the configuration's answer, not the request's.
    expect(apiPost).toHaveBeenCalledWith('/api/import/batches/batch-1/run', { workspace: 'personal' })
    runs = wrapper.get('.import-runs')
    expect(runs.text()).toContain('Running')
    expect(runs.text()).toContain('Reading session sess-b')
    expect(runs.get('progress').attributes('value')).toBe('1')
    expect(runs.text()).toContain('1 of 2 conversations read')
    expect(runs.text()).toContain('3 proposals filed')

    await runs.get('.import-run-actions button.import-quiet').trigger('click')
    await flushPromises()

    expect(apiPost).toHaveBeenCalledWith('/api/import/batches/batch-1/cancel', { workspace: 'personal' })
    runs = wrapper.get('.import-runs')
    const text = runs.text()
    expect(text).toContain('Cancelled after 1 of 2 conversations')
    // The two halves of what a cancel means, both said out loud.
    expect(text).toContain('3 proposals stay queued for you to decide')
    expect(text).toContain('text already sent to the configured provider cannot be unsent')
    // The evidence the run recorded, and where a date is read.
    expect(text).toContain('What this filed (1)')
    expect(text).toContain('Claude Code · session sess-a')
    expect(text).toContain('message m-1 — waiting for review')
    expect(wrapper.getComponent(RouterLinkStub).props('to')).toBe('/memory/review')
    wrapper.unmount()
  })

  it('states the retention the store enforces, and says removing keeps memories', async () => {
    const wrapper = await mountPanel()
    await flushPromises()
    const text = wrapper.get('.import-runs').text()

    // The window is the server's own number, not one typed into the PWA.
    expect(text).toContain('30 days after the import settles')
    // About that long, and no longer: the sweep drops any settled batch past the
    // window whatever became of its proposals, and only looks when Ciaobot
    // starts. A sentence tying the window to the reader's indecision would
    // promise an undecided import a longer life than the store gives it.
    expect(text).toContain('checked when Ciaobot starts')
    expect(text).not.toContain('if you never decided')
    expect(text).toContain('Facts you accepted keep a short record')
    expect(text).toContain('Removing an import only removes this record')
    expect(text).toContain('a fact you accepted stays in your memory')
    expect(text).toContain('never modified or deleted')
    wrapper.unmount()
  })

  it('says so when a listing is one page rather than the whole history', async () => {
    apiGet.mockImplementation(async (path: string) => (
      String(path).startsWith('/api/import/batches')
        ? EMPTY_RUNS
        : { workspace: 'personal', sources: listing({ truncated: { claude_code: false, opencode: true } }) }
    ))
    const wrapper = await mountPanel()

    await find(wrapper)

    expect(wrapper.text()).toContain('opencode lists one page of conversations')
  })

  it('drops the listing on a workspace switch, so a selection cannot outlive it', async () => {
    const wrapper = await mountPanel()
    await find(wrapper)
    await wrapper.findAll('input[type="checkbox"]')[0].setValue(true)
    expect(wrapper.findAll('input[type="checkbox"]:checked')).toHaveLength(1)

    useProjectStore().activeWorkspace = 'work'
    await flushPromises()

    expect(wrapper.findAll('input[type="checkbox"]')).toHaveLength(0)
    wrapper.unmount()
  })

  it('keeps the controls reachable after a long listing', async () => {
    // 60 rows is an ordinary listing (the engine's cap is 500) and it is what
    // made the panel unusable when it was a card on the map: the list pushed
    // Review and Cancel thousands of pixels down a pane that does not scroll.
    // jsdom has no layout engine, so what it can establish is that the markup is
    // complete — every row rendered, and the controls still there and enabled
    // after a selection — and the pane that scrolls is pinned in
    // MemoryMapView.test.ts. The geometry is pinned by the browser spec
    // (e2e/specs/import-list.spec.ts), the only place a rect is real.
    const rows = Array.from({ length: 60 }, (_, index) => source(`sess-${index}`))
    apiGet.mockImplementation(async (path: string) => (
      String(path).startsWith('/api/import/batches')
        ? EMPTY_RUNS
        : { workspace: 'personal', sources: listing({ available: rows }) }
    ))
    const wrapper = await mountPanel()
    await find(wrapper)

    // The offered list, not the two disclosure lists below it.
    expect(wrapper.findAll('ul.import-list')[0]!.findAll('.import-row')).toHaveLength(60)

    await wrapper.findAll('.import-check input[type="checkbox"]')[59].setValue(true)
    await flushPromises()
    const review = wrapper.get('.import-actions button.btn-primary')
    expect(review.text()).toBe('Review 1 selected')
    expect(review.attributes('disabled')).toBeUndefined()
    expect(wrapper.find('.import-actions button.import-quiet').exists()).toBe(true)

    await review.trigger('click')
    await flushPromises()
    // The confirmation the reader has to reach is rendered, with the row that
    // was ticked at the end of the list.
    expect(wrapper.text()).toContain('Before anything runs')
    expect(apiPost).toHaveBeenCalledWith('/api/import/preview', {
      workspace: 'personal',
      sources: [{ provider: 'claude_code', source_id: 'sess-59' }],
    })
    wrapper.unmount()
  })

  it('keeps every control native and on the shared 44px touch floor', async () => {
    const wrapper = await mountPanel()
    await find(wrapper)
    await wrapper.findAll('input[type="checkbox"]')[0].setValue(true)
    await flushPromises()

    // Native controls only: a pointer-only affordance would be unreachable by
    // keyboard, and the row (not the 13px box) is the hit target.
    expect(wrapper.findAll('.import-check input[type="checkbox"]')).toHaveLength(2)
    expect(wrapper.findAll('.import-row--off input')).toHaveLength(0)
    expect(wrapper.findAll('summary')).toHaveLength(2)
    expect(wrapper.findAll('button').length).toBeGreaterThanOrEqual(2)

    // jsdom has no layout engine (every rect is 0x0), so the floor is asserted
    // from the rule the components ship rather than from a measured height — and
    // from the shared token, so it cannot drift from App.vue's 44px. Both halves
    // of the journey read the same file, which is the point of the file: a row, a
    // disclosure and a quiet button must measure the same on either side of the
    // "file this batch" line.
    const here = join(__dirname, '..')
    const css = readFileSync(join(here, 'importSources.css'), 'utf8')
    expect(css).toMatch(/\.import-check\s*\{[^}]*min-height:\s*var\(--touch\)/)
    expect(css).toMatch(/\.import-quiet\s*\{[^}]*min-height:\s*var\(--touch\)/)
    expect(css).toMatch(/\.import-aside-title\s*\{[^}]*min-height:\s*var\(--touch\)/)
    for (const name of ['ImportSources.vue', 'ImportRuns.vue']) {
      const source = readFileSync(join(here, name), 'utf8')
      expect(source).toContain('<style scoped src="./importSources.css">')
    }
    // The runs list's own chips and its review link are on the floor too.
    const runs = readFileSync(join(here, 'ImportRuns.vue'), 'utf8')
    expect(runs).toMatch(/\.import-chip\s*\{[^}]*min-height:\s*var\(--touch\)/)
    expect(runs).toMatch(/\.import-run-review\s*\{[^}]*min-height:\s*var\(--touch\)/)
    wrapper.unmount()
  })
})

describe('ImportSources — the run list states', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    useProjectStore().activeWorkspace = 'personal'
    apiDel.mockReset().mockResolvedValue(undefined)
  })

  afterEach(() => {
    document.body.innerHTML = ''
  })

  it('never claims an empty list while the first read is open', async () => {
    apiGet.mockReset().mockReturnValue(new Promise(() => {}))
    apiPost.mockReset()
    const wrapper = await mountPanel()

    const text = wrapper.get('.import-runs').text()
    expect(text).toContain('Reading your import runs')
    expect(text).not.toContain('No imports filed')
    wrapper.unmount()
  })

  it('a failed first read is an error with a Retry, never an empty list', async () => {
    apiGet.mockReset().mockRejectedValue(new Error('the engine went away'))
    apiPost.mockReset()
    const wrapper = await mountPanel()

    expect(wrapper.get('.import-runs').text()).toContain('the engine went away')
    expect(wrapper.get('.import-runs').text()).not.toContain('No imports filed')
    wrapper.unmount()
  })

  it('a failed refresh keeps the runs on screen and marks them stale', async () => {
    apiGet.mockReset().mockResolvedValue(runList(batch()))
    apiPost.mockReset()
    const wrapper = await mountPanel()
    expect(wrapper.get('.import-runs').text()).toContain('Filed, waiting to start')

    apiGet.mockRejectedValueOnce(new Error('the engine went away'))
    await wrapper.get('.import-runs-head-actions button').trigger('click')
    await flushPromises()

    const text = wrapper.get('.import-runs').text()
    expect(text).toContain('Filed, waiting to start')
    expect(text).toContain('These runs may be out of date: the engine went away')
    wrapper.unmount()
  })

  it('shows a running import on arrival, not an empty list', async () => {
    // The reason the run list reads on mount at all: a reader who filed an
    // import, left it running and came back must see it mid-flight. A lazy read
    // would show them "No imports filed for personal yet", which is the lie the
    // load states exist to prevent — and a read here is Ciaobot's own private
    // batch store, which opens no conversation.
    apiGet.mockReset().mockResolvedValue(runList(batch({
      status: 'running',
      updated_at: '2026-10-04T09:01:00+00:00',
      progress: {
        total_sources: 2, completed_sources: 1, proposals_filed: 3,
        skipped: 0, current_source_id: 'sess-b',
      },
    })))
    apiPost.mockReset()
    const wrapper = await mountPanel()

    const text = wrapper.get('.import-runs').text()
    expect(text).toContain('Running')
    expect(text).toContain('Reading session sess-b')
    expect(text).toContain('1 of 2 conversations read')
    expect(text).not.toContain('No imports filed')
    // And no conversation was opened to draw it.
    const read = apiGet.mock.calls.map(([path]) => String(path))
    expect(read.some((url) => url.startsWith('/api/import/sources'))).toBe(false)
    expect(read.some((url) => url.startsWith('/api/import/preview'))).toBe(false)
    wrapper.unmount()
  })

  it('says an empty list is empty, and never that an import was lost', async () => {
    apiGet.mockReset().mockResolvedValue(EMPTY_RUNS)
    apiPost.mockReset()
    const wrapper = await mountPanel()

    expect(wrapper.get('.import-runs').text()).toContain('No imports filed for personal yet')
    wrapper.unmount()
  })

  it('tells a filter that hid everything apart from an empty list', async () => {
    apiGet.mockReset().mockResolvedValue(runList(batch({ status: 'done', updated_at: '2026-10-04T09:10:00+00:00' })))
    apiPost.mockReset()
    const wrapper = await mountPanel()

    const chip = (label: string) =>
      wrapper.findAll('.import-chip').find((c) => c.text() === label)!
    await chip('Still open').trigger('click')
    await flushPromises()

    const text = wrapper.get('.import-runs').text()
    expect(text).toContain('No import matches “Still open”.')
    expect(text).not.toContain('No imports filed for personal yet')
    // And the way back is on the same line.
    await wrapper.findAll('.import-runs .import-status button')[0]!.trigger('click')
    await flushPromises()
    expect(wrapper.get('.import-runs').text()).toContain('Finished')
    wrapper.unmount()
  })

  it('removes a record without touching anything the import produced', async () => {
    apiGet.mockReset().mockResolvedValue(runList(batch({
      status: 'done',
      updated_at: '2026-10-04T09:10:00+00:00',
      progress: { total_sources: 2, completed_sources: 2, proposals_filed: 2, skipped: 0, current_source_id: '' },
    })))
    apiPost.mockReset()
    const wrapper = await mountPanel()

    await wrapper.get('.import-run-actions button.import-quiet').trigger('click')
    await flushPromises()

    expect(apiDel).toHaveBeenCalledWith('/api/import/batches/batch-1?workspace=personal')
    const text = wrapper.get('.import-runs').text()
    expect(text).toContain('No imports filed for personal yet')
    // The retention sentence is what says the memories survive it.
    expect(text).toContain('a fact you accepted stays in your memory')
    wrapper.unmount()
  })
})