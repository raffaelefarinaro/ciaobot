import { expect, test } from '@playwright/test'
import { COMPOSER, boot, isolate } from '../support/app'

/**
 * The sidebar never scrolls sideways. Idle project rows collapse their ⋯ and +
 * buttons to zero width; without clipping, their icon and padding spilled
 * about 10px past the row and gave the chat list a horizontal scroll.
 */
test.use({ viewport: { width: 1440, height: 900 } })

test('the sidebar chat list has nothing wider than itself, idle or hovered', async ({ page }) => {
  await isolate(page, 'sidebar-overflow')
  await boot(page, '/chat/alpha-chat-1', COMPOSER)

  const overflow = () => page.locator('.chats-scroll-area').evaluate(el => el.scrollWidth - el.clientWidth)
  expect(await overflow(), 'idle rows').toBeLessThanOrEqual(0)

  await page.locator('.project-header').first().hover()
  await expect(page.locator('.project-header').first().locator('.add-chat-btn')).toBeVisible()
  expect(await overflow(), 'a hovered project row').toBeLessThanOrEqual(0)
})
