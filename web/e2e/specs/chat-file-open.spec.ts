import { expect, test } from '@playwright/test'
import { boot, COMPOSER, horizontalOverflow, isolate } from '../support/app'

for (const mobile of [false, true]) {
  test(`chat-relative file opens on ${mobile ? 'mobile' : 'desktop with a failed pin'}`, async ({ page }, testInfo) => {
    await isolate(page, `chat-file-${testInfo.workerIndex}-${mobile}`)
    await page.setViewportSize(mobile ? { width: 390, height: 844 } : { width: 1280, height: 900 })
    const canonical = '/fixture/work/memory-vault/Workspace/draft.md'
    await page.route('**/api/chats/alpha-chat-1/messages*', route => route.fulfill({ json: [
      { role: 'user', content: 'Write a draft', turn_index: 0 },
      { role: 'assistant', content: 'Open `memory-vault/Workspace/draft.md`.', turn_index: 0 },
    ] }))
    await page.route('**/api/chats/alpha-chat-1/file-path?*', route => route.fulfill({ json: { path: canonical } }))
    await page.route('**/api/workspace-file?*', route => route.fulfill({ body: '# Workspace draft\n\nCorrect source workspace.', contentType: 'text/plain' }))
    let pinAttempts = 0
    await page.route('**/api/chats/alpha-chat-1', route => {
      pinAttempts++
      expect(route.request().postDataJSON().pin.path).toBe(canonical)
      return route.fulfill({ status: 404, json: { error: 'not found' } })
    })
    await boot(page, '/chat/alpha-chat-1', COMPOSER)
    await expect(page.locator('.startup-overlay')).toHaveCount(0)
    await page.locator('a[data-file-path="memory-vault/Workspace/draft.md"]').click()
    const viewer = page.locator('.fv-modal')
    await expect(viewer).toBeVisible()
    await expect(viewer).toContainText('Correct source workspace.')
    await expect(viewer.locator('.fv-subtitle')).toContainText(canonical)
    if (!mobile) {
      await expect(page.getByText('Could not update pinned file', { exact: true })).toBeVisible()
      expect(pinAttempts).toBe(1)
      await viewer.getByRole('button', { name: 'Pin to sidebar', exact: true }).click()
      await expect.poll(() => pinAttempts).toBe(2)
      await expect(viewer).toBeVisible()
    } else {
      expect(pinAttempts).toBe(0)
    }
    expect((await horizontalOverflow(page)).overflow).toBeLessThanOrEqual(0)
    await page.screenshot({ path: testInfo.outputPath('file-open.png'), fullPage: true })
  })
}
