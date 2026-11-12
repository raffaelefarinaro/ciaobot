import { readFileSync } from 'node:fs'
import { expect, test, type Page } from '@playwright/test'
import { COMPOSER, boot, isolate } from '../support/app'

/**
 * On a phone the chat's shared pin is a chip under the header; tapping it opens
 * the pinned file in the viewer. Every kind `ciao file surface` can pin must
 * open there: the tile on a wide pane renders images itself, so an image pin
 * that took the text path showed "Unsupported file type." instead.
 *
 * The fixture's only pinned chat is beta-chat-1; the spec moves its pin to the
 * file under test on both the events snapshot and the chat rows, and answers
 * the file routes the way the engine does (`/api/workspace-file` is text only
 * and refuses an image with 415).
 */
test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

const CHAT = 'beta-chat-1'
// A real PNG from the app's own icons, so the rendered image has a real size.
const PNG = readFileSync(new URL('../../public/icons/icon-512.png', import.meta.url))
const IMAGE_RE = /\.(png|jpe?g|gif|webp|svg|avif|bmp|ico)$/i

async function pinTo(page: Page, pin: string): Promise<void> {
  await page.routeWebSocket(/\/ws\/events/, ws => {
    const server = ws.connectToServer()
    server.onMessage(message => {
      const frame = typeof message === 'string' ? JSON.parse(message) : null
      if (frame?.type === 'snapshot' && frame.chat_pins?.[CHAT]) {
        frame.chat_pins[CHAT] = { path: pin, dismissed_paths: [], revision: 5 }
        ws.send(JSON.stringify(frame))
        return
      }
      ws.send(message)
    })
    ws.onMessage(message => server.send(message))
  })
  await page.route(/\/api\/chats(\/beta-chat-1)?(\?.*)?$/, async route => {
    if (route.request().method() !== 'GET') return route.fallback()
    const response = await route.fetch()
    const data = await response.json() as Record<string, unknown> | Array<Record<string, unknown>>
    const fix = (c: Record<string, unknown>) => c.chat_id === CHAT ? { ...c, pinned_file_path: pin, pin_revision: 5 } : c
    await route.fulfill({ response, json: Array.isArray(data) ? data.map(fix) : fix(data) })
  })
  await page.route(`**/api/chats/${CHAT}/file-path?*`, route => route.fulfill({ json: { path: pin } }))
  await page.route('**/api/workspace-file?*', route => IMAGE_RE.test(new URL(route.request().url()).searchParams.get('path') || '')
    ? route.fulfill({ status: 415, json: { error: 'unsupported type' } })
    : route.fulfill({ body: '# Pinned report\n\nThe body.', contentType: 'text/plain' }))
  await page.route('**/api/workspace-image?*', route => route.fulfill({ body: PNG, contentType: 'image/png' }))
  await page.route('**/api/workspace-html?*', route => route.fulfill({
    body: '<!doctype html><title>Art</title><h1>Pinned artifact</h1>', contentType: 'text/html',
  }))
}

async function tapChip(page: Page, pin: string, session: string) {
  await isolate(page, session)
  await pinTo(page, pin)
  await boot(page, `/chat/${CHAT}`, COMPOSER)
  const chip = page.locator('.narrow-pin-btn')
  await expect(chip).toBeVisible()
  await expect(chip).toContainText(pin.split('/').pop()!)
  await chip.tap()
  const viewer = page.locator('.fv-modal')
  await expect(viewer).toBeVisible()
  await expect(viewer.locator('.fv-error')).toHaveCount(0)
  return viewer
}

test('an image pin opens as an image from the phone chip', async ({ page }, testInfo) => {
  const pin = '/synthetic/beta/code-mode-auto-chat-details-v2.png'
  const viewer = await tapChip(page, pin, `phone-pin-png-${testInfo.workerIndex}`)
  const img = viewer.locator('.fv-img')
  await expect(img).toBeVisible()
  await expect(img).toHaveAttribute('src', new RegExp(`^/api/workspace-image\\?path=${encodeURIComponent(pin)}`))
  await expect.poll(() => img.evaluate(el => (el as HTMLImageElement).naturalWidth)).toBeGreaterThan(0)
})

test('a markdown pin opens rendered from the phone chip', async ({ page }, testInfo) => {
  const viewer = await tapChip(page, '/synthetic/beta/pinned-report.md', `phone-pin-md-${testInfo.workerIndex}`)
  await expect(viewer.locator('.fv-md h1')).toHaveText('Pinned report')
})

test('an HTML artifact pin opens in the artifact frame from the phone chip', async ({ page }, testInfo) => {
  const viewer = await tapChip(page, '/synthetic/beta/page.html', `phone-pin-html-${testInfo.workerIndex}`)
  const frame = viewer.locator('.hav-frame')
  await expect(frame).toBeVisible()
  await expect(page.frameLocator('.fv-modal .hav-frame').locator('h1')).toHaveText('Pinned artifact')
})
