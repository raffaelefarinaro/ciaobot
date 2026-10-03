// @vitest-environment jsdom

/**
 * Memory → Import conversations (#1029, C5).
 *
 * The panel's job is consent, so what is asserted is the consent surface rather
 * than the rendering: the five load states stay distinct (a failed or pending
 * GET must never read as "there is nothing here"), Ciaobot's own and unreadable
 * rows are shown with their reason and cannot be selected, nothing is selected
 * for the reader, every control clears the 44px touch minimum, and the
 * confirmation states what would be processed — the rows, the provider/model
 * that would receive the text, an estimate and the batch cap — before anything
 * runs. There is no Start button on purpose: extraction is C7 and the batch
 * store is C6.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import ImportSources from '../ImportSources.vue'
import { useProjectStore } from '../../stores/projects'

const apiGet = vi.fn()
const apiPost = vi.fn()

vi.mock('../../lib/api', () => ({
  api: {
    get: (...args: unknown[]) => apiGet(...args),
    post: (...args: unknown[]) => apiPost(...args),
    patch: vi.fn(),
    del: vi.fn(),
  },
}))

function source(id: string, provider = 'claude_code'): Record<string, unknown> {
  return { provider, source_id: id, project_hint: '-tmp-workspace', path: `/home/u/.claude/projects/-tmp-workspace/${id}.jsonl` }
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

async function mountPanel() {
  const wrapper = mount(ImportSources, { attachTo: document.body })
  await flushPromises()
  return wrapper
}

/** Click Find and settle. */
async function find(wrapper: Awaited<ReturnType<typeof mountPanel>>) {
  await wrapper.get('button.btn-primary').trigger('click')
  await flushPromises()
}

describe('ImportSources', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    const store = useProjectStore()
    store.workspaces = [
      { name: 'personal', vault_root: '/tmp/p', default_provider: 'claude', gws_profile: '' },
      { name: 'work', vault_root: '/tmp/w', default_provider: 'claude', gws_profile: '' },
    ]
    store.activeWorkspace = 'personal'
    apiGet.mockReset().mockResolvedValue({ workspace: 'personal', sources: listing() })
    apiPost.mockReset().mockResolvedValue({
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
    })
  })

  afterEach(() => {
    document.body.innerHTML = ''
    vi.restoreAllMocks()
  })

  it('lists nothing until the reader asks: discovery is opt-in', async () => {
    const wrapper = await mountPanel()

    expect(apiGet).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('Import conversations')
    expect(wrapper.findAll('input[type="checkbox"]')).toHaveLength(0)

    await find(wrapper)
    expect(apiGet).toHaveBeenCalledWith('/api/import/sources?workspace=personal')
    expect(wrapper.findAll('input[type="checkbox"]')).toHaveLength(2)
  })

  it('says the first listing is in flight, and never that there is nothing', async () => {
    let release: (value: unknown) => void = () => {}
    apiGet.mockReturnValue(new Promise(resolve => { release = resolve }))
    const wrapper = await mountPanel()

    await wrapper.get('button.btn-primary').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Looking for conversations')
    // Not an empty-state claim while the request is still open.
    expect(wrapper.text()).not.toContain('No past conversations found')

    release({ workspace: 'personal', sources: listing({ available: [], excluded: [], unsupported: [] }) })
    await flushPromises()
    expect(wrapper.text()).toContain('No past conversations found')
  })

  it('a failed first listing is an error with a Retry, not an empty list', async () => {
    apiGet.mockRejectedValueOnce(new Error('cannot list that directory'))
    const wrapper = await mountPanel()

    await find(wrapper)
    expect(wrapper.text()).toContain('cannot list that directory')
    expect(wrapper.text()).not.toContain('No past conversations found')

    await wrapper.get('button.btn-primary').trigger('click')
    await flushPromises()
    expect(apiGet).toHaveBeenCalledTimes(2)
  })

  it('a failed refresh keeps the rows on screen and marks them stale', async () => {
    const wrapper = await mountPanel()
    await find(wrapper)
    expect(wrapper.text()).toContain('sess-a')

    apiGet.mockRejectedValueOnce(new Error('the engine went away'))
    await wrapper.get('button.btn-primary').trigger('click')
    await flushPromises()

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
    expect(text).toContain('At most 10 conversations per batch')
    // The destination, and a cancel.
    expect(text).toContain('/tmp/p/memory-vault/personal')
    expect(wrapper.text()).toContain('Cancel')
    // No extraction exists yet, so there is nothing to press that would run one.
    expect(wrapper.text()).not.toContain('Start import')
  })

  it('says so when a listing is one page rather than the whole history', async () => {
    apiGet.mockResolvedValue({
      workspace: 'personal',
      sources: listing({ truncated: { claude_code: false, opencode: true } }),
    })
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
    // from the rule the component ships rather than from a measured height — and
    // from the shared token, so it cannot drift from App.vue's 44px.
    const source = readFileSync(join(__dirname, '..', 'ImportSources.vue'), 'utf8')
    const style = source.slice(source.indexOf('<style'))
    expect(style).toMatch(/\.import-check\s*\{[^}]*min-height:\s*var\(--touch\)/)
    expect(style).toMatch(/\.import-quiet\s*\{[^}]*min-height:\s*var\(--touch\)/)
    expect(style).toMatch(/\.import-aside-title\s*\{[^}]*min-height:\s*var\(--touch\)/)
  })
})