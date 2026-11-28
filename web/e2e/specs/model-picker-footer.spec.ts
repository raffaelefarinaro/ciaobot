import { expect, test, type Page } from '@playwright/test'
import { COMPOSER, boot, isolate, TOUCH_FLOOR } from '../support/app'

// The composer model picker's footer controls, chosen the way a person does:
// with a mouse on a desktop and a tap on a phone. A pick keeps the picker
// open and puts focus back on the control's button, as Enter does.

async function openPicker(page: Page) {
  await page.locator('.model-picker-wrap.composer-model .composer-chip').click()
  const picker = page.locator('.model-selector__popover')
  await expect(picker).toBeVisible()
  return picker
}

test.describe('model picker footer with a mouse', () => {
  test.use({ viewport: { width: 1280, height: 900 } })

  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `model-picker-footer-mouse-${testInfo.workerIndex}`)
    await boot(page, '/chat/alpha-chat-1', COMPOSER)
  })

  test('picking a permission mode keeps the picker open and focus on the button', async ({ page }) => {
    const picker = await openPicker(page)
    const button = picker.locator('.permission-mode-trigger')
    for (const name of ['Plan', 'Bypass', 'Manual']) {
      await button.click()
      await page.getByRole('menuitemradio', { name: new RegExp(name) }).click()
      await expect(page.getByRole('menuitemradio')).toHaveCount(0)
      await expect(picker).toBeVisible()
      await expect(button).toContainText(name)
      await expect(button).toBeFocused()
    }
  })

  test('shows Thinking as one segmented row on a wide footer', async ({ page }) => {
    const picker = await openPicker(page)
    await expect(picker.locator('.thinking-levels__chips')).toBeVisible()
    await expect(picker.locator('.thinking-levels__menu')).toBeHidden()
  })
})

test.describe('model picker footer on a phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `model-picker-footer-touch-${testInfo.workerIndex}`)
    await boot(page, '/chat/alpha-chat-1', COMPOSER)
  })

  test('a tapped permission mode keeps the sheet open and focus on the button', async ({ page }) => {
    const picker = await openPicker(page)
    const button = picker.locator('.permission-mode-trigger')
    await button.tap()
    await page.getByRole('menuitemradio', { name: /Plan/ }).tap()
    await expect(page.getByRole('menuitemradio')).toHaveCount(0)
    await expect(picker).toBeVisible()
    await expect(button).toContainText('Plan')
    await expect(button).toBeFocused()
  })

  test('Thinking is a 44px menu button that never scrolls sideways', async ({ page }) => {
    const picker = await openPicker(page)
    await expect(picker.locator('.thinking-levels__chips')).toBeHidden()
    const thinking = picker.locator('.thinking-levels__menu')
    await expect(thinking).toBeVisible()
    const box = (await thinking.boundingBox())!
    expect(box.height).toBeGreaterThanOrEqual(TOUCH_FLOOR)
    expect(await picker.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true)
    await thinking.tap()
    const levels = page.getByRole('menuitemradio')
    await expect(levels.first()).toBeVisible()
    await page.getByRole('menuitemradio', { name: 'High' }).tap()
    await expect(levels).toHaveCount(0)
    // A Thinking pick is saved and closes the picker, as a segment tap does.
    await expect(picker).toBeHidden()
    await expect(page.locator('.model-picker-wrap.composer-model .composer-chip')).toContainText('think:high')
  })
})
