import { expect, test, type Locator, type Page } from '@playwright/test'
import { BOOTED, COMPOSER, boot, isolate } from '../support/app'

// The model chip reads "<provider> · <model>" and keeps its box when its
// picker opens; the picker opens at its designed width (520px, or the room
// there is), whatever the chip's own width.

async function box(locator: Locator) {
  const rect = (await locator.boundingBox())!
  return { x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height) }
}

async function checkChip(page: Page, chip: Locator) {
  await expect(chip).toContainText('Claude Code ·')
  await expect(chip).not.toContainText(/default/i)
  // The catalog read on mount has landed when the model part is named.
  await expect(chip).toContainText('Synthetic Model')
  const closed = await box(chip)
  await chip.click()
  const popover = page.locator('.model-selector__popover')
  await expect(popover).toBeVisible()
  expect(await box(chip)).toEqual(closed)
  const width = await popover.evaluate(el => el.getBoundingClientRect().width)
  const room = await page.evaluate(() => window.innerWidth - 24)
  expect(width).toBeGreaterThanOrEqual(Math.min(520, room) - 1)
  const rect = (await popover.boundingBox())!
  expect(rect.y + rect.height).toBeLessThanOrEqual(page.viewportSize()!.height)
  await expect(popover.locator('.model-selector__item.ms-item--active')).toContainText('Synthetic Model')
  await expect(popover).not.toContainText(/\bdefault\b/i)
}

for (const viewport of [{ width: 1280, height: 800 }, { width: 1280, height: 900 }, { width: 390, height: 844 }]) {
  test.describe(`model chip at ${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport, ...(viewport.width < 500 ? { isMobile: true, hasTouch: true } : {}) })

    test.beforeEach(async ({ page }, testInfo) => {
      await isolate(page, `model-chip-${viewport.width}-${viewport.height}-${testInfo.workerIndex}`)
    })

    test('on Home', async ({ page }) => {
      await boot(page, '/', BOOTED)
      await checkChip(page, page.locator('.home-intake-model-trigger'))
    })

    test('in the chat composer', async ({ page }) => {
      await boot(page, '/chat/alpha-chat-1', COMPOSER)
      await checkChip(page, page.locator('.model-picker-wrap.composer-model .composer-chip'))
    })
  })
}
