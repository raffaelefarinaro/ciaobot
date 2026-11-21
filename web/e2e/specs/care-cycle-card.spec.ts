import { expect, test, type Locator, type Page } from '@playwright/test'
import { boot, fixtureWrites, horizontalOverflow, isolate, quiesce } from '../support/app'

/**
 * Settings > Workspace > Overview's "Weekly care cycle" card (#3061), on a
 * phone. Only a real engine can say whether the card fits the width, whether the
 * checkbox and buttons reach 44px on a coarse pointer, and whether the keyboard
 * reaches the confirm. The fixture answers the dry run and the switch.
 */
const OVERVIEW = '/settings/workspace'
const CONFIRM_LABEL = 'I have stopped every other Ciaobot or older version that uses this workspace.'

test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

function card(page: Page): Locator {
  return page.locator('.card', { has: page.getByText('Weekly care cycle', { exact: true }) })
}

async function openCard(page: Page): Promise<Locator> {
  await boot(page, OVERVIEW, '.settings-main .section-title')
  const panel = card(page)
  await expect(panel).toBeVisible()
  await expect(page.getByText('Checking the care state')).toHaveCount(0)
  await quiesce(page)
  return panel
}

test.describe('care cycle card', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `care-cycle-${testInfo.workerIndex}`)
  })

  test('fits a phone, meets touch targets, and switches only after the box and the confirm', async ({ page }) => {
    const panel = await openCard(page)
    await expect(panel.getByRole('button', { name: 'Switch this workspace' })).toBeVisible()
    const { overflow, culprits } = await horizontalOverflow(page)
    expect(overflow, JSON.stringify(culprits)).toBeLessThanOrEqual(0)

    const confirm = panel.locator('label.care-confirm')
    const button = panel.getByRole('button', { name: 'Switch this workspace' })
    expect(((await confirm.boundingBox()) ?? { height: 0 }).height).toBeGreaterThanOrEqual(44)
    expect(((await button.boundingBox()) ?? { height: 0 }).height).toBeGreaterThanOrEqual(44)

    // The switch is off until the box is ticked; the keyboard ticks it with Space.
    await expect(button).toHaveAttribute('aria-disabled', 'true')
    await panel.getByRole('checkbox', { name: CONFIRM_LABEL }).focus()
    await page.keyboard.press('Space')
    await expect(panel.getByRole('checkbox', { name: CONFIRM_LABEL })).toBeChecked()
    await expect(button).not.toHaveAttribute('aria-disabled', 'true')

    // Enter on the switch opens the confirm. Cancel takes focus first, so a stray Enter cannot switch.
    await button.focus()
    await page.keyboard.press('Enter')
    const dialog = page.getByRole('dialog', { name: 'Switch Workspace care to the new weekly cycle?' })
    await expect(dialog).toBeVisible()
    await expect(dialog).toContainText('Stop other copies first.')
    await expect(dialog.getByRole('button', { name: 'Cancel' })).toBeFocused()
    await page.keyboard.press('Tab')
    await expect(dialog.getByRole('button', { name: 'Switch', exact: true })).toBeFocused()
    await page.keyboard.press('Enter')

    await expect(panel.getByRole('status').filter({ hasText: 'Switched.' })).toBeVisible()
    await expect(panel.getByText('already uses the weekly cycle')).toBeVisible()

    // The backup path wraps inside the card at a phone width.
    const path = panel.locator('.care-path').first()
    const box = await path.boundingBox()
    expect(box!.x + box!.width).toBeLessThanOrEqual(390)
    const after = await horizontalOverflow(page)
    expect(after.overflow, JSON.stringify(after.culprits)).toBeLessThanOrEqual(0)

    const writes = (await fixtureWrites(page)).filter((w) => w.path === '/api/care/cycle')
    expect(writes).toEqual([{ method: 'POST', path: '/api/care/cycle' }])
  })

  test('cancelling the confirm sends nothing', async ({ page }) => {
    const panel = await openCard(page)
    await panel.getByRole('checkbox', { name: CONFIRM_LABEL }).check()
    await panel.getByRole('button', { name: 'Switch this workspace' }).click()
    const dialog = page.getByRole('dialog', { name: 'Switch Workspace care to the new weekly cycle?' })
    await expect(dialog).toBeVisible()
    await dialog.getByRole('button', { name: 'Cancel' }).click()
    await expect(dialog).toHaveCount(0)
    // Focus goes back to the control that opened the confirm.
    await expect(panel.getByRole('button', { name: 'Switch this workspace' })).toBeFocused()
    const writes = (await fixtureWrites(page)).filter((w) => w.path === '/api/care/cycle')
    expect(writes).toEqual([])
  })

  test('a live lease shows the run-in-progress sentence and no switch can run', async ({ page }) => {
    await page.route('**/api/care/cycle**', async (route) => {
      if (route.request().method() !== 'GET') return route.fallback()
      await route.fulfill({
        json: {
          workspace: 'alpha',
          on_host: true,
          lease_live: true,
          status: 'v1',
          live_lease: { holder: 'care run', started_at: '2026-11-21T01:00:00Z', expires_at: '2026-11-21T02:00:00Z' },
          marker: 'present',
          last_full_pass: '2026-11-09',
          daily: 0,
          weekly_historical: 0,
          ambiguous: [],
          dropped: [],
          backup_path: '/fixture/vault/Workspace/Curation-State.v1.bak',
          digest: 'x',
          blockers: ['a live curation lease holds this vault; wait for the run to finish or expire'],
        },
      })
    })
    const panel = await openCard(page)
    await expect(panel.getByText('A care run is in progress. Wait for it to finish.')).toBeVisible()
    await expect(panel.getByRole('button', { name: 'Switch this workspace' })).toHaveAttribute('aria-disabled', 'true')
  })

  test('a browser that is not on the host gets the command to copy, not a switch', async ({ page }) => {
    await page.route('**/api/care/cycle**', async (route) => {
      if (route.request().method() !== 'GET') return route.fallback()
      await route.fulfill({ json: { workspace: 'alpha', on_host: false, reason: 'not_on_host', lease_live: null } })
    })
    const panel = await openCard(page)
    await expect(panel.getByText('This switch runs on the computer running Ciaobot.')).toBeVisible()
    await expect(panel.locator('code')).toContainText('ciao curation-cycle-migrate')
    await expect(panel.getByRole('button', { name: 'Switch this workspace' })).toHaveCount(0)
    const { overflow, culprits } = await horizontalOverflow(page)
    expect(overflow, JSON.stringify(culprits)).toBeLessThanOrEqual(0)
  })
})
