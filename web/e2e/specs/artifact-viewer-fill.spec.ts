import { expect, test } from '@playwright/test'
import { boot, COMPOSER, isolate } from '../support/app'

/**
 * An .html artifact opened from a phone transcript must fill the viewer body.
 *
 * `.fv-body` is a block box for every other file kind, so the artifact's
 * column-flex root could not resolve `flex: 1` and the frame fell back to its
 * 320px floor, leaving the rest of the modal as dead space (#2311). jsdom lays
 * nothing out, so only a real browser can assert the geometry.
 */
test.use({
  viewport: { width: 430, height: 932 },
  isMobile: true,
  hasTouch: true,
  deviceScaleFactor: 3,
})

const HTML_DOC = [
  '<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">',
  '<style>body{margin:0;padding:16px;font:16px/1.5 -apple-system,sans-serif}</style></head>',
  '<body><h1>Glance</h1><p>' + 'Artifact body text that must have room to breathe. '.repeat(40) + '</p></body></html>',
].join('')

test('the artifact frame fills the phone viewer', async ({ page }, testInfo) => {
  await isolate(page, `artifact-fill-${testInfo.workerIndex}`)
  const canonical = '/fixture/work/memory-vault/Workspace/glance.html'
  await page.route('**/api/chats/alpha-chat-1/messages*', route => route.fulfill({ json: [
    { role: 'user', content: 'Build a glance page', turn_index: 0 },
    { role: 'assistant', content: 'Open [glance](memory-vault/Workspace/glance.html).', turn_index: 0 },
  ] }))
  await page.route('**/api/chats/alpha-chat-1/file-path?*', route => route.fulfill({ json: { path: canonical } }))
  await page.route('**/api/workspace-file?*', route => route.fulfill({ body: HTML_DOC, contentType: 'text/html' }))
  await page.route('**/api/workspace-html?*', route => route.fulfill({ body: HTML_DOC, contentType: 'text/html' }))

  await boot(page, '/chat/alpha-chat-1', COMPOSER)
  await page.locator('a[data-file-path="memory-vault/Workspace/glance.html"]').click()
  const viewer = page.locator('.fv-modal')
  await expect(viewer).toBeVisible()
  const frame = viewer.locator('.hav-frame')
  await expect(frame).toBeVisible()

  const body = viewer.locator('.fv-body')
  const toolbar = viewer.locator('.hav-toolbar')
  const frameBox = (await frame.boundingBox())!
  const bodyBox = (await body.boundingBox())!
  const toolbarBox = (await toolbar.boundingBox())!

  // The frame takes the space the toolbar and the 6px gap leave.
  expect(frameBox.height).toBeGreaterThanOrEqual(bodyBox.height - 64)
  // It is no longer sitting on the 320px floor.
  expect(frameBox.height).toBeGreaterThan(400)
  // The viewer body itself does not scroll; the artifact frame is the scroller.
  const bodyScroll = await body.evaluate(el => ({ scrollHeight: el.scrollHeight, clientHeight: el.clientHeight }))
  expect(bodyScroll.scrollHeight).toBeLessThanOrEqual(bodyScroll.clientHeight + 1)
  // Toolbar and frame do not overlap.
  expect(frameBox.y).toBeGreaterThan(toolbarBox.y + toolbarBox.height - 1)

  await page.screenshot({ path: testInfo.outputPath('artifact-viewer-fill.png'), fullPage: false })
})
