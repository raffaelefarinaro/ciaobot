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

  // The branch is a disclosure: the summary stays one line, the full name and
  // folder appear in its body when it is opened.
  const branch = section.locator('.chat-repo-branch-details')
  const summary = branch.locator('summary')
  await expect(branch.locator('.chat-repo-branch-body')).not.toBeInViewport()
  await summary.click()
  await expect(branch.locator('.chat-repo-branch-body')).toBeVisible()
  await expect(branch.locator('.chat-repo-branch-value')).toHaveText(/\S/)
  await summary.click()

  const rail = page.locator('.chat-rail').first()
  const overflow = await rail.evaluate(el => el.scrollWidth - el.clientWidth)
  expect(overflow).toBeLessThanOrEqual(0)
  await section.screenshot({ path: 'test-results/chat-details-repo.png' })
})
