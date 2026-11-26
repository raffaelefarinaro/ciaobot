import { expect, test, type Locator, type Page } from '@playwright/test'
import { boot, horizontalOverflow, isolate, quiesce } from '../support/app'

/**
 * Settings > Workspace > Overview's Sources panel (#3614), on a phone. The
 * panel's API is stubbed per test so the exact previewed scope, the snapshot
 * bytes, cancel and revoke are observable without a live engine or vault.
 */
const OVERVIEW = '/settings/workspace'

interface Grant {
  grant_id: string
  type: 'standing' | 'snapshot'
  project_id: string
  source_id: string
  source_kind: 'stream' | 'document'
  source_label: string
  brain: { name: string; identity: string; link_fingerprint: string }
  audience: string
  field_mask: string[]
  producers: string[]
  issued_at: string
  start_revision: number | null
  consent_generation: number
  active: boolean
  linked: boolean
  status: 'active' | 'revoked' | 'withdrawn' | 'unlinked' | 'inactive'
  revoked_at: string | null
  revoked_generation: number | null
  withdrawn_at: string | null
  withdrawal_reason: string
  history: Array<Record<string, unknown>>
}

function card(page: Page): Locator {
  return page.locator('.card', { has: page.getByText('Shared brains', { exact: true }) })
}

function state(grants: Grant[]) {
  return {
    disclosure_tokens: { 'proj-alpha': 'scope' },
    workspace: 'alpha',
    chat_evidence: { available: false, status: 'remote_client' },
    mode: grants.some((grant) => grant.active && grant.type === 'standing') ? 'automatic' : 'review',
    fields: ['decision', 'reason', 'effective_date'],
    producers: ['memory_extraction', 'nightly_curation'],
    brains: [{ name: 'team-notes', identity: 'github.com/acme/team-notes' }],
    projects: [{
      project_id: 'proj-alpha',
      name: 'Alpha',
      sources: [
        {
          source_id: 'stream:proj-alpha',
          kind: 'stream',
          label: 'Structured decisions · Alpha',
          project_id: 'proj-alpha',
          project_name: 'Alpha',
          entry_count: 2,
          stream_id: 'strm_synthetic',
        },
        {
          source_id: 'document:Decisions.md',
          kind: 'document',
          label: 'Decision document · Decisions.md',
          project_id: 'proj-alpha',
          project_name: 'Alpha',
          entry_count: 2,
        },
      ],
    }],
    grants,
  }
}

function grant(): Grant {
  return {
    grant_id: 'grn_cccccccccccccccccccccccc',
    type: 'snapshot',
    project_id: 'proj-alpha',
    source_id: 'document:Decisions.md',
    source_kind: 'document',
    source_label: 'Decision document · Decisions.md',
    brain: {
      name: 'team-notes',
      identity: 'github.com/acme/team-notes',
      link_fingerprint: 'f'.repeat(16),
    },
    audience: 'github.com/acme/team-notes',
    field_mask: ['decision', 'effective_date'],
    producers: ['memory_extraction'],
    issued_at: '2026-11-25T10:00:00Z',
    start_revision: null,
    consent_generation: 1,
    active: true,
    linked: true,
    status: 'active',
    revoked_at: null,
    revoked_generation: null,
    withdrawn_at: null,
    withdrawal_reason: '',
    history: [{ event: 'issued', at: '2026-11-25T10:00:00Z', generation: 1 }],
  }
}

async function stubSources(page: Page) {
  let grants: Grant[] = []
  const calls = { preview: 0, confirm: 0 }
  await page.route(/\/api\/workspaces\/alpha\/shared-brains$/, (route) => route.fulfill({
    json: { shared_brains: [{
      name: 'team-notes',
      kind: 'git',
      identity: 'github.com/acme/team-notes',
      path: '/synthetic/team-notes',
      origin_changed: false,
      missing: false,
      problem: '',
      branch: 'main',
      head: 'abc1234',
      head_date: '2026-11-20',
      dirty: false,
      upstream: true,
      fetched_at: null,
      last_update: null,
    }] },
  }))
  await page.route(/\/api\/workspaces\/alpha\/share-evidence$/, (route) => route.fulfill({
    json: state(grants),
  }))
  await page.route(/\/api\/workspaces\/alpha\/share-evidence\/preview$/, (route) => {
    calls.preview += 1
    return route.fulfill({
      json: {
        preview_token: `sev_synthetic_${calls.preview}`,
        preview: {
          grant_type: 'snapshot',
          scope: 'one_decision',
          summary: 'When sharing becomes available, only the decision shown below is permitted, exactly as previewed.',
          future_items_without_review: false,
          project_id: 'proj-alpha',
          source_id: 'document:Decisions.md',
          source_label: 'Decision document · Decisions.md',
          entry_label: 'Decision 2 in Decisions.md',
          fields: [[
            { field: 'decision', value: 'Delay the next beta' },
            { field: 'effective_date', value: '2026-11-22' },
          ]],
          field_mask: ['decision', 'effective_date'],
          excluded_fields: ['reason'],
          producers: ['memory_extraction'],
          recipient: {
            brain: 'team-notes',
            identity: 'github.com/acme/team-notes',
            audience: 'github.com/acme/team-notes',
          },
          expected_generation: 'a'.repeat(64),
          expires_at: '2026-11-25T10:15:00Z',
          notice: 'Changing or deleting the document later does not withdraw this confirmation; revoke it here instead.',
        },
      },
    })
  })
  await page.route(/\/api\/workspaces\/alpha\/share-evidence\/confirm$/, (route) => {
    calls.confirm += 1
    grants = [grant()]
    return route.fulfill({ json: { grant: grants[0], mode: 'automatic' } })
  })
  await page.route(/\/api\/workspaces\/alpha\/share-evidence\/grants\/[^/]+\/revoke$/, (route) => {
    grants = grants.map((row) => ({
      ...row,
      active: false,
      linked: true,
      status: 'revoked' as const,
      revoked_at: '2026-11-25T11:00:00Z',
      revoked_generation: 1,
    }))
    return route.fulfill({ json: { grant: grants[0], fresh_grant_required: true } })
  })
  return calls
}

test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

test.describe('shared brain sources and consent', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `share-evidence-${testInfo.testId}`)
  })

  test('previews exact fields, cancels, confirms, revokes from the keyboard and survives reload', async ({ page }) => {
    const calls = await stubSources(page)
    await boot(page, OVERVIEW, '.settings-main .section-title')
    const panel = card(page)
    await panel.getByRole('button', { name: 'Sources and consent' }).click()
    const sources = panel.locator('.sources-panel')
    await expect(sources.getByText('Review', { exact: true })).toBeVisible()
    await expect(sources.getByTestId('chat-evidence-status')).toContainText('Ordinary chat messages remain private and are not shared')
    await expect(sources.getByTestId('chat-evidence-status')).toContainText('Only the Ciaobot Mac app on this Mac')
    await quiesce(page)
    const { overflow, culprits } = await horizontalOverflow(page)
    expect(overflow, JSON.stringify(culprits)).toBeLessThanOrEqual(0)

    // The picker is server-filtered to the linked brain and the project's actual sources.
    await expect(sources.getByLabel('Shared brain', { exact: true }).locator('option')).toHaveText([
      'team-notes — github.com/acme/team-notes',
    ])
    await expect(sources.getByLabel('Actual source', { exact: true }).locator('option')).toHaveText([
      'Structured decisions · Alpha (2)',
      'Decision document · Decisions.md (2)',
    ])
    await sources.getByLabel('Actual source', { exact: true }).selectOption('document:Decisions.md')
    await sources.getByLabel('Only this decision', { exact: true }).check()
    await sources.getByLabel('Decision in Decisions.md', { exact: true }).selectOption('1')
    await sources.getByLabel('reason', { exact: true }).uncheck()
    await sources.getByLabel('Nightly curation', { exact: true }).uncheck()

    await sources.getByRole('button', { name: 'Preview exact scope' }).click()
    const preview = sources.locator('.sources-preview')
    await expect(preview).toContainText('When sharing becomes available, only the decision shown below is permitted, exactly as previewed.')
    await expect(preview).toContainText('Delay the next beta')
    await expect(preview).toContainText('2026-11-22')
    await expect(preview).toContainText('reason')
    await expect(preview).toContainText('Changing or deleting the document later does not withdraw this confirmation')
    // The preview is values, not an editable stand-in for the frozen bytes.
    await expect(preview.locator('input')).toHaveCount(0)

    await preview.getByRole('button', { name: 'Cancel' }).click()
    await expect(sources.locator('.sources-preview')).toHaveCount(0)
    expect(calls.confirm).toBe(0)

    await sources.getByRole('button', { name: 'Preview exact scope' }).click()
    await sources.getByRole('button', { name: 'Confirm and grant' }).click()
    expect(calls.confirm).toBe(1)
    await expect(sources.getByText('Review', { exact: true })).toBeVisible()
    const row = sources.locator('.sources-grant-row')
    await expect(row).toContainText('grn_cccccccccccccccccccccccc')
    await expect(row).toContainText('github.com/acme/team-notes')
    await expect(row).toContainText('One decision')

    await row.getByRole('button', { name: 'Inspect exact scope' }).click()
    await expect(row.locator('.sources-grant-detail')).toContainText('Exact previewed snapshot')

    const revoke = row.getByRole('button', { name: 'Revoke' })
    await revoke.focus()
    await page.keyboard.press('Enter')
    const dialog = page.getByRole('dialog', { name: 'Revoke this disclosure?' })
    await expect(dialog).toContainText('Earlier authorized evidence can no longer be extracted')
    await page.keyboard.press('Tab')
    await page.keyboard.press('Enter')
    await expect(sources.getByText('Generation 1 was revoked. A fresh preview and grant are required.')).toBeVisible()
    await expect(row.locator('.sources-grant-state')).toHaveText('Revoked')
    await expect(revoke).toBeDisabled()

    await page.reload()
    await quiesce(page)
    const reloaded = card(page)
    await reloaded.getByRole('button', { name: 'Sources and consent' }).click()
    await expect(reloaded.locator('.sources-grant-state')).toHaveText('Revoked')
    await expect(reloaded.locator('.sources-grant-row')).toContainText('Generation 1 ended. A fresh preview and grant are required.')
  })
})
