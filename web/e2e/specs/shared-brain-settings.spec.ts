import { expect, test, type Locator, type Page } from '@playwright/test'
import { boot, horizontalOverflow, isolate, quiesce } from '../support/app'

/**
 * Settings > Workspace > Overview's "Shared brains" card (#3008), on a phone.
 * Only a real engine layout can say whether the Link type select, path field and
 * buttons fit the width and reach 44px on a coarse pointer, whether the keyboard
 * reaches Remove and its confirm, and that mixed Folder and Git rows keep their
 * kinds after a reload. The fixture keeps the list per session.
 */
const OVERVIEW = '/settings/workspace'
const TEAM = '/synthetic/team-notes'
const OTHER = '/synthetic/other-notes'
const FOLDER = '/synthetic/local-folder'
const UNAVAILABLE = '/synthetic/unavailable-folder'
const DIRTY = '/synthetic/team-notes-dirty'
const COPY = '/synthetic/team-notes-copy'
const LIMIT_SENTENCE = 'You can connect up to 5 shared brains. Remove one to add another.'

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

async function link(
  panel: Locator,
  path: string,
  { name = '', kind = 'folder' }: { name?: string; kind?: 'folder' | 'git' } = {},
): Promise<void> {
  await panel.getByLabel('Link type', { exact: true }).selectOption(kind)
  await panel.getByLabel('Folder path', { exact: true }).fill(path)
  await panel.getByLabel('Name (optional)', { exact: true }).fill(name)
  await panel.getByRole('button', {
    name: kind === 'git' ? 'Link Git repository' : 'Link folder',
  }).click()
}

test.describe('shared brains card', () => {
  // One fixture session per test: the linked folders are per session.
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `shared-brain-${testInfo.testId}`)
  })

  test('links a Folder and a Git repository, keeps both kinds after reload, and fits a phone', async ({ page }) => {
    const panel = await openCard(page)
    const type = panel.getByLabel('Link type', { exact: true })
    await expect(type).toBeVisible()
    await expect(type).toHaveValue('folder')

    const { overflow, culprits } = await horizontalOverflow(page)
    expect(overflow, JSON.stringify(culprits)).toBeLessThanOrEqual(0)

    const field = panel.getByLabel('Folder path', { exact: true })
    expect(Number.parseFloat(await field.evaluate((el) => getComputedStyle(el).fontSize))).toBeGreaterThanOrEqual(16)
    expect(((await type.boundingBox()) ?? { height: 0 }).height).toBeGreaterThanOrEqual(44)
    const folderButton = panel.getByRole('button', { name: 'Link folder' })
    expect(((await folderButton.boundingBox()) ?? { height: 0 }).height).toBeGreaterThanOrEqual(44)

    // The setup guidance follows the explicit choice, not the rows already listed.
    await expect(panel.getByText('Folder links are read in place for reference')).toBeVisible()
    await expect(panel.getByText('To set up a Git repository')).toHaveCount(0)
    await type.selectOption('git')
    await expect(panel.getByText('To set up a Git repository')).toBeVisible()
    await expect(panel.getByText('Folder links are read in place for reference')).toHaveCount(0)
    await type.selectOption('folder')

    await link(panel, FOLDER)
    await expect(panel.getByText('local-folder', { exact: true })).toBeVisible()
    await expect(panel.locator('.shared-brain-kind', { hasText: 'Folder · reference only' })).toHaveCount(1)
    await expect(panel.getByText('Folder is locally available. Read in place, never updated.')).toBeVisible()
    await expect(panel.getByText('Checked-out commit')).toHaveCount(0)

    await link(panel, TEAM, { kind: 'git' })
    await expect(panel.getByText('team-notes', { exact: true })).toBeVisible()
    await expect(panel.locator('.shared-brain-kind', { hasText: 'Git repository' })).toHaveCount(1)
    await expect(panel.getByText('Up to date')).toHaveCount(1)
    await expect(panel.locator('.shared-brain-kind', { hasText: 'Folder · reference only' })).toHaveCount(1)

    await page.reload()
    await quiesce(page)
    await expect(card(page).getByText('local-folder', { exact: true })).toBeVisible()
    await expect(card(page).locator('.shared-brain-kind', { hasText: 'Folder · reference only' })).toHaveCount(1)
    await expect(card(page).locator('.shared-brain-kind', { hasText: 'Git repository' })).toHaveCount(1)
  })

  test("the engine's refusals show inline, in its own words, and change nothing", async ({ page }) => {
    const panel = await openCard(page)
    await link(panel, TEAM, { kind: 'git' })
    await expect(panel.getByText('team-notes', { exact: true })).toBeVisible()

    await link(panel, '/synthetic/not-a-checkout', { kind: 'git' })
    await expect(panel.getByRole('alert')).toContainText('Not a git repository: /synthetic/not-a-checkout')
    await expect(panel.getByLabel('Folder path', { exact: true })).toHaveValue('/synthetic/not-a-checkout')

    await link(panel, TEAM, { kind: 'git' })
    await expect(panel.getByRole('alert')).toContainText(`Shared brain folder is listed twice: ${TEAM}`)

    await link(panel, COPY, { kind: 'git' })
    await expect(panel.getByRole('alert')).toContainText(`${COPY} is the same repository as shared brain 'team-notes'`)

    await link(panel, OTHER, { name: 'Bad Name', kind: 'git' })
    await expect(panel.getByRole('alert')).toContainText('shared brain name must be 1 to 32 lowercase letters, digits or dashes')

    await link(panel, OTHER, { name: 'team-notes', kind: 'git' })
    await expect(panel.getByRole('alert')).toContainText("shared brain name 'team-notes' is already used in this workspace")

    await expect(panel.getByText('team-notes', { exact: true })).toHaveCount(1)
    await expect(panel.getByText('other-notes', { exact: true })).toHaveCount(0)
  })

  test('the engine refuses a sixth row, and five folders show the replacement sentence', async ({ page }) => {
    const panel = await openCard(page)
    for (const n of [1, 2, 3, 4, 5]) await link(panel, `/synthetic/brain-${n}`, { kind: 'git' })
    await expect(panel.getByText('brain-5', { exact: true })).toBeVisible()
    await expect(panel.getByText(LIMIT_SENTENCE)).toBeVisible()
    await expect(panel.getByLabel('Folder path', { exact: true })).toHaveCount(0)

    const rows = [1, 2, 3, 4, 5, 6].map((n) => ({ path: `/synthetic/brain-${n}`, kind: 'git' }))
    const sixth = await page.request.patch('/api/workspaces/alpha', { data: { shared_brains: rows } })
    expect(sixth.status()).toBe(400)
    expect(await sixth.json()).toEqual({ error: 'A workspace can link at most 5 shared brains' })
  })

  test('a linked folder says local availability and never shows Git metadata', async ({ page }) => {
    const panel = await openCard(page)
    await link(panel, UNAVAILABLE)
    await expect(panel.getByText('Folder is not available to read on this computer')).toBeVisible()
    await expect(panel.getByText('First update pending')).toHaveCount(0)
  })

  test('a Git checkout with local changes says so in words', async ({ page }) => {
    const panel = await openCard(page)
    await link(panel, DIRTY, { kind: 'git' })
    await expect(panel.getByText('Not updated: local changes in the folder')).toBeVisible()
  })

  test('removing one brain asks first, runs from the keyboard, and leaves the other kind intact', async ({ page }) => {
    const panel = await openCard(page)
    await link(panel, TEAM, { kind: 'git' })
    await link(panel, FOLDER)
    await expect(panel.getByText('local-folder', { exact: true })).toBeVisible()

    await panel.getByRole('button', { name: 'Remove team-notes' }).focus()
    await page.keyboard.press('Enter')
    const dialog = page.getByRole('dialog', { name: 'Disconnect this shared brain?' })
    await expect(dialog).toContainText('Only this brain is disconnected.')
    await expect(dialog.getByRole('button', { name: 'Cancel' })).toBeFocused()
    await page.keyboard.press('Tab')
    await expect(dialog.getByRole('button', { name: 'Remove', exact: true })).toBeFocused()
    await page.keyboard.press('Enter')

    await expect(panel.getByText('team-notes', { exact: true })).toHaveCount(0)
    await expect(panel.getByText('local-folder', { exact: true })).toBeVisible()
    await expect(panel.locator('.shared-brain-kind', { hasText: 'Folder · reference only' })).toHaveCount(1)
  })
})
