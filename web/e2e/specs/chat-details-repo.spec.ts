import { expect, test } from '@playwright/test'
import { COMPOSER, boot, isolate } from '../support/app'

/**
 * Chat details' Repository section (#1546) is laid out by the browser: the
 * name row with its copy button, the code-mode ⓘ next to the switch, and the
 * rail never scrolling sideways.
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

  const info = section.locator('.repo-code-info')
  await expect(info).toHaveAttribute('aria-expanded', 'false')
  await expect(section.locator('.repo-code-hint').first()).not.toBeInViewport()
  await info.click()
  await expect(info).toHaveAttribute('aria-expanded', 'true')
  await expect(section.locator('.repo-code-hint').first()).toBeVisible()

  await expect(page.locator('.chat-rail-agent-context').getByText('Always loaded', { exact: true })).toBeVisible()

  // The branch is a disclosure: its line stays one line with the checkout
  // facts beside it, and the full name and folder open underneath at the
  // section's full width, not in a column squeezed beside the facts.
  const toggle = section.locator('button.chat-repo-branch')
  const body = section.locator('.chat-repo-branch-body')
  await expect(body).toBeHidden()
  await toggle.click()
  await expect(body).toBeVisible()
  await expect(section.locator('.chat-repo-branch-value')).toHaveText(/\S/)
  const facts = section.locator('.chat-repo-facts')
  const toggleBox = (await toggle.boundingBox())!
  const factsBox = (await facts.boundingBox())!
  const bodyBox = (await body.boundingBox())!
  const sectionBox = (await section.boundingBox())!
  expect(Math.abs((factsBox.y + factsBox.height / 2) - (toggleBox.y + toggleBox.height / 2))).toBeLessThanOrEqual(3)
  expect(bodyBox.y).toBeGreaterThanOrEqual(toggleBox.y + toggleBox.height - 1)
  expect(bodyBox.width).toBeGreaterThanOrEqual(sectionBox.width - 2)
  const openOverflow = await section.evaluate(el => el.scrollWidth - el.clientWidth)
  expect(openOverflow).toBeLessThanOrEqual(0)
  await page.locator('.chat-rail').first().screenshot({ path: 'test-results/chat-details-rail-open.png' })
  await toggle.click()

  const rail = page.locator('.chat-rail').first()
  const overflow = await rail.evaluate(el => el.scrollWidth - el.clientWidth)
  expect(overflow).toBeLessThanOrEqual(0)
  await section.screenshot({ path: 'test-results/chat-details-repo.png' })
})
