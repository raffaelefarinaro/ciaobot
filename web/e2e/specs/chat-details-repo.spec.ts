import { expect, test } from '@playwright/test'
import { COMPOSER, boot, isolate } from '../support/app'

/**
 * Chat details' Repository section (#1546) is laid out by the browser: the
 * repo and branch rows, and the rail never scrolling sideways.
 */
const CHAT_PATH = '/chat/alpha-chat-1'

test.use({ viewport: { width: 1440, height: 900 } })

test.beforeEach(async ({ page }, testInfo) => {
  await isolate(page, `chat-details-repo-${testInfo.workerIndex}`)
})

test('the Repository section is compact and the rail does not scroll sideways', async ({ page }) => {
  await boot(page, CHAT_PATH, COMPOSER)
  const section = page.locator('.chat-repo-section')
  await expect(section).toBeVisible({ timeout: 8_000 })

  await expect(section.locator('.chat-repo-copy')).toBeVisible()
  await expect(section).not.toContainText('Open AGENTS.md')
  await expect(section).not.toContainText('This chat works in')

  // Code mode follows the project's repository link: no switch, no note.
  await expect(section.getByRole('switch')).toHaveCount(0)
  await expect(section).not.toContainText('code mode')

  await expect(page.locator('.chat-rail-agent-context').getByText('Always loaded', { exact: true })).toBeVisible()

  await expect(section.locator('button.chat-repo-branch')).toHaveCount(0)
  await expect(section.locator('.chat-repo-branch-body')).toHaveCount(0)
  const path = section.locator('.chat-repo-path')
  await expect(path).toHaveText(/\S/)
  expect(await path.evaluate(el => getComputedStyle(el).textOverflow)).toBe('ellipsis')
  const branchBox = (await section.locator('.chat-repo-branch').boundingBox())!
  const factsBox = (await section.locator('.chat-repo-facts').boundingBox())!
  expect(factsBox.y).toBeGreaterThanOrEqual(branchBox.y + branchBox.height - 1)
  // rows bleed 8px past the section box by design; measure the rail, which has padding
  const rail = page.locator('.chat-rail').first()
  expect(await rail.evaluate(el => el.scrollWidth - el.clientWidth)).toBeLessThanOrEqual(0)
  await page.locator('.chat-rail').first().screenshot({ path: 'test-results/chat-details-rail-open.png' })

  await section.screenshot({ path: 'test-results/chat-details-repo.png' })
})

test('a long branch ellipsizes on its own row and the rail never scrolls sideways', async ({ page }) => {
  const branch = 'feature/a-very-long-branch-name-that-cannot-fit-beside-the-facts'
  await page.route('**/api/projects', async route => {
    const response = await route.fetch()
    const projects = await response.json()
    await route.fulfill({ response, json: projects.map((p: { repo?: Record<string, unknown> | null }) => (
      p.repo ? { ...p, repo: { ...p.repo, branch, dirty: true } } : p
    )) })
  })
  // The chat works in the main checkout, so the uncommitted-changes fact shows.
  // The list (`?active_only=1`) and the single-chat read both carry it.
  await page.route(/\/api\/chats(\/[^/?]+)?(\?.*)?$/, async route => {
    const response = await route.fetch()
    const body = await response.json()
    const strip = (c: Record<string, unknown>) => {
      const { repo_location: _drop, ...rest } = c
      return rest
    }
    await route.fulfill({ response, json: Array.isArray(body) ? body.map(strip) : strip(body) })
  })
  await boot(page, CHAT_PATH, COMPOSER)
  const section = page.locator('.chat-repo-section')
  await expect(section.locator('.chat-repo-dirty')).toBeVisible({ timeout: 8_000 })
  // The facts sit under the name, so the name gets most of the section's width.
  const nameWidth = await section.locator('.chat-repo-branch').evaluate(el => el.clientWidth)
  const sectionWidth = await section.evaluate(el => el.clientWidth)
  expect(nameWidth).toBeGreaterThan(sectionWidth * 0.6)
  expect(await section.locator('.chat-repo-branch').evaluate(el => el.scrollWidth > el.clientWidth)).toBe(true)
  const rail = page.locator('.chat-rail').first()
  expect(await rail.evaluate(el => el.scrollWidth - el.clientWidth)).toBeLessThanOrEqual(0)
})
