import { expect, test, type Page } from '@playwright/test'
import { COMPOSER, boot, isolate } from '../support/app'

/**
 * Dragging files over a chat shows where a drop goes. In the Mac app on its own
 * engine (here: a page given the app's `ciaobotNativeDrop` handler) there are
 * two zones, attach a copy and use in place; a browser tab on the engine's
 * machine (the fixture's 127.0.0.1) offers only the copy and says why.
 */
const CHAT_PATH = '/chat/alpha-chat-1'
const GRANT = '0f8fad5b-d9cb-469f-a165-70867728950e'
const REF = `drop_${'e'.repeat(32)}`

test.beforeEach(async ({ page }, testInfo) => {
  await isolate(page, `composer-drop-zones-${testInfo.workerIndex}`)
})

async function installNativeBridge(page: Page) {
  await page.addInitScript((grant) => {
    const calls: unknown[] = []
    ;(window as unknown as { __nativeDropCalls: unknown[] }).__nativeDropCalls = calls
    ;(window as unknown as { webkit: unknown }).webkit = {
      messageHandlers: {
        ciaobotNativeDrop: {
          postMessage: async (body: unknown) => {
            calls.push(body)
            return { grantId: grant, names: ['Q3 data/'] }
          },
        },
      },
    }
  }, GRANT)
}

async function drag(page: Page, type: 'dragover' | 'drop', selector: string) {
  const box = await page.locator(selector).boundingBox()
  const x = box ? box.x + box.width / 2 : 10
  const y = box ? box.y + box.height / 2 : 10
  const dataTransfer = await page.evaluateHandle(() => {
    const dt = new DataTransfer()
    dt.items.add(new File(['x'], 'notes.md', { type: 'text/markdown' }))
    return dt
  })
  await page.dispatchEvent('.chat-panel', type, { dataTransfer, clientX: x, clientY: y })
}

test('the Mac app offers both zones and a drop on use in place references the path', async ({ page }, testInfo) => {
  await installNativeBridge(page)
  const bodies: unknown[] = []
  await page.route('**/api/desktop-drop', route => {
    bodies.push(route.request().postDataJSON())
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ file_refs: [{ ref: REF, name: 'Q3 data/', kind: 'folder', outside: true }], image_refs: [], errors: [], notices: [] }),
    })
  })
  await boot(page, CHAT_PATH, COMPOSER)

  await drag(page, 'dragover', '.chat-panel')
  await expect(page.locator('.drop-zone')).toHaveCount(2)
  await drag(page, 'dragover', '.drop-zone:nth-child(2)')
  await expect(page.locator('.drop-zone--active .drop-zone__title')).toHaveText('Use in place')
  await page.screenshot({ path: testInfo.outputPath('drop-zones-native.png') })
  await page.emulateMedia({ colorScheme: 'dark', reducedMotion: 'reduce' })
  await page.screenshot({ path: testInfo.outputPath('drop-zones-native-dark.png') })
  for (const zone of await page.locator('.drop-zone').all()) {
    expect((await zone.boundingBox())!.height).toBeGreaterThanOrEqual(44)
  }

  await drag(page, 'drop', '.drop-zone:nth-child(2)')

  await expect(page.locator('.pending-attachments')).toContainText('Q3 data/')
  await expect(page.locator('.file-chip-tag')).toHaveText('in place')
  expect(bodies).toEqual([expect.objectContaining({ grant_id: GRANT, mode: 'in_place' })])
  await expect(page.locator('.toast').filter({ hasText: "outside this chat's folders" })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('drop-in-place-chip.png') })
})

test('the in-place button is the keyboard path and asks the app for its panel', async ({ page }) => {
  await installNativeBridge(page)
  await page.route('**/api/desktop-drop', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ file_refs: [{ ref: REF, name: 'Q3 data/', outside: false }] }),
  }))
  await boot(page, CHAT_PATH, COMPOSER)

  const button = page.getByRole('button', { name: 'Use a file or folder in place (path only, nothing copied)' })
  await button.focus()
  await page.keyboard.press('Enter')

  await expect(page.locator('.file-chip-tag')).toHaveText('in place')
  expect(await page.evaluate(() => (window as unknown as { __nativeDropCalls: unknown[] }).__nativeDropCalls)).toEqual([{ op: 'pick' }])
})

test('a browser tab on the engine machine offers only a copy, and says why', async ({ page }, testInfo) => {
  await boot(page, CHAT_PATH, COMPOSER)

  await drag(page, 'dragover', '.chat-panel')

  await expect(page.locator('.drop-zone')).toHaveCount(1)
  await expect(page.locator('.drop-zone__note')).toContainText('A browser tab cannot see where a file lives')
  await expect(page.getByRole('button', { name: /in place/ })).toHaveCount(0)
  await page.screenshot({ path: testInfo.outputPath('drop-zones-loopback.png') })
})
