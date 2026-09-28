import { test, expect } from '@playwright/test'
import { fileURLToPath } from 'node:url'

test('marketing memory story advances, resets, and keeps keyboard access', async ({ page }) => {
  await page.goto(`file://${fileURLToPath(new URL('../../../site/index.html', import.meta.url))}`)
  const board = page.locator('[data-memory-demo]')
  const action = board.locator('.memory-action')

  await expect(board).toHaveAttribute('data-step', '0')
  await expect(board.locator('.memory-paper-line')).toBeHidden()
  await action.focus()
  await page.keyboard.press('Enter')
  await expect(board).toHaveAttribute('data-step', '1')
  await expect(board.locator('.memory-paper-line')).toBeVisible()
  await expect(board.locator('.memory-answer')).toBeHidden()

  await action.click()
  await expect(board).toHaveAttribute('data-step', '2')
  await expect(board.locator('.memory-answer')).toBeVisible()
  await expect(board.locator('[role="status"]')).toContainText('saved context')

  await action.click()
  await expect(board).toHaveAttribute('data-step', '0')
  await expect(board.locator('.memory-paper-line')).toBeHidden()

  await page.setViewportSize({ width: 390, height: 844 })
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await action.click()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)
  expect((await action.boundingBox())!.height).toBeGreaterThanOrEqual(44)
  expect(await board.locator('.memory-paper-line').evaluate(node => getComputedStyle(node).animationName)).toBe('none')
})
