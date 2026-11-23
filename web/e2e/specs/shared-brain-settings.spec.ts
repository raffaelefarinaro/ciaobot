import { expect, test, type Locator, type Page } from '@playwright/test'
import { boot, horizontalOverflow, isolate, quiesce } from '../support/app'

/**
 * Settings > Workspace > Overview's "Shared brains" card (#3008), on a phone.
 * Only a real engine layout can say whether the path field fits the width,
 * whether the buttons reach 44px on a coarse pointer, and whether the keyboard
 * reaches Remove and its confirm. The fixture keeps the list per session.
 */
const OVERVIEW = '/settings/workspace'
const TEAM = '/synthetic/team-notes'
const OTHER = '/synthetic/other-notes'
const DIRTY = '/synthetic/team-notes-dirty'

test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

function card(page: Page): Locator {
  return page.locator('.card', { has: page.getByText('Shared brains', { exact: true }) })
}

async function openCard(page: Page): Promise<Locator> {
  await boot(page, OVERVIEW, '.settings-main .section-title')
  const panel = card(page)
  await expect(panel).toBeVisible()
  await expect(panel.getByText('Checking the shared brains')).toHaveCount(0)
  await quiesce(page)
  return panel
}

async function link(panel: Locator, path: string, name = ''): Promise<void> {
  await panel.getByLabel('Folder path', { exact: true }).fill(path)
  if (name) await panel.getByLabel('Name (optional)', { exact: true }).fill(name)
  await panel.getByRole('button', { name: 'Link folder' }).click()
}

test.describe('shared brains card', () => {
  // One fixture session per test: the linked folders are per session.
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `shared-brain-${testInfo.testId}`)
  })

  test('links two folders, shows their status, keeps them after a reload, and fits a phone', async ({ page }) => {
    const panel = await openCard(page)
    await expect(panel.getByText('Folder path')).toBeVisible()
    const { overflow, culprits } = await horizontalOverflow(page)
    expect(overflow, JSON.stringify(culprits)).toBeLessThanOrEqual(0)

    const field = panel.getByLabel('Folder path', { exact: true })
    expect(Number.parseFloat(await field.evaluate((el) => getComputedStyle(el).fontSize))).toBeGreaterThanOrEqual(16)
    const button = panel.getByRole('button', { name: 'Link folder' })
    expect(((await button.boundingBox()) ?? { height: 0 }).height).toBeGreaterThanOrEqual(44)

    await link(panel, TEAM)
    await expect(panel.getByText('team-notes', { exact: true })).toBeVisible()
    await link(panel, OTHER)
    await expect(panel.getByText('other-notes', { exact: true })).toBeVisible()
    await expect(panel.getByText('Up to date')).toHaveCount(2)

    await page.reload()
    await quiesce(page)
    await expect(card(page).getByText('other-notes', { exact: true })).toBeVisible()
    await expect(card(page).getByText('team-notes', { exact: true })).toBeVisible()
  })

  test("a refused folder shows the engine's words inline and changes nothing", async ({ page }) => {
    const panel = await openCard(page)
    await link(panel, TEAM)
    await expect(panel.getByText('team-notes', { exact: true })).toBeVisible()

    await link(panel, '/synthetic/not-a-checkout')
    await expect(panel.getByRole('alert')).toContainText('Not a git repository: /synthetic/not-a-checkout')
    await expect(panel.getByLabel('Folder path', { exact: true })).toHaveValue('/synthetic/not-a-checkout')

    await link(panel, TEAM)
    await expect(panel.getByRole('alert')).toContainText('Already linked')
    await expect(panel.getByText('team-notes', { exact: true })).toHaveCount(1)
  })

  test('a folder with local changes says so in words', async ({ page }) => {
    const panel = await openCard(page)
    await link(panel, DIRTY)
    await expect(panel.getByText('Not updated: local changes in the folder')).toBeVisible()
  })

  test('removing one brain asks first, runs from the keyboard, and leaves the other', async ({ page }) => {
    const panel = await openCard(page)
    await link(panel, TEAM)
    await link(panel, OTHER)
    await expect(panel.getByText('other-notes', { exact: true })).toBeVisible()

    await panel.getByRole('button', { name: 'Remove team-notes' }).focus()
    await page.keyboard.press('Enter')
    const dialog = page.getByRole('dialog', { name: 'Disconnect this shared brain?' })
    await expect(dialog).toContainText('Only this brain is disconnected.')
    await expect(dialog.getByRole('button', { name: 'Cancel' })).toBeFocused()
    await page.keyboard.press('Tab')
    await expect(dialog.getByRole('button', { name: 'Remove', exact: true })).toBeFocused()
    await page.keyboard.press('Enter')

    await expect(panel.getByText('team-notes', { exact: true })).toHaveCount(0)
    await expect(panel.getByText('other-notes', { exact: true })).toBeVisible()
  })
})
