import { expect, test, type Page } from '@playwright/test'
import { boot, isolate } from '../support/app'
import { careReports } from '../../src/lib/careReportFixture'

/**
 * The running glyph's arc must turn in a real engine. It runs under Chromium
 * and under WebKit (the engine of the Mac app's web view). The arc is an SVG
 * animation, not a CSS transform on an SVG child, where engines differ.
 * Reduced motion keeps it still.
 */
async function openRunning(page: Page, id: string) {
  await isolate(page, id)
  await page.route(/\/api\/care-report\?/, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ ...careReports.running, workspace: 'alpha', report_path: 'alpha/Workspace/Care-Report.md', unsure_path: 'alpha/Workspace/Unsure.md' }),
  }))
  await boot(page, '/memory/care', '.mc-step')
}

test('the running arc is animated and its timeline advances', async ({ page }, testInfo) => {
  await openRunning(page, `care-turn-${testInfo.workerIndex}-${testInfo.project.name}`)
  const arc = page.locator('.mc-summary .care-glyph-arc')
  await expect(arc).toBeVisible()
  // SMIL: the animation element is there and the SVG's timeline is advancing.
  await expect(arc.locator('animateTransform')).toHaveCount(1)
  const svg = page.locator('.mc-summary .care-glyph')
  expect(await svg.evaluate((el: SVGSVGElement) => el.animationsPaused())).toBe(false)
  const t0 = await svg.evaluate((el: SVGSVGElement) => el.getCurrentTime())
  await page.waitForTimeout(300)
  expect(await svg.evaluate((el: SVGSVGElement) => el.getCurrentTime())).toBeGreaterThan(t0)
})

test.describe('under reduced motion', () => {
  test.use({ reducedMotion: 'reduce' })

  test('the running arc holds still', async ({ page }, testInfo) => {
    await openRunning(page, `care-still-${testInfo.workerIndex}-${testInfo.project.name}`)
    const arc = page.locator('.mc-summary .care-glyph-arc')
    await expect(arc.locator('animateTransform')).toHaveCount(0)
  })
})
