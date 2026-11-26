// @vitest-environment jsdom
//
// Settings > Workspace > Overview > "Consent and history", mounted on its own.
// Read-only: the only call it makes is the evidence GET. Pinned: audience is
// shown unknown, no recall or cloud-sync claim, every grant's scope and status,
// history merged newest first, and a stale reply never lands on another workspace.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import SettingsShareAudit from '../settings/SettingsShareAudit.vue'
import { useProjectStore } from '../../stores/projects'
import type { SharedBrainState, ShareEvidenceGrant, ShareEvidenceState } from '../../lib/types'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
vi.mock('../../lib/api', () => ({ api: { get: apiGet, post: apiPost, patch: vi.fn(), delete: vi.fn() } }))

function brain(over: Partial<SharedBrainState> = {}): SharedBrainState {
  return {
    name: 'team-notes', kind: 'git', identity: 'github.com/acme/team-notes', path: '/x', lifetime: 'l',
    sharing_policy: 'review', sharing_generation: 0, sharing_granted_at: '', sharing_grant_pending: false,
    sharing_invalidation_failed: false, origin_changed: false, missing: false, problem: '', branch: 'main',
    head: 'abc', head_date: '', dirty: false, upstream: true, fetched_at: null, last_update: null, ...over,
  }
}

function grant(over: Partial<ShareEvidenceGrant> = {}): ShareEvidenceGrant {
  return {
    grant_id: 'grn_a', type: 'standing', project_id: 'proj-alpha', source_id: 'stream:proj-alpha',
    source_kind: 'stream', source_label: 'Structured decisions · Alpha',
    brain: { name: 'team-notes', identity: 'github.com/acme/team-notes', link_fingerprint: 'f' },
    audience: 'github.com/acme/team-notes', field_mask: ['decision', 'effective_date'],
    producers: ['memory_extraction', 'nightly_curation'], issued_at: '2026-11-25T10:00:00Z', start_revision: 1,
    consent_generation: 1, active: true, linked: true, status: 'active', revoked_at: null, revoked_generation: null,
    withdrawn_at: null, withdrawal_reason: '',
    history: [{ event: 'issued', at: '2026-11-25T10:00:00Z', generation: 1 }], ...over,
  }
}

function evidence(grants: ShareEvidenceGrant[]): ShareEvidenceState {
  return {
    chat_evidence: { available: false, status: 'remote_client' }, disclosure_tokens: {}, workspace: 'personal',
    mode: 'review', fields: ['decision', 'reason', 'effective_date'], producers: ['memory_extraction', 'nightly_curation'],
    brains: [], projects: [], grants,
  }
}

const mounted: VueWrapper[] = []
async function mountView(brains: SharedBrainState[]) {
  const view = mount(SettingsShareAudit, { props: { brains } })
  mounted.push(view)
  await flushPromises()
  return view
}

beforeEach(() => {
  setActivePinia(createPinia())
  useProjectStore().activeWorkspace = 'personal'
  apiGet.mockReset(); apiPost.mockReset()
})
afterEach(() => { while (mounted.length) mounted.pop()!.unmount() })

describe('SettingsShareAudit', () => {
  it('reads only the evidence record and changes nothing', async () => {
    apiGet.mockResolvedValue(evidence([grant()]))
    const view = await mountView([brain()])
    expect(apiGet).toHaveBeenCalledTimes(1)
    expect(apiGet).toHaveBeenCalledWith('/api/workspaces/personal/share-evidence')
    expect(view.findAll('button')).toHaveLength(0)
    expect(view.findAll('input,select,textarea')).toHaveLength(0)
    expect(apiPost).not.toHaveBeenCalled()
  })

  it('shows scope, status and an unknown audience without claiming recall or sync', async () => {
    apiGet.mockResolvedValue(evidence([grant()]))
    const view = await mountView([brain()])
    const text = view.text()
    expect(text).toContain('You review each note before it is shared.')
    expect(text).toContain('Audience unknown')
    expect(text).toContain('Target: github.com/acme/team-notes')
    expect(text).toContain('Future decisions to team-notes')
    expect(text).toContain('Recorded, not yet in use')
    expect(text).not.toMatch(/\bActive\b/)
    expect(text).toContain('decision, effective_date')
    expect(text).toContain('Memory extraction, Nightly curation')
    expect(text).toContain('cannot recall a note once it is published')
    expect(text).not.toMatch(/synced to|recalled|undo/i)
  })

  it('states automatic policy as chosen but not active, for the unverified audience', async () => {
    apiGet.mockResolvedValue(evidence([]))
    const view = await mountView([brain({ sharing_policy: 'automatic', sharing_generation: 2 })])
    expect(view.text()).toContain('is chosen for everyone who can access github.com/acme/team-notes; it is not active yet')
    expect(view.text()).toContain('No project permission is recorded for this brain.')
  })

  it('describes a folder as local files, never as cloud sync', async () => {
    apiGet.mockResolvedValue(evidence([]))
    const view = await mountView([brain({ name: 'local', kind: 'folder', identity: 'folder:/Users/me/local' })])
    expect(view.text()).toContain('A folder is never shared automatically')
    expect(view.text()).toContain('does not confirm that Google Drive, iCloud or another sync service has finished syncing')
    expect(view.text()).not.toContain('cannot recall')
  })

  it('lists revoked and unlinked grants and merges history newest first', async () => {
    const revoked = grant({
      status: 'revoked', active: false, revoked_at: '2026-11-25T12:00:00Z', revoked_generation: 1,
      history: [
        { event: 'issued', at: '2026-11-25T10:00:00Z', generation: 1 },
        { event: 'revoked', at: '2026-11-25T12:00:00Z', generation: 1 },
      ],
    })
    const gone = grant({
      grant_id: 'grn_b', type: 'snapshot', status: 'unlinked', linked: false,
      brain: { name: 'old-notes', identity: 'github.com/acme/old', link_fingerprint: 'g' },
      history: [{ event: 'issued', at: '2026-11-25T11:00:00Z', generation: 1 }],
    })
    apiGet.mockResolvedValue(evidence([revoked, gone]))
    const view = await mountView([brain()])
    expect(view.text()).toContain('Revoked')
    expect(view.text()).toContain('old-notes')
    expect(view.text()).toContain('no longer linked')
    expect(view.text()).toContain('Brain unlinked')
    const items = view.findAll('.audit-event').map((row) => row.text())
    expect(items).toHaveLength(3)
    expect(items[0]).toContain('Revoked (generation 1)')
    expect(items[1]).toContain('Permission recorded (generation 1)')
    expect(items[1]).toContain('One decision to old-notes')
    expect(items[2]).toContain('Future decisions to team-notes')
    expect(view.text()).toContain('does not remove a note that was already published')
  })

  it('bounds long engine text and renders markup as text', async () => {
    const long = 'x'.repeat(5000)
    apiGet.mockResolvedValue(evidence([grant({
      project_id: long, source_label: long, field_mask: [long],
      history: [{ event: 'withdrawn', at: '2026-11-25T10:00:00Z', reason: '<img src=x onerror=alert(1)>' }],
    })]))
    const view = await mountView([brain()])
    expect(view.text().length).toBeLessThan(2500)
    expect(view.text()).not.toContain('x'.repeat(300))
    expect(view.find('img').exists()).toBe(false)
    expect(view.text()).toContain('<img src=x onerror=alert(1)>')
  })

  it('says so when nothing has been recorded', async () => {
    apiGet.mockResolvedValue(evidence([]))
    const view = await mountView([])
    expect(view.text()).toContain('No shared brains are linked')
    expect(view.text()).toContain('No grants or revocations have been recorded.')
  })

  it('shows a failed first read with a retry, and a stale refresh keeps the last record', async () => {
    apiGet.mockRejectedValueOnce(new Error('offline'))
    const view = await mountView([brain()])
    expect(view.find('[role="alert"]').text()).toContain('Could not read the consent record')
    apiGet.mockResolvedValueOnce(evidence([grant()]))
    await view.find('button').trigger('click')
    await flushPromises()
    expect(view.text()).toContain('Future decisions to team-notes')
  })

  it('drops a stale reply after the workspace changes', async () => {
    let release: (v: ShareEvidenceState) => void = () => {}
    apiGet.mockImplementationOnce(() => new Promise((resolve) => { release = resolve }))
    const view = await mountView([brain()])
    apiGet.mockResolvedValueOnce(evidence([grant({ grant_id: 'grn_new', project_id: 'proj-new' })]))
    useProjectStore().activeWorkspace = 'other'
    await flushPromises()
    release(evidence([grant({ grant_id: 'grn_old', project_id: 'proj-old' })]))
    await flushPromises()
    expect(view.text()).toContain('proj-new')
    expect(view.text()).not.toContain('proj-old')
  })
})
