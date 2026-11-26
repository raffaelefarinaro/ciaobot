import { defineComponent, h } from 'vue'
import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import SettingsShareAudit from '../components/settings/SettingsShareAudit.vue'
import type { SharedBrainState, ShareEvidenceGrant } from '../lib/types'
import { WORKSPACE, held, json, ownerPanel, route, seedWorkspace, settled } from './settingsAccessFixture'

/**
 * SettingsShareAudit (the `components-settings-settings-share-audit` inventory
 * row, #3622): the read-only consent and history view. It reads
 * `/api/workspaces/{name}/share-evidence` on mount and performs no write, so
 * no story declares another route. Every brain, repository and project is
 * synthetic and uses a reserved `.invalid` host.
 */
const meta = {
  id: 'settings-share-audit',
  title: 'Settings access and memory/SettingsShareAudit',
  component: SettingsShareAudit,
  args: { brains: [] },
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof SettingsShareAudit>
export default meta
type Story = StoryObj<typeof meta>

function brain(over: Partial<SharedBrainState> = {}): SharedBrainState {
  return {
    name: 'team-notes', kind: 'git', identity: 'example.invalid/storybook/team-notes', path: '/synthetic/team-notes',
    lifetime: 'storybook-lifetime', sharing_policy: 'review', sharing_generation: 0, sharing_granted_at: '',
    sharing_grant_pending: false, sharing_invalidation_failed: false, origin_changed: false, missing: false, problem: '',
    branch: 'main', head: 'abc1234', head_date: '2026-11-20', dirty: false, upstream: true, fetched_at: null,
    last_update: null, ...over,
  }
}

function grant(over: Partial<ShareEvidenceGrant> = {}): ShareEvidenceGrant {
  return {
    grant_id: 'grn_storybook_a', type: 'standing', project_id: 'proj-alpha', source_id: 'stream:proj-alpha',
    source_kind: 'stream', source_label: 'Structured decisions · Alpha',
    brain: { name: 'team-notes', identity: 'example.invalid/storybook/team-notes', link_fingerprint: 'f'.repeat(16) },
    audience: 'example.invalid/storybook/team-notes', field_mask: ['decision', 'effective_date'],
    producers: ['memory_extraction'], issued_at: '2026-11-25T10:00:00Z', start_revision: 1, consent_generation: 1,
    active: true, linked: true, status: 'active', revoked_at: null, revoked_generation: null, withdrawn_at: null,
    withdrawal_reason: '', history: [{ event: 'issued', at: '2026-11-25T10:00:00Z', generation: 1 }], ...over,
  }
}

const evidence = (grants: ShareEvidenceGrant[]) => ({
  chat_evidence: { available: false, status: 'remote_client' }, disclosure_tokens: {}, workspace: WORKSPACE,
  mode: 'review', fields: ['decision', 'reason', 'effective_date'], producers: ['memory_extraction', 'nightly_curation'],
  brains: [], projects: [], grants,
})

const evidenceRoute = (respond: () => Response | Promise<never>) =>
  route('GET', `/api/workspaces/${WORKSPACE}/share-evidence`, respond)
const panel = (brains: SharedBrainState[]) => ownerPanel({
  label: 'SettingsShareAudit',
  narrow: true,
  seed: seedWorkspace,
  component: defineComponent({ name: 'ShareAuditHost', setup: () => () => h(SettingsShareAudit, { brains }) }),
})

const REVOKED = grant({
  grant_id: 'grn_storybook_b', status: 'revoked', active: false, revoked_at: '2026-11-25T12:00:00Z', revoked_generation: 1,
  history: [
    { event: 'issued', at: '2026-11-25T10:00:00Z', generation: 1 },
    { event: 'revoked', at: '2026-11-25T12:00:00Z', generation: 1 },
  ],
})

export const Loading: Story = {
  parameters: { fixture: { routes: [evidenceRoute(held)] } },
  render: () => panel([brain()]),
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.textContent).toContain('Reading the consent record'))
    expect(canvasElement.querySelector('.audit-list')).toBeNull()
  },
}

export const Failed: Story = {
  parameters: { fixture: { routes: [evidenceRoute(() => json({ error: 'Synthetic read refused' }, 500))] } },
  render: () => panel([brain()]),
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.querySelector('[role="alert"]')?.textContent)
      .toContain('Could not read the consent record: Synthetic read refused'))
    expect(canvasElement.querySelector('.audit-list')).toBeNull()
  },
}

export const Empty: Story = {
  parameters: { fixture: { routes: [evidenceRoute(() => json(evidence([])))] } },
  render: () => panel([brain()]),
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.textContent).toContain('No project permission is recorded for this brain.'))
    expect(canvasElement.textContent).toContain('You review each note before it is shared.')
    expect(canvasElement.textContent).toContain('No grants or revocations have been recorded.')
  },
}

export const ActiveGrant: Story = {
  parameters: { fixture: { routes: [evidenceRoute(() => json(evidence([grant()])))] } },
  render: () => panel([brain({ sharing_policy: 'automatic', sharing_generation: 1 })]),
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.querySelector('.audit-state')?.textContent).toBe('Active'))
    expect(canvasElement.textContent).toContain('it is not active yet')
    expect(canvasElement.textContent).toContain('Audience unknown')
    expect(canvasElement.textContent).toContain('cannot recall a note once it is published')
    expect(canvasElement.querySelectorAll('button')).toHaveLength(0)
  },
}

export const RevokedHistory: Story = {
  parameters: { fixture: { routes: [evidenceRoute(() => json(evidence([grant(), REVOKED])))] } },
  render: () => panel([brain()]),
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.querySelectorAll('.audit-event')).toHaveLength(3))
    expect(canvasElement.querySelector('.audit-event')?.textContent).toContain('Revoked (generation 1)')
    expect([...canvasElement.querySelectorAll('.audit-state')].map((el) => el.textContent)).toEqual(['Active', 'Revoked'])
  },
}

export const FolderBrain: Story = {
  parameters: { fixture: { routes: [evidenceRoute(() => json(evidence([])))] } },
  render: () => panel([brain({ name: 'local-notes', kind: 'folder', identity: 'folder:/synthetic/local-notes' })]),
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.textContent).toContain('A folder is never shared automatically'))
    expect(canvasElement.textContent).toContain('does not confirm that Google Drive, iCloud or another sync service has finished syncing')
    expect(canvasElement.textContent).not.toContain('cannot recall')
  },
}
