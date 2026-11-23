import { expect, test } from '@playwright/test'
import { boot, COMPOSER, isolate, openChatDetails, quiesce } from '../support/app'

test.use({ isMobile: true, hasTouch: true })

test('repository changes controls do not overlap on a phone or at 200% zoom', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await isolate(page, 'worktree-changes-controls')
  await page.route('**/api/chats/*/worktree-changes', route => route.fulfill({
    json: { state: 'ready', source: 'worktree', staged: '', unstaged: '', untracked: [], truncated: false },
  }))
  await boot(page, '/chat/alpha-chat-1', COMPOSER)
  await openChatDetails(page)
  const section = page.locator('.chat-repo-section')
  await expect(section).toBeVisible()
  await section.getByRole('button', { name: 'View changes' }).click()
  const hide = section.getByRole('button', { name: 'Hide changes' })
  const refresh = section.getByRole('button', { name: 'Refresh' })
  await expect(refresh).toBeVisible()

  for (const width of [390, 195]) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 422 })
    await quiesce(page)
    const hideBox = await hide.boundingBox()
    const refreshBox = await refresh.boundingBox()
    expect(hideBox).not.toBeNull()
    expect(refreshBox).not.toBeNull()
    const overlaps = hideBox!.x < refreshBox!.x + refreshBox!.width
      && refreshBox!.x < hideBox!.x + hideBox!.width
      && hideBox!.y < refreshBox!.y + refreshBox!.height
      && refreshBox!.y < hideBox!.y + hideBox!.height
    expect(overlaps, `Hide changes overlaps Refresh at ${width}px`).toBe(false)
    expect(hideBox!.width, 'Hide changes keeps a usable touch target').toBeGreaterThanOrEqual(44)
    expect(hideBox!.height, 'Hide changes keeps a usable touch target').toBeGreaterThanOrEqual(44)
  }
})
