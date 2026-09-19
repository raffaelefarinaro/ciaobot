import { expect, test } from '@playwright/test'
import { BOOTED, COMPOSER, boot, horizontalOverflow, isolate } from '../support/app'

/**
 * Why this is a browser test and not a vitest one.
 *
 * web/README.md is explicit that browser pinch/page zoom must stay enabled and
 * must not be worked around with `user-scalable=no` or `maximum-scale=1`, and
 * that the in-app font scale is an addition to it, not a replacement. Both
 * claims are about how a real engine reflows the page; jsdom applies no CSS,
 * so it can neither zoom nor notice that zooming broke the layout.
 */
test.describe('zoom and text scaling', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `zoom-${testInfo.workerIndex}`)
  })

  test('the viewport meta does not disable pinch zoom', async ({ page }) => {
    await boot(page)
    const content = await page.locator('meta[name="viewport"]').getAttribute('content')
    expect(content).toBeTruthy()
    expect(content).not.toContain('user-scalable=no')
    expect(content).not.toMatch(/maximum-scale\s*=\s*1(\D|$)/)
  })

  test('page zoom at 200% does not break the layout', async ({ page }) => {
    await boot(page)

    // Chromium's page zoom is not scriptable, but it is indistinguishable from
    // halving the CSS viewport: at 200% a 780px-wide window lays out as 390
    // CSS px. Re-measuring after the resize is what makes this a zoom test
    // rather than a second narrow-viewport test — the app must reflow, not
    // merely have been born small.
    await page.setViewportSize({ width: 1280, height: 900 })
    await expect(page.locator(BOOTED).first()).toBeVisible()
    await page.setViewportSize({ width: 640, height: 450 })
    await expect(page.locator(BOOTED).first()).toBeVisible()

    const { overflow, culprits } = await horizontalOverflow(page)
    expect(
      overflow,
      `zoomed layout scrolls ${overflow}px sideways; widest unclipped: ${JSON.stringify(culprits)}`,
    ).toBeLessThanOrEqual(0)
  })

  test('the largest in-app font scale keeps the composer usable', async ({ page }) => {
    // 1.5 is the top of the range Settings > Appearance offers. Setting it
    // before boot is how a returning user arrives, since main.ts restores the
    // scale from localStorage before Vue mounts.
    await page.addInitScript(() => {
      try { localStorage.setItem('ciao-font-scale', '1.5') } catch { /* blocked */ }
    })
    await boot(page, '/chat/alpha-chat-1', COMPOSER)
    const composer = page.locator(COMPOSER)

    // The composer must stay inside the viewport, not slide under it: a fixed
    // input bar that the enlarged type pushes off-screen is unreachable,
    // because scrolling does not move it.
    const box = await composer.boundingBox()
    const height = await page.evaluate(() => window.innerHeight)
    expect(box).not.toBeNull()
    expect(box!.y + box!.height).toBeLessThanOrEqual(height + 1)

    const { overflow, culprits } = await horizontalOverflow(page)
    expect(
      overflow,
      `scaled layout scrolls ${overflow}px sideways; widest unclipped: ${JSON.stringify(culprits)}`,
    ).toBeLessThanOrEqual(0)
  })
})
