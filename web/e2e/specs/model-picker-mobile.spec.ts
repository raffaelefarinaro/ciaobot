import { expect, test } from '@playwright/test'
import { COMPOSER, boot, isolate } from '../support/app'

test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

test.describe('mobile chat model picker', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `model-picker-mobile-${testInfo.workerIndex}`)
    await boot(page, '/chat/alpha-chat-1', COMPOSER)
  })

  async function openPicker(page: import('@playwright/test').Page) {
    await page.locator('.model-picker-wrap.composer-model .composer-chip').click()
    const picker = page.locator('.model-selector__popover')
    await expect(picker).toBeVisible()
    return picker
  }

  test('does not dismiss on an inside tap and closes from its visible 44px control', async ({ page }) => {
    const picker = await openPicker(page)
    await picker.locator('.model-selector__search').click()
    await expect(picker).toBeVisible()

    const close = picker.getByRole('button', { name: 'Close model picker' })
    const box = (await close.boundingBox())!
    expect(box.width).toBeGreaterThanOrEqual(44)
    expect(box.height).toBeGreaterThanOrEqual(44)
    await close.click()
    await expect(picker).toBeHidden()
    await expect(page.locator('.model-picker-wrap.composer-model .composer-chip')).toBeFocused()
  })

  test('closes when the phone top bar or backdrop is tapped', async ({ page }) => {
    let picker = await openPicker(page)
    await page.mouse.click(20, 24)
    await expect(picker).toBeHidden()

    picker = await openPicker(page)
    await page.mouse.click(4, 400)
    await expect(picker).toBeHidden()
  })

  test('keeps the sheet within the keyboard-reduced visible viewport', async ({ page }) => {
    const picker = await openPicker(page)
    // The fixture browser has no native soft keyboard; shrink the layout
    // viewport and publish the measured height exactly as viewport.ts does.
    await page.setViewportSize({ width: 390, height: 420 })
    await page.evaluate(() => document.documentElement.style.setProperty('--app-h', '420px'))
    const box = (await picker.boundingBox())!

    expect(box.y).toBeGreaterThanOrEqual(61)
    expect(box.y + box.height).toBeLessThanOrEqual(420)
  })
})
