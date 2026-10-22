import { expect, test } from '@playwright/test'

import { COMPOSER, boot, horizontalOverflow, isolate } from '../support/app'

const CHAT = '/chat/alpha-chat-1'
const SETTINGS = '.settings-main'
const DRAFT = 'add the design workspace before I forget'

/**
 * Why this is a browser test: whether a link opens a second tab, reloads the
 * page or routes in place is the browser's default action against the click
 * handler, and jsdom performs no navigation at all. Back preserving the
 * composer draft is history the browser owns, not the router.
 *
 * The transcript is injected with the same `page.route` contract
 * `chat-file-open.spec.ts` uses, because the fixture's named shapes do not
 * include a settings link and the fixture server is not this change's to edit.
 */
async function transcriptWithSettingsLink(page: import('@playwright/test').Page): Promise<void> {
  await page.route('**/api/chats/alpha-chat-1/messages*', route => route.fulfill({ json: [
    { role: 'user', content: 'Where do I add a workspace?', sent_at: '2026-01-01T09:50:00Z', turn_index: 0 },
    {
      role: 'assistant',
      content: 'Open [Workspaces](/settings/workspaces), then choose **New workspace**.',
      sent_at: '2026-01-01T09:50:20Z',
      duration_ms: 20000,
      effective_model: 'synthetic-model',
      usage: { input_tokens: 18, output_tokens: 64, context_pct: '1%' },
      turn_index: 0,
    },
  ] }))
}

test.describe('an app page link in a chat reply', () => {
  test('navigates in place on desktop and Back restores the unsent draft', async ({ browser }, testInfo) => {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } })
    const page = await context.newPage()
    await page.addInitScript(() => localStorage.setItem('ciao-theme', 'dark'))
    await isolate(page, `app-links-desktop-${testInfo.workerIndex}`)
    await transcriptWithSettingsLink(page)
    await boot(page, CHAT, COMPOSER)

    const link = page.locator('.message-wrap.assistant a', { hasText: 'Workspaces' })
    await expect(link).toHaveAttribute('href', '/settings/workspaces')
    await expect(link).not.toHaveAttribute('target', /.*/)
    await page.locator(COMPOSER).fill(DRAFT)
    // A full page load would clear this marker; routing in place keeps it.
    await page.evaluate(() => { (window as unknown as { __kept?: boolean }).__kept = true })

    await link.click()
    await expect(page).toHaveURL(/\/settings\/workspaces$/)
    await expect(page.locator(SETTINGS)).toBeVisible()
    await expect(page.locator('.page-tag')).toHaveText('Manage workspaces')
    expect(await page.evaluate(() => (window as unknown as { __kept?: boolean }).__kept)).toBe(true)
    expect(context.pages()).toHaveLength(1)
    await page.screenshot({ path: testInfo.outputPath('desktop-settings.png'), fullPage: true })

    await page.goBack()
    await expect(page).toHaveURL(/\/chat\/alpha-chat-1$/)
    await expect(page.locator(COMPOSER)).toHaveValue(DRAFT)

    // 200% page zoom is indistinguishable from halving the CSS viewport
    // (browser-zoom.spec.ts): re-measure after the resize so the app has to
    // reflow, not merely have been born narrow.
    await page.setViewportSize({ width: 640, height: 450 })
    await expect(page.locator(COMPOSER)).toBeVisible()
    const { overflow, culprits } = await horizontalOverflow(page)
    expect(
      overflow,
      `zoomed chat scrolls ${overflow}px sideways; widest unclipped: ${JSON.stringify(culprits)}`,
    ).toBeLessThanOrEqual(0)
    await page.screenshot({ path: testInfo.outputPath('desktop-back-zoomed.png'), fullPage: true })
    await expect(page.locator(COMPOSER)).toHaveValue(DRAFT)
    await context.close()
  })

  test('navigates from the keyboard on a phone in the light theme', async ({ browser }, testInfo) => {
    const context = await browser.newContext({
      viewport: { width: 390, height: 844 },
      hasTouch: true,
      isMobile: true,
    })
    const page = await context.newPage()
    await page.addInitScript(() => localStorage.setItem('ciao-theme', 'light'))
    await isolate(page, `app-links-mobile-${testInfo.workerIndex}`)
    await transcriptWithSettingsLink(page)
    await boot(page, CHAT, COMPOSER)

    const link = page.locator('.message-wrap.assistant a', { hasText: 'Workspaces' })
    await page.locator(COMPOSER).fill(DRAFT)
    // An Enter activation is a click with no pointer detail; it must route in
    // place exactly like a tap, not fall through to the browser.
    await link.focus()
    await expect(link).toBeFocused()
    await page.keyboard.press('Enter')

    await expect(page).toHaveURL(/\/settings\/workspaces$/)
    await expect(page.locator(SETTINGS)).toBeVisible()
    expect(context.pages()).toHaveLength(1)
    const settingsOverflow = await horizontalOverflow(page)
    expect(
      settingsOverflow.overflow,
      `settings scrolls ${settingsOverflow.overflow}px sideways; widest unclipped: ${JSON.stringify(settingsOverflow.culprits)}`,
    ).toBeLessThanOrEqual(0)
    await page.screenshot({ path: testInfo.outputPath('mobile-settings.png'), fullPage: true })

    await page.goBack()
    await expect(page).toHaveURL(/\/chat\/alpha-chat-1$/)
    await expect(page.locator(COMPOSER)).toHaveValue(DRAFT)
    const chatOverflow = await horizontalOverflow(page)
    expect(
      chatOverflow.overflow,
      `chat scrolls ${chatOverflow.overflow}px sideways; widest unclipped: ${JSON.stringify(chatOverflow.culprits)}`,
    ).toBeLessThanOrEqual(0)
    await page.screenshot({ path: testInfo.outputPath('mobile-back.png'), fullPage: true })
    await context.close()
  })
})
