import { expect, test, type Page } from '@playwright/test'
import { COMPOSER, boot, horizontalOverflow, isolate, settled } from '../support/app'

// Real layout is essential here: jsdom cannot establish a single visual line,
// slide motion, a 44px touch target, or overflow at phone width / browser zoom.
async function speak(page: Page, frames: unknown[]) {
  const status = await page.evaluate(async (frames) => {
    const response = await fetch('/__fixture__/chat-frame', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ chat_id: 'alpha-chat-1', frames }),
    })
    return response.status
  }, frames)
  expect(status).toBe(200)
}

for (const phone of [false, true]) {
  test(`live Working stays one line and expands with the keyboard (${phone ? 'phone' : 'desktop'})`, async ({ page }, info) => {
    await page.setViewportSize(phone ? { width: 390, height: 844 } : { width: 1280, height: 900 })
    if (phone) {
      const cdp = await page.context().newCDPSession(page)
      await cdp.send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 1 })
    }
    await isolate(page, `working-row-${info.workerIndex}-${phone}`)
    await boot(page, '/chat/alpha-chat-1', COMPOSER)
    await speak(page, [
      { type: 'tool_use', tool_use_id: 'read-1', tool_name: 'Read', tool_input: '/fixture/first.md' },
      { type: 'tool_use', tool_use_id: 'read-2', tool_name: 'Read', tool_input: '/fixture/second.md' },
    ])
    const row = page.locator('.trace-block.live .trace-summary')
    await expect(row).toBeVisible()
    await expect(row).toHaveAttribute('aria-expanded', 'false')
    await expect(row).toContainText('second.md')
    await expect(row.locator('.live-step-enter-active, .live-step-leave-active')).toHaveCount(0)
    await expect(page.locator('.trace-block.live .trace-body')).toHaveCount(0)
    const box = await row.boundingBox()
    expect(box!.height).toBeLessThanOrEqual(phone ? 48 : 40)
    if (phone) expect(box!.height).toBeGreaterThanOrEqual(44)
    expect((await horizontalOverflow(page)).overflow).toBeLessThanOrEqual(1)
    await page.screenshot({ path: info.outputPath('working-collapsed.png') })

    await row.focus()
    await page.keyboard.press('Enter')
    await expect(row).toHaveAttribute('aria-expanded', 'true')
    await expect(page.locator('.trace-block.live .trace-body')).toContainText('first.md')
    await expect(page.locator('.trace-block.live .trace-body')).toContainText('second.md')
    await settled(row)
    await page.screenshot({ path: info.outputPath('working-expanded.png') })
    await page.keyboard.press('Space')
    await expect(row).toHaveAttribute('aria-expanded', 'false')

    // Observe actual browser motion, rather than merely finding CSS class names.
    await row.evaluate((element) => {
      const preview = element.querySelector('.live-preview')!
      preview.setAttribute('data-saw-motion', 'false')
      const observer = new MutationObserver(() => {
        const moving = Array.from(preview.querySelectorAll('span')).some(span => {
          const transform = getComputedStyle(span).transform
          return transform !== 'none' && transform !== 'matrix(1, 0, 0, 1, 0, 0)'
        })
        if (moving) {
          preview.setAttribute('data-saw-motion', 'true')
          observer.disconnect()
        }
      })
      observer.observe(preview, { subtree: true, attributes: true, childList: true })
    })
    await speak(page, [{ type: 'tool_use', tool_use_id: 'read-3', tool_name: 'Read', tool_input: '/fixture/third.md' }])
    await expect(row.locator('.live-preview')).toHaveAttribute('data-saw-motion', 'true')
    await expect(row).toContainText('third.md')
    await expect(row.locator('.live-step-enter-active, .live-step-leave-active')).toHaveCount(0)

    await page.emulateMedia({ reducedMotion: 'reduce' })
    await speak(page, [{
      type: 'tool_use', tool_use_id: 'search-3', tool_name: 'Grep',
      tool_input: 'a-very-long-search-description-'.repeat(20),
    }])
    await expect(row).toContainText('Searching')
    await page.evaluate(() => { document.documentElement.style.zoom = '2' })
    await expect(row).toHaveAttribute('aria-expanded', 'false')
    expect((await horizontalOverflow(page)).overflow).toBeLessThanOrEqual(1)
    await row.focus()
    await page.keyboard.press('Enter')
    await expect(page.locator('.trace-block.live .trace-body')).toContainText('first.md')
  })
}
