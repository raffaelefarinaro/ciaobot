import { expect, test } from '@playwright/test'

import { COMPOSER, boot, horizontalOverflow, isolate } from '../support/app'

const CHAT = '/chat/alpha-chat-1'
const SETTINGS = '.settings-main'
const TARGET = '/settings/workspaces?workspace=beta#file-access'
const DRAFT = 'add the design workspace before I forget'

/**
 * Why this is a browser test: the link is a real hash navigation that switches
 * the active workspace and lands on Overview, and whether the File access
 * selector is scrolled inside the pane and focused is a layout question jsdom
 * cannot answer. A write would be a real request, so the same test listens.
 *
 * The transcript is injected with the `page.route` contract `app-links.spec.ts`
 * uses; the fixture server is not this change's to edit.
 */
async function transcriptWithFileAccessLink(page: import('@playwright/test').Page): Promise<void> {
  await page.route('**/api/chats/alpha-chat-1/messages*', route => route.fulfill({ json: [
    { role: 'user', content: 'How do I tighten beta file access?', sent_at: '2026-01-01T09:50:00Z', turn_index: 0 },
    {
      role: 'assistant',
      content: 'Open [File access for beta](/settings/workspaces?workspace=beta#file-access) and choose a scope.',
      sent_at: '2026-01-01T09:50:20Z',
      duration_ms: 20000,
      effective_model: 'synthetic-model',
      usage: { input_tokens: 18, output_tokens: 64, context_pct: '1%' },
      turn_index: 0,
    },
  ] }))
}

/** Workspace mutations the link must never issue. */
function watchWorkspaceWrites(page: import('@playwright/test').Page): string[] {
  const writes: string[] = []
  page.on('request', request => {
    const url = new URL(request.url())
    if (url.pathname.startsWith('/api/workspaces') && request.method() !== 'GET') {
      writes.push(`${request.method()} ${url.pathname}`)
    }
  })
  return writes
}

async function expectFileAccessTarget(
  page: import('@playwright/test').Page,
  name: string,
): Promise<void> {
  await expect(page).toHaveURL(/\/settings\/workspace#file-access$/)
  // The link made the named workspace active; the pane header names it.
  await expect(page.locator('.page-tag')).toContainText(`${name[0].toUpperCase()}${name.slice(1)} · Overview`)
  const select = page.locator(`#workspace-file-access-${name}`)
  await expect(select).toBeVisible()
  await expect(select).toBeFocused()
  // The fixture's workspace never chose a scope, so the engine default is
  // showing. The link located the control without selecting a value.
  await expect(select).toHaveValue('')

  // `toBeVisible` is a CSS fact, not a viewport fact. The selector must be
  // wholly on screen and wholly inside the pane that clips it — the failure
  // this catches is scrolling a card header into view and leaving the field
  // row below the fold.
  await expect(select).toBeInViewport({ ratio: 1 })
  const placement = await select.evaluate((el) => {
    const pane = el.closest('.pane-body')
    if (!pane) return null
    const box = el.getBoundingClientRect()
    const clip = pane.getBoundingClientRect()
    return {
      box: { top: box.top, right: box.right, bottom: box.bottom, left: box.left },
      clip: { top: clip.top, right: clip.right, bottom: clip.bottom, left: clip.left },
    }
  })
  expect(placement, 'File access selector is outside the settings pane').not.toBeNull()
  expect(placement!.box.top).toBeGreaterThanOrEqual(placement!.clip.top - 1)
  expect(placement!.box.bottom).toBeLessThanOrEqual(placement!.clip.bottom + 1)
  expect(placement!.box.left).toBeGreaterThanOrEqual(placement!.clip.left - 1)
  expect(placement!.box.right).toBeLessThanOrEqual(placement!.clip.right + 1)
}

test.describe('a File access link in a chat reply', () => {
  test('locates the exact workspace control in place, without a write', async ({ browser }, testInfo) => {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } })
    const page = await context.newPage()
    await page.addInitScript(() => localStorage.setItem('ciao-theme', 'dark'))
    await isolate(page, `workspace-deep-links-desktop-${testInfo.workerIndex}`)
    const writes = watchWorkspaceWrites(page)
    await transcriptWithFileAccessLink(page)
    await boot(page, CHAT, COMPOSER)

    const link = page.locator('.message-wrap.assistant a', { hasText: 'File access for beta' })
    await expect(link).toHaveAttribute('href', TARGET)
    await expect(link).not.toHaveAttribute('target', /.*/)
    await page.locator(COMPOSER).fill(DRAFT)
    // A full page load would clear this marker; routing in place keeps it.
    await page.evaluate(() => { (window as unknown as { __kept?: boolean }).__kept = true })

    await link.click()
    await expectFileAccessTarget(page, 'beta')

    expect(context.pages()).toHaveLength(1)
    expect(await page.evaluate(() => (window as unknown as { __kept?: boolean }).__kept)).toBe(true)
    expect(writes).toEqual([])
    await page.screenshot({ path: testInfo.outputPath('desktop-dark-file-access.png'), fullPage: true })

    await page.goBack()
    await expect(page).toHaveURL(/\/chat\/alpha-chat-1$/)
    await expect(page.locator(COMPOSER)).toHaveValue(DRAFT)
  })

  test('a direct load reveals the control and reflows at 200% without horizontal scroll', async ({ browser }, testInfo) => {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } })
    const page = await context.newPage()
    await page.addInitScript(() => localStorage.setItem('ciao-theme', 'light'))
    await isolate(page, `workspace-deep-links-direct-${testInfo.workerIndex}`)
    const writes = watchWorkspaceWrites(page)

    await boot(page, TARGET, SETTINGS)
    await expectFileAccessTarget(page, 'beta')
    expect(writes).toEqual([])
    await page.screenshot({ path: testInfo.outputPath('desktop-light-direct.png'), fullPage: true })

    // 200% page zoom is indistinguishable from halving the CSS viewport
    // (browser-zoom.spec.ts): re-measure after the resize so the app has to
    // reflow, not merely have been born narrow.
    await page.setViewportSize({ width: 640, height: 450 })
    await expectFileAccessTarget(page, 'beta')
    const { overflow, culprits } = await horizontalOverflow(page)
    expect(
      overflow,
      `settings scrolls ${overflow}px sideways; widest unclipped: ${JSON.stringify(culprits)}`,
    ).toBeLessThanOrEqual(0)
    await page.screenshot({ path: testInfo.outputPath('desktop-light-zoomed.png'), fullPage: true })
    await context.close()
  })

  test('a short direct load puts the selector fully inside the pane', async ({ browser }, testInfo) => {
    const context = await browser.newContext({ viewport: { width: 640, height: 450 } })
    const page = await context.newPage()
    await page.addInitScript(() => localStorage.setItem('ciao-theme', 'light'))
    await isolate(page, `workspace-deep-links-short-${testInfo.workerIndex}`)
    const writes = watchWorkspaceWrites(page)

    await boot(page, TARGET, SETTINGS)
    await expectFileAccessTarget(page, 'beta')
    expect(writes).toEqual([])
    await page.screenshot({ path: testInfo.outputPath('desktop-light-short-direct.png'), fullPage: true })
    await context.close()
  })

  test('keyboard activation works on a phone with reduced motion and a 44px control', async ({ browser }, testInfo) => {
    const context = await browser.newContext({
      viewport: { width: 390, height: 844 },
      hasTouch: true,
      isMobile: true,
      reducedMotion: 'reduce',
    })
    const page = await context.newPage()
    await page.addInitScript(() => localStorage.setItem('ciao-theme', 'light'))
    await isolate(page, `workspace-deep-links-mobile-${testInfo.workerIndex}`)
    const writes = watchWorkspaceWrites(page)
    await transcriptWithFileAccessLink(page)
    await boot(page, CHAT, COMPOSER)

    const link = page.locator('.message-wrap.assistant a', { hasText: 'File access for beta' })
    await page.locator(COMPOSER).fill(DRAFT)
    // An Enter activation is a click with no pointer detail; it must route in
    // place exactly like a tap, not fall through to the browser.
    await link.focus()
    await expect(link).toBeFocused()
    await page.keyboard.press('Enter')

    await expectFileAccessTarget(page, 'beta')
    expect(context.pages()).toHaveLength(1)
    expect(writes).toEqual([])

    const select = page.locator('#workspace-file-access-beta')
    const box = await select.boundingBox()
    expect(box, 'File access select has no layout box').toBeTruthy()
    expect(box!.height).toBeGreaterThanOrEqual(44)
    expect(box!.width).toBeGreaterThanOrEqual(44)
    // Reduced motion keeps the focus ring but drops the transient tint.
    const row = page.locator('#workspace-file-access-row-beta')
    await expect(row).toHaveClass(/workspace-card--target/)
    expect(await row.evaluate(el => getComputedStyle(el).animationName)).toBe('none')

    const { overflow, culprits } = await horizontalOverflow(page)
    expect(
      overflow,
      `settings scrolls ${overflow}px sideways; widest unclipped: ${JSON.stringify(culprits)}`,
    ).toBeLessThanOrEqual(0)
    await page.screenshot({ path: testInfo.outputPath('mobile-light-reduced-motion.png'), fullPage: true })
    await context.close()
  })
})
