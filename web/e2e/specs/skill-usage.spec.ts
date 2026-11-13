import { expect, test, type Page } from '@playwright/test'
import { boot, COMPOSER, isolate, openChatDetails } from '../support/app'

/**
 * An opencode skill the provider records by `id` must appear in Chat details,
 * with its named activity description, while the transcript renders in a real
 * browser (#2863). The section only exists when the rail collects the skill, so
 * a unit test can miss a rail that never opens at this width.
 */
async function openSkillChat(page: Page, session: string) {
  await isolate(page, session)
  await page.route('**/api/chats/alpha-chat-1/messages*', route => route.fulfill({ json: [
    { role: 'user', content: 'Use the loop skill', turn_index: 0 },
    { role: 'system', content: '⚙️ skill {"id": "loop-engineering"}', tool_name: '_activity' },
    { role: 'assistant', content: 'Finished.', timestamp: '2026-08-01T00:00:10Z', turn_index: 0 },
  ] }))
  await boot(page, '/chat/alpha-chat-1', COMPOSER)
  const rail = page.locator('#chat-work-rail')
  if (!(await rail.isVisible())) await openChatDetails(page)
  await expect(rail).toBeVisible()
  return rail
}

test.describe('desktop', () => {
  test.use({ viewport: { width: 1280, height: 900 } })

  test('Chat details names an opencode skill from its id', async ({ page }, testInfo) => {
    const rail = await openSkillChat(page, `skill-usage-desktop-${testInfo.workerIndex}`)
    const section = rail.locator('.chat-rail-tools')
    await expect(section).toBeVisible()
    await expect(section.locator('.cd-heading')).toHaveText('Skills and MCP')
    await expect(section.locator('.chat-rail-tool')).toHaveCount(1)
    const row = section.locator('.chat-rail-tool')
    await expect(row.locator('.cd-primary')).toHaveText('loop-engineering')
    await expect(row.locator('.cd-detail')).toHaveText('Skill')
    await expect(row.locator('.cd-value')).toHaveCount(0)
    await page.screenshot({ path: testInfo.outputPath('skill-usage-desktop.png'), fullPage: false, animations: 'disabled' })
  })
})

test.describe('phone', () => {
  test.use({
    viewport: { width: 430, height: 932 },
    isMobile: true,
    hasTouch: true,
    deviceScaleFactor: 3,
  })

  test('the Chat details drawer names an opencode skill from its id', async ({ page }, testInfo) => {
    const rail = await openSkillChat(page, `skill-usage-phone-${testInfo.workerIndex}`)
    const section = rail.locator('.chat-rail-tools')
    await expect(section).toBeVisible()
    await expect(section.locator('.cd-heading')).toHaveText('Skills and MCP')
    const row = section.locator('.chat-rail-tool')
    await expect(row).toHaveCount(1)
    await expect(row.locator('.cd-primary')).toHaveText('loop-engineering')
    await expect(row.locator('.cd-detail')).toHaveText('Skill')
    await page.screenshot({ path: testInfo.outputPath('skill-usage-phone.png'), fullPage: false, animations: 'disabled' })
  })
})
