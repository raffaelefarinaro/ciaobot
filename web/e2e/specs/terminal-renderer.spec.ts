import { expect, test } from '@playwright/test'

/**
 * The terminal screen, in a real browser.
 *
 * jsdom has no xterm, no layout engine and no font metrics, so the unit
 * suite can only prove the contract around the screen. This spec proves
 * the screen itself: it mounts the real renderer on a bare div through the
 * harness page (a test-only page in its own build tree, never a product
 * route) and checks the four things a browser is the only judge of.
 *
 * The harness is a real renderer, not a stand-in: the bytes are written to
 * an xterm, the keystrokes come from an xterm, and the size is measured by
 * the fit addon against a real layout.
 */

const HARNESS = '/__fixture__/harness/index.html'
const TEXT = 'hello from the renderer'

test('the screen draws, gates input, resizes and disposes', async ({ page }) => {
  const consoleMessages: string[] = []
  const pageErrors: string[] = []
  page.on('console', (message) => {
    consoleMessages.push(message.text())
  })
  page.on('pageerror', (error) => {
    pageErrors.push(error.message)
  })

  await page.goto(HARNESS)
  await page.waitForFunction(() => Boolean(window.__terminal))

  // The bytes reach the screen, and the acknowledgement carries the
  // absolute end offset of the bytes that were just parsed.
  const first = await page.evaluate((text) => window.__terminal.write(text), TEXT)
  expect(first).toBe(TEXT.length)
  await expect(page.locator('#terminal')).toContainText(TEXT)

  const second = await page.evaluate(() => window.__terminal.write('!'))
  expect(second).toBe(TEXT.length + 1)
  expect(await page.evaluate(() => window.__terminal.acks())).toEqual([TEXT.length, TEXT.length + 1])

  // A keystroke reaches the shell only while input is enabled. The screen
  // starts with input off, so the first keystroke goes nowhere.
  await page.locator('#terminal').click()
  await page.keyboard.press('a')
  expect(await page.evaluate(() => window.__terminal.inputs())).toEqual([])

  await page.evaluate(() => window.__terminal.setInputEnabled(true))
  await page.keyboard.press('a')
  expect(await page.evaluate(() => window.__terminal.inputs())).toEqual([[97]])

  // A resize changes the window: the fit addon measures the host against
  // the font's cell size, and the engine has to be told the new size.
  const before = await page.evaluate(() => window.__terminal.size())
  expect(before.cols).toBeGreaterThan(0)
  await page.setViewportSize({ width: 640, height: 720 })
  await expect
    .poll(() => page.evaluate(() => window.__terminal.size()))
    .not.toEqual(before)

  // A disposed screen leaves nothing behind: the terminal's own DOM is
  // gone, and the observer and the listeners went with it.
  await page.evaluate(() => window.__terminal.dispose())
  expect(await page.evaluate(() => window.__terminal.disposed())).toBe(true)
  await expect(page.locator('#terminal .xterm')).toHaveCount(0)

  const loggedBefore = consoleMessages.length
  await page.setViewportSize({ width: 900, height: 720 })
  await page.keyboard.press('b')
  expect(consoleMessages.slice(loggedBefore)).toEqual([])
  expect(pageErrors).toEqual([])
})
