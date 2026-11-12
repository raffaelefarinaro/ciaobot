import { expect, test, type Locator, type Page } from '@playwright/test'
import { COMPOSER, boot, fixtureWrites, isolate, settled } from '../support/app'

/**
 * Two chats side by side, in a real browser.
 *
 * The journey is the one a person takes: hover a sidebar row, open it beside
 * the chat on screen, type in its own composer, drag the divider, close it
 * with Esc, and find it back after a reload. The layout's rules (one slot,
 * newest takes it, 940px floor) are unit-tested; this proves they hold on a
 * real layout engine with real pointer and focus.
 */
const CHAT_PATH = '/chat/alpha-chat-1'
const SECOND_TITLE = 'alpha second conversation'

/** Drag the slot's divider to an absolute x, the way a person drags it. */
async function dragDivider(page: Page, divider: Locator, toX: number): Promise<void> {
  const box = (await divider.boundingBox())!
  const y = box.y + box.height / 2
  await page.mouse.move(box.x + box.width / 2, y)
  await page.mouse.down()
  await page.mouse.move(toX, y, { steps: 8 })
  await page.mouse.up()
}

test.use({ viewport: { width: 1440, height: 900 } })

test.beforeEach(async ({ page }, testInfo) => {
  await isolate(page, `side-by-side-${testInfo.workerIndex}`)
})

test('a chat opens beside the open one and is remembered across a reload', async ({ page }) => {
  await boot(page, CHAT_PATH, COMPOSER)

  const row = page.locator('.chat-item', { hasText: SECOND_TITLE })
  await row.hover()
  await row.locator('.chat-beside-btn').click()

  const tile = page.locator('.chat-split-side--chat')
  await expect(tile).toBeVisible()
  await expect(tile.locator('.chat-title')).toHaveText(SECOND_TITLE)
  await expect(page.locator('.chat-split-resizer')).toBeVisible()
  await expect(tile.locator('textarea.chat-input')).toBeFocused()
  // Opening it reads it, like any chat on screen.
  await expect.poll(() => fixtureWrites(page)).toContainEqual({ method: 'POST', path: '/api/chats/alpha-chat-2/read' })

  await page.reload()
  await expect(page.locator('.chat-split-side--chat .chat-title')).toHaveText(SECOND_TITLE)
})

test('the pane with the keyboard wears the ring, and Esc closes the second pane', async ({ page }) => {
  await boot(page, CHAT_PATH, COMPOSER)
  const row = page.locator('.chat-item', { hasText: SECOND_TITLE })
  await row.hover()
  await row.locator('.chat-beside-btn').click()
  await expect(page.locator('.chat-split-side--chat')).toBeVisible()

  await page.locator('.chat-main > .chat-body .messages').click()
  await expect(page.locator('.chat-body--pane-focused')).toBeVisible()

  await page.locator('.chat-split-side--chat .messages').click()
  await expect(page.locator('.chat-split-side--focused')).toBeVisible()

  await page.locator('.chat-split-side--chat textarea.chat-input').focus()
  await page.keyboard.press('Escape')
  await expect(page.locator('.chat-split-side--chat')).toHaveCount(0)
  await expect(page).toHaveURL(new RegExp(`${CHAT_PATH}$`))
})

test('a narrow pane shows one chat', async ({ page }) => {
  await boot(page, CHAT_PATH, COMPOSER)
  const row = page.locator('.chat-item', { hasText: SECOND_TITLE })
  await row.hover()
  await row.locator('.chat-beside-btn').click()
  await expect(page.locator('.chat-split-side--chat')).toBeVisible()

  // The sidebar takes its share: the pane is under 940px.
  await page.setViewportSize({ width: 1000, height: 900 })
  await expect(page.locator('.chat-split-side--chat')).toHaveCount(0)

  await page.setViewportSize({ width: 1440, height: 900 })
  await expect(page.locator('.chat-split-side--chat')).toBeVisible()
})

test('the panes keep their floors when the divider is dragged', async ({ page }) => {
  await boot(page, CHAT_PATH, COMPOSER)
  const row = page.locator('.chat-item', { hasText: SECOND_TITLE })
  await row.hover()
  await row.locator('.chat-beside-btn').click()
  const tile = page.locator('.chat-split-side--chat')
  await expect(tile).toBeVisible()

  const divider = page.locator('.chat-split-resizer')
  const chatColumn = page.locator('.chat-main > .chat-body')
  await dragDivider(page, divider, 1400)
  await settled(tile)
  expect((await tile.boundingBox())!.width).toBeGreaterThanOrEqual(360)
  expect((await chatColumn.boundingBox())!.width).toBeGreaterThanOrEqual(360)

  await dragDivider(page, divider, 40)
  await settled(tile)
  expect((await tile.boundingBox())!.width).toBeGreaterThanOrEqual(360)
  expect((await chatColumn.boundingBox())!.width).toBeGreaterThanOrEqual(360)
})

test('the keyboard opens a chat beside and moves between the panes', async ({ page }) => {
  await boot(page, CHAT_PATH, COMPOSER)
  await page.locator(COMPOSER).first().click()

  await page.keyboard.press('Control+Backslash')
  const tile = page.locator('.chat-split-side--chat')
  await expect(tile).toBeVisible()

  await page.keyboard.press('Control+Alt+BracketLeft')
  // The tile sits inside .chat-body too; the main composer is the first in DOM order.
  await expect(page.locator('.chat-body textarea.chat-input').first()).toBeFocused()
  await expect(page.locator('.chat-body--pane-focused')).toBeVisible()

  await page.keyboard.press('Control+Alt+BracketRight')
  await expect(tile.locator('textarea.chat-input')).toBeFocused()

  await page.keyboard.press('Control+Backslash')
  await expect(page.locator('.chat-split-side--chat')).toHaveCount(0)
})

test('the pane with the keyboard is the selected chat in the sidebar', async ({ page }) => {
  await boot(page, CHAT_PATH, COMPOSER)
  const second = page.locator('.chat-item', { hasText: SECOND_TITLE })
  await second.hover()
  await second.locator('.chat-beside-btn').click()
  const tile = page.locator('.chat-split-side--chat')
  await expect(tile).toBeVisible()
  await expect(second).toHaveClass(/\bactive\b/)
  // The second pane wears the same header as a chat open on its own.
  await expect(tile.locator('.chat-title-row .title-rail')).toBeVisible()

  // The main pane's chat, picked from the sidebar, takes the keyboard back.
  await page.locator('.chat-item.active').first().waitFor()
  const first = page.locator('.chat-item').filter({ hasNotText: SECOND_TITLE }).filter({ has: page.locator('[class*="title"]') }).first()
  await page.locator('.chat-main > .chat-body .messages').click()
  await expect(second).not.toHaveClass(/\bactive\b/)
  await expect(first).toHaveClass(/\bactive\b/)
  await tile.locator('.messages').click()
  await first.click()
  await expect(page.locator('.chat-body--pane-focused')).toBeVisible()
  await expect(page).toHaveURL(new RegExp(`${CHAT_PATH}$`))
  await expect(tile.locator('.chat-title')).toHaveText(SECOND_TITLE)
})
