import { expect, test } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { gzipSync } from 'node:zlib'
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
      // The boot also GETs the chat detail; only the pin write (PATCH) is mocked.
      if (route.request().method() !== 'PATCH') return route.fallback()
      pinAttempts++
      expect(route.request().postDataJSON().pin.path).toBe(canonical)
      return route.fulfill({ status: 404, json: { error: 'not found' } })
    })
    await boot(page, '/chat/alpha-chat-1', COMPOSER)
    await expect(page.locator('.startup-overlay')).toHaveCount(0)
    const fileLink = page.locator('a[data-file-path="memory-vault/Workspace/draft.md"]')
    await fileLink.click()
    const viewer = page.locator('.fv-modal')
    await expect(viewer).toBeVisible()
    await expect(viewer).toContainText('Correct source workspace.')
    await expect(viewer.locator('.fv-subtitle')).toContainText(canonical)
    await expect.poll(() => viewer.evaluate(el => el.contains(document.activeElement))).toBe(true)
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
    await page.keyboard.press('Escape')
    await expect(viewer).toHaveCount(0)
    await expect(fileLink).toBeFocused()
    await page.screenshot({ path: testInfo.outputPath('chat-after-close.png'), fullPage: true })
    await expect(page).toHaveURL(/\/chat\/alpha-chat-1$/)
    // First lazy mount and subsequent opens must share the same focus contract.
    await fileLink.press('Enter')
    await expect(viewer).toBeVisible()
    await expect.poll(() => viewer.evaluate(el => el.contains(document.activeElement))).toBe(true)
    await page.keyboard.press('Escape')
    await expect(viewer).toHaveCount(0)
    await expect(fileLink).toBeFocused()
  })
}

test('Back closes the file viewer and leaves the chat where it was', async ({ browser }, testInfo) => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true })
  const page = await context.newPage()
  try {
    await isolate(page, `chat-file-back-${testInfo.workerIndex}`)
    const canonical = '/fixture/work/memory-vault/Workspace/draft.md'
    await page.route('**/api/chats/alpha-chat-1/messages*', route => route.fulfill({ json: [
      { role: 'user', content: 'Write a draft', turn_index: 0 },
      { role: 'assistant', content: 'Open `memory-vault/Workspace/draft.md`.', turn_index: 0 },
    ] }))
    await page.route('**/api/chats/alpha-chat-1/file-path?*', route => route.fulfill({ json: { path: canonical } }))
    await page.route('**/api/workspace-file?*', route => route.fulfill({ body: '# Workspace draft\n\nCorrect source workspace.', contentType: 'text/plain' }))
    await boot(page, '/chat/alpha-chat-1', COMPOSER)
    await expect(page.locator('.startup-overlay')).toHaveCount(0)
    const fileLink = page.locator('a[data-file-path="memory-vault/Workspace/draft.md"]')
    const viewer = page.locator('.fv-modal')
    const marker = () => page.evaluate(() => (history.state?.ciaoOverlay ?? null) as string | null)

    const lengthBefore = await page.evaluate(() => history.length)
    await fileLink.click()
    await expect(viewer).toBeVisible()
    await expect.poll(() => page.evaluate(() => history.length)).toBe(lengthBefore + 1)

    await page.goBack()
    await expect(viewer).toHaveCount(0)
    await expect(page).toHaveURL(/\/chat\/alpha-chat-1$/)
    await expect(fileLink).toBeFocused()

    await fileLink.click()
    await expect(viewer).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(viewer).toHaveCount(0)
    await expect.poll(marker).toBeNull()

    await page.goForward().catch(() => null)
    await expect(viewer).toHaveCount(0)
    await expect(page).toHaveURL(/\/chat\/alpha-chat-1$/)
    await expect.poll(marker).toBeNull()
  } finally {
    await context.close()
  }
})

test('Home loads chat and file code only when the user needs it', async ({ page }, testInfo) => {
  await isolate(page, `optional-code-${testInfo.workerIndex}`)
  const optionalRequests: string[] = []
  const homeAssets = new Set<string>()
  page.on('request', request => {
    const pathname = new URL(request.url()).pathname
    if (/^\/assets\/.*\.(?:js|css)$/.test(pathname)) homeAssets.add(pathname)
    if (/\/assets\/(?:ChatPanel|FileViewerModal|PinnedFilePanel)-.*\.js$/.test(pathname)) {
      optionalRequests.push(pathname)
    }
  })
  await boot(page)
  expect(optionalRequests).toEqual([])
  // The fixture does not compress responses. Measure the actual build files
  // to keep this byte budget independent of the browser or runner's speed.
  const compressedHomeBytes = [...homeAssets].reduce((total, path) => {
    const asset = new URL(`../../../ciao/web/static${path}`, import.meta.url)
    return total + gzipSync(readFileSync(asset)).byteLength
  }, 0)
  // 280,000 bytes (~273 KiB) since 2026-10-26. It was 270,000 earlier that day,
  // 260,000 from 2026-10-22,
  // before that 250,000, set when the page-load work landed. The notification
  // inbox bell (#2077) is on every page, so its store and projection ship in
  // the shell chunk Home loads: origin/develop measured 257,553 compressed
  // bytes without it and this build measures 263,694, so the inbox costs about
  // 6.1 KB. None of it is optional (the bell count must work on every page,
  // and the row view is already a lazy chunk), which leaves roughly 6 KB of
  // headroom. The shared drag engine (useDragSort) that the sidebar
  // mounts on every page then took the build from 263,694 to 270,564 (#2079),
  // so the budget moved to 280,000. Raise only with a measurement, not to
  // silence a regression.
  expect(compressedHomeBytes).toBeLessThan(280_000)
  await page.screenshot({ path: testInfo.outputPath('home.png'), fullPage: true })

  await boot(page, '/chat/alpha-chat-1', COMPOSER)
  expect(optionalRequests.some(path => path.includes('/ChatPanel-'))).toBe(true)
  expect(optionalRequests.some(path => /\/(?:FileViewerModal|PinnedFilePanel)-/.test(path))).toBe(false)
  await expect(page.locator('.messages')).toHaveAttribute('aria-busy', 'false')
  await expect(page.locator('.history-skeleton-stack')).toHaveCount(0)
  await page.screenshot({ path: testInfo.outputPath('chat.png'), fullPage: true })
})
