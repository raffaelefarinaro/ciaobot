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
  // The model part is on screen, not only in the DOM, at every width; a phone
  // shows the model alone (the provider is in the name), never "Provider ·".
  const modelPart = chip.locator('.home-intake-chip-label, .composer-chip-label').first()
  await expect(modelPart).toBeVisible()
  await expect(modelPart).toHaveText('Synthetic Model')
  // A long name may ellipsise on a phone, but at least ~8 characters show.
  expect((await modelPart.boundingBox())!.width).toBeGreaterThanOrEqual(48)
  const sep = chip.locator('.home-intake-chip-sep, .composer-chip-sep').first()
  const provider = chip.locator('.home-intake-chip-provider, .composer-chip-provider').first()
  expect(await sep.isVisible()).toBe(await provider.isVisible())
  expect(await provider.isVisible()).toBe((page.viewportSize()!.width) > 520)
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
  await page.keyboard.press('Escape')
  await expect(popover).toHaveCount(0)
  expect(await box(chip)).toEqual(closed)
}

for (const viewport of [{ width: 1280, height: 800 }, { width: 1280, height: 900 }, { width: 390, height: 844 }]) {
  test.describe(`model chip at ${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport, ...(viewport.width < 500 ? { isMobile: true, hasTouch: true } : {}) })

    test.beforeEach(async ({ page }, testInfo) => {
      await isolate(page, `model-chip-${viewport.width}-${viewport.height}-${testInfo.workerIndex}`)
    })

    test('on Home, and again after a workspace switch', async ({ page }) => {
      await boot(page, '/', BOOTED)
      const chip = page.locator('.home-intake-model-trigger')
      await checkChip(page, chip)
      // A switch drops the catalog; the chip reads the new workspace's
      // default without the picker being opened.
      const before = await box(chip)
      // The switcher is in the drawer on a phone, so read the active workspace
      // from where the app keeps it.
      const active = () => page.evaluate(() => localStorage.getItem('ciao-active-workspace'))
      const start = await active()
      await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur())
      await page.keyboard.press(start === 'beta' ? '1' : '2')
      await expect.poll(active).not.toBe(start)
      await expect(chip).toContainText('Synthetic Model')
      expect(await box(chip)).toEqual(before)
      await checkChip(page, chip)
    })

    test('in the chat composer', async ({ page }) => {
      await boot(page, '/chat/alpha-chat-1', COMPOSER)
      await checkChip(page, page.locator('.model-picker-wrap.composer-model .composer-chip'))
    })
  })
}
