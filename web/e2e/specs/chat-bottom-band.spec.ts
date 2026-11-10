import { expect, test } from '@playwright/test'
import { COMPOSER, boot, isolate } from '../support/app'

test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

/**
 * The composer has to end on the screen's edge, not a dead band's: the layout
 * is the full viewport with the keyboard closed, and the surface sits 8px plus
 * the bottom safe inset above it. Only a real layout engine can prove this;
 * jsdom reports every box as 0x0.
 */
test('the phone chat fills the screen and the composer sits on the safe-area edge', async ({ page }) => {
  await isolate(page, 'bottom-band')
  await boot(page, '/chat/alpha-chat-1', COMPOSER)
  // The fixture chat starts empty; the transcript gap needs a last row.
  await page.request.post('/__fixture__/transcript', { data: {} })
  await page.reload()
  await expect(page.locator('.messages-content .message-wrap').last()).toBeVisible()
  // The transcript pins itself to the bottom a frame or so after the last row
  // renders; measuring before that read the row ~46px low (#2777). The gap
  // under test is the pinned layout, so wait for the pin.
  await expect.poll(() => page.locator('.messages').evaluate(el => el.scrollHeight - el.clientHeight - el.scrollTop)).toBeLessThanOrEqual(1)

  const measured = await page.evaluate(async () => {
    const root = document.documentElement
    const composer = document.querySelector('.composer-surface')
    const lastRow = document.querySelector('.messages-content > :last-child')
    const layout = document.querySelector('.chat-layout')
    if (!composer || !lastRow || !layout) {
      throw new Error('chat composer, transcript row or layout missing')
    }
    const viewportHeight = root.clientHeight
    const composerBox = composer.getBoundingClientRect()
    const before = {
      appHeight: getComputedStyle(root).getPropertyValue('--app-h').trim(),
      layoutHeight: layout.getBoundingClientRect().height,
      viewportHeight,
      bottomGap: viewportHeight - composerBox.bottom,
      rowGap: composerBox.top - lastRow.getBoundingClientRect().bottom,
    }
    // The inset is a CSS variable this spec owns; no emulated media needed.
    root.style.setProperty('--safe-bottom', '34px')
    await new Promise<void>(resolve => requestAnimationFrame(() => resolve()))
    const withInset = viewportHeight - composer.getBoundingClientRect().bottom
    return { ...before, withInset }
  })

  // Closed keyboard: the layout is the viewport itself, not a latched pixel height.
  expect(measured.appHeight).toBe('')
  expect(Math.abs(measured.layoutHeight - measured.viewportHeight)).toBeLessThanOrEqual(1)
  // Browser mode, safe inset 0: 8px between the surface and the bottom edge.
  expect(Math.abs(measured.bottomGap - 8)).toBeLessThanOrEqual(1)
  // With a 34px inset the same gap carries it: 8 + 34.
  expect(Math.abs(measured.withInset - 42)).toBeLessThanOrEqual(1)
  // The transcript keeps 16px above the composer surface.
  expect(Math.abs(measured.rowGap - 16)).toBeLessThanOrEqual(2)
})
