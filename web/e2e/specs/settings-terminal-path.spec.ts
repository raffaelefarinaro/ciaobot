import { expect, test } from '@playwright/test'
import { boot, horizontalOverflow, isolate } from '../support/app'

for (const mobile of [false, true]) {
  test(`terminal PATH approval is keyboard operable and never writes before click (${mobile ? 'phone' : 'desktop'})`, async ({ browser }, testInfo) => {
    const context = await browser.newContext({ viewport: mobile ? { width: 390, height: 844 } : { width: 1280, height: 900 }, hasTouch: mobile, isMobile: mobile })
    const page = await context.newPage()
    await isolate(page, `terminal-path-${testInfo.workerIndex}-${mobile}`)
    let refused = false
    const writes: unknown[] = []
    await page.route('**/api/settings/terminal-path/preview', route => route.fulfill({ json: refused
      ? { status: 'refused', detail: 'Conflicting command', executable: '', directory: '', destination: '', revision: '' }
      : { status: 'ready', detail: 'Review the proposed change', executable: '/fixture/bin/ciao', directory: '/fixture/bin', destination: '/fixture/.profile', revision: 'a'.repeat(64) } }))
    await page.route('**/api/settings/terminal-path/apply', route => {
      writes.push(route.request().postDataJSON())
      return route.fulfill({ json: { status: 'applied', detail: 'Verified', mutation: 'written', warning: '' } })
    })
    await boot(page, '/settings/advanced', '#restart-host')
    const card = page.locator('#restart-host')
    const add = card.getByRole('button', { name: 'Add to terminal PATH' })
    await expect(add).toBeVisible()
    await expect(card).toContainText('Terminal PATH · ciao')
    await expect(card).toContainText('Executable: /fixture/bin/ciao')
    await expect(card).toContainText('Directory: /fixture/bin')
    await expect(card).toContainText('Destination: /fixture/.profile')
    await expect(card).toContainText('Append this directory to your terminal PATH')
    await expect(card).toContainText('Open a new terminal')
    expect(writes).toEqual([])
    await page.screenshot({ path: testInfo.outputPath('ready.png'), fullPage: true })
    await add.focus()
    await page.keyboard.press('Enter')
    await expect(card).toContainText('Verified')
    expect(writes).toEqual([{ target: 'ciao', revision: 'a'.repeat(64) }])
    await page.screenshot({ path: testInfo.outputPath('success.png'), fullPage: true })
    refused = true
    await page.reload()
    await expect(card).toContainText('Conflicting command')
    await page.screenshot({ path: testInfo.outputPath('refused.png'), fullPage: true })
    expect((await horizontalOverflow(page)).overflow).toBeLessThanOrEqual(0)
    await context.close()
  })
}
