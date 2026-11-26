import { expect, test, type Locator, type Page } from '@playwright/test'
import { boot, horizontalOverflow, isolate, quiesce } from '../support/app'

/**
 * Settings > Workspace > Overview's read-only Consent and history view (#3622).
 * The evidence API is stubbed; the test records every non-GET so it can assert
 * the view never writes.
 */
const OVERVIEW = '/settings/workspace'

const GRANT = {
  grant_id: 'grn_dddddddddddddddddddddddd',
  type: 'standing',
  project_id: 'proj-alpha',
  source_id: 'stream:proj-alpha',
  source_kind: 'stream',
  source_label: 'Structured decisions · Alpha',
  brain: { name: 'team-notes', identity: 'github.com/acme/team-notes', link_fingerprint: 'f'.repeat(16) },
  audience: 'github.com/acme/team-notes',
  field_mask: ['decision', 'effective_date'],
  producers: ['memory_extraction'],
  issued_at: '2026-11-25T10:00:00Z',
  start_revision: 1,
  consent_generation: 1,
  active: false,
  linked: true,
  status: 'revoked',
  revoked_at: '2026-11-25T12:00:00Z',
  revoked_generation: 1,
  withdrawn_at: null,
  withdrawal_reason: '',
  history: [
    { event: 'issued', at: '2026-11-25T10:00:00Z', generation: 1 },
    { event: 'revoked', at: '2026-11-25T12:00:00Z', generation: 1 },
  ],
}

function card(page: Page): Locator {
  return page.locator('.card', { has: page.getByText('Shared brains', { exact: true }) })
}

async function stub(page: Page) {
  const writes: string[] = []
  await page.route(/\/api\/workspaces\/alpha\/shared-brains$/, (route) => route.fulfill({
    json: { shared_brains: [{
      name: 'team-notes', kind: 'git', identity: 'github.com/acme/team-notes', path: '/synthetic/team-notes',
      lifetime: 'synthetic-lifetime', sharing_policy: 'review', sharing_generation: 1, sharing_granted_at: '',
      sharing_grant_pending: false, sharing_invalidation_failed: false, origin_changed: false, missing: false,
      problem: '', branch: 'main', head: 'abc1234', head_date: '2026-11-20', dirty: false, upstream: true,
      fetched_at: null, last_update: null,
    }] },
  }))
  await page.route(/\/api\/workspaces\/alpha\/share-evidence/, (route) => {
    if (route.request().method() !== 'GET') writes.push(route.request().method())
    return route.fulfill({ json: {
      disclosure_tokens: {}, workspace: 'alpha', chat_evidence: { available: false, status: 'remote_client' },
      mode: 'review', fields: ['decision', 'reason', 'effective_date'], producers: ['memory_extraction', 'nightly_curation'],
      brains: [], projects: [], grants: [GRANT],
    } })
  })
  return writes
}

async function open(page: Page) {
  await boot(page, OVERVIEW, '.settings-main .section-title')
  const toggle = card(page).getByRole('button', { name: 'Consent and history' })
  // Keyboard: focus the toggle and open it with Enter, never a pointer click.
  await toggle.focus()
  await page.keyboard.press('Enter')
  const panel = card(page).locator('.audit-panel')
  await expect(panel).toBeVisible()
  return { toggle, panel }
}

async function assertView(page: Page, panel: Locator) {
  await expect(panel).toContainText('Audience unknown')
  await expect(panel).toContainText('Target: github.com/acme/team-notes')
  await expect(panel).toContainText('Ciaobot cannot recall a note once it is published')
  await expect(panel).toContainText('Revoked')
  await expect(panel.locator('.audit-event').first()).toContainText('Revoked (generation 1)')
  await expect(panel.locator('button, input, select, textarea')).toHaveCount(0)
  await quiesce(page)
  const { overflow, culprits } = await horizontalOverflow(page)
  expect(overflow, JSON.stringify(culprits)).toBeLessThanOrEqual(0)
}

test.describe('shared brain consent and history', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `share-audit-${testInfo.testId}`)
  })

  test.describe('phone', () => {
    test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

    test('opens from the keyboard, is read-only, keeps a 44px toggle and survives reload', async ({ page }, testInfo) => {
      const writes = await stub(page)
      const { toggle, panel } = await open(page)
      const box = await toggle.boundingBox()
      expect(box?.height ?? 0).toBeGreaterThanOrEqual(44)
      await assertView(page, panel)
      await page.screenshot({ path: testInfo.outputPath('audit-390.png'), fullPage: true })
      await page.reload()
      await quiesce(page)
      await card(page).getByRole('button', { name: 'Consent and history' }).click()
      await expect(card(page).locator('.audit-panel')).toContainText('Revoked')
      expect(writes).toEqual([])
    })
  })

  // A real 200% zoom: half the CSS viewport at twice the device scale. Not CSS zoom.
  test.describe('200% zoom', () => {
    test.use({ viewport: { width: 640, height: 400 }, deviceScaleFactor: 2 })

    test('keeps every line reachable with no horizontal overflow', async ({ page }, testInfo) => {
      await stub(page)
      const { panel } = await open(page)
      await assertView(page, panel)
      await page.screenshot({ path: testInfo.outputPath('audit-200pct.png'), fullPage: true })
    })
  })
})
