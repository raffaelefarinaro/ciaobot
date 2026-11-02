import { expect, test, type Page } from '@playwright/test'
import { boot, COMPOSER, isolate } from '../support/app'

/**
 * An .html artifact opened from a transcript must fill the viewer body.
 *
 * `.fv-body` is a block box for every other file kind, so the artifact's
 * column-flex root could not resolve `flex: 1` and the frame fell back to its
 * 320px floor, leaving the rest of the modal as dead space (#2311). jsdom lays
 * nothing out, so only a real browser can assert the geometry.
 *
 * The phone case runs where the ≤640px query zeroes the body padding, so it
 * cannot see a desktop gutter regression — the desktop case exists for that
 * (#2320: an early cut of the fix set `padding: 0` on `.fv-body-html`).
 */
const HTML_DOC = [
  '<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">',
  '<style>body{margin:0;padding:16px;font:16px/1.5 -apple-system,sans-serif}</style></head>',
  '<body><h1>Glance</h1><p>' + 'Artifact body text that must have room to breathe. '.repeat(40) + '</p></body></html>',
].join('')

/** Open the artifact from a mocked transcript and return the three boxes. */
async function openArtifact(page: Page, session: string) {
  await isolate(page, session)
  const canonical = '/fixture/work/memory-vault/Workspace/glance.html'
  await page.route('**/api/chats/alpha-chat-1/messages*', route => route.fulfill({ json: [
    { role: 'user', content: 'Build a glance page', turn_index: 0 },
    { role: 'assistant', content: 'Open [glance](memory-vault/Workspace/glance.html).', turn_index: 0 },
  ] }))
  await page.route('**/api/chats/alpha-chat-1/file-path?*', route => route.fulfill({ json: { path: canonical } }))
  await page.route('**/api/workspace-file?*', route => route.fulfill({ body: HTML_DOC, contentType: 'text/html' }))
  await page.route('**/api/workspace-html?*', route => route.fulfill({ body: HTML_DOC, contentType: 'text/html' }))
  // On a wide pane the link pins the file to the side tile instead of opening
  // the viewer; a refused pin is the documented fallback to the modal, which is
  // where `.fv-body-html` lives.
  await page.route('**/api/chats/alpha-chat-1', route => route.request().method() === 'PATCH'
    ? route.fulfill({ status: 404, json: { error: 'not found' } })
    : route.fallback())

  await boot(page, '/chat/alpha-chat-1', COMPOSER)
  await page.locator('a[data-file-path="memory-vault/Workspace/glance.html"]').click()
  const viewer = page.locator('.fv-modal')
  await expect(viewer).toBeVisible()
  const frame = viewer.locator('.hav-frame')
  await expect(frame).toBeVisible()
  return { viewer, frame, body: viewer.locator('.fv-body'), toolbar: viewer.locator('.hav-toolbar') }
}

test.describe('phone', () => {
  test.use({
    viewport: { width: 430, height: 932 },
    isMobile: true,
    hasTouch: true,
    deviceScaleFactor: 3,
  })

  test('the artifact frame fills the phone viewer', async ({ page }, testInfo) => {
    const { frame, body, toolbar } = await openArtifact(page, `artifact-fill-${testInfo.workerIndex}`)

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
})

test.describe('desktop', () => {
  test.use({ viewport: { width: 1280, height: 900 } })

  test('the artifact keeps the viewer gutter on desktop', async ({ page }, testInfo) => {
    const { viewer, frame } = await openArtifact(page, `artifact-gutter-${testInfo.workerIndex}`)

    const modalBox = (await viewer.boundingBox())!
    const frameBox = (await frame.boundingBox())!

    // The body's 32px side padding and 28px top padding are the gutter; losing
    // it puts the artifact flush against the dialog edge.
    expect(frameBox.x - modalBox.x).toBeGreaterThanOrEqual(24)
    expect((modalBox.x + modalBox.width) - (frameBox.x + frameBox.width)).toBeGreaterThanOrEqual(24)
    expect(frameBox.y - modalBox.y).toBeGreaterThanOrEqual(60)
    expect(frameBox.height).toBeGreaterThan(500)

    await page.screenshot({ path: testInfo.outputPath('artifact-viewer-gutter.png'), fullPage: false })
  })
})
