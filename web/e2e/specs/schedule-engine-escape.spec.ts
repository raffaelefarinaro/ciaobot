import { expect, test } from '@playwright/test'
import { boot, isolate } from '../support/app'

for (const viewport of [{ width: 1280, height: 900 }, { width: 390, height: 844 }]) {
  test(`Escape closes the engine picker before its editor at ${viewport.width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport)
    await isolate(page, `engine-escape-${testInfo.workerIndex}-${testInfo.testId}`)
    await boot(page, '/schedules/alpha-daily-brief', 'text=Daily project briefing')
    await page.getByRole('button', { name: 'Edit engine' }).click()
    const editor = page.locator('.card-editing')
    const opener = editor.locator('.model-selector__trigger')
    await opener.click()
    await expect(editor.locator('.model-selector__popover')).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(editor.locator('.model-selector__popover')).toHaveCount(0)
    await expect(opener).toBeFocused()
    await page.keyboard.press('Escape')
    await expect(editor).toHaveCount(0)
  })
}

test('Escape dismisses a dirty engine confirmation without replacing it', async ({ page }, testInfo) => {
  await isolate(page, `engine-confirm-${testInfo.workerIndex}-${testInfo.testId}`)
  await page.route('**/api/models?**', route => route.fulfill({
    json: { models: ['different-model'], default: '', provider_models: { claude: ['different-model'] } },
  }))
  await boot(page, '/schedules/alpha-daily-brief', 'text=Daily project briefing')
  await page.getByRole('button', { name: 'Edit engine' }).click()
  const editor = page.locator('.card-editing')
  await editor.locator('.model-selector__trigger').click()
  await editor.getByRole('option').first().click()
  await expect(editor.locator('.dirty-flag')).toBeVisible()
  await page.keyboard.press('Escape')
  const confirm = page.getByRole('dialog').filter({ hasText: 'Discard your unsaved changes?' })
  await expect(confirm).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(confirm).toHaveCount(0)
  await expect(editor.locator('.dirty-flag')).toBeVisible()
})
