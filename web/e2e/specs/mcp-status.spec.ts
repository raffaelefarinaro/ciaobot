import { expect, test } from '@playwright/test'
import { boot, horizontalOverflow, isolate } from '../support/app'

for (const mobile of [false, true]) {
  test(`MCP failure remains visible on ${mobile ? 'touch' : 'desktop and zoom'}`, async ({ browser }, testInfo) => {
    const context = await browser.newContext({ viewport: mobile ? { width: 390, height: 844 } : { width: 1280, height: 900 }, hasTouch: mobile, isMobile: mobile })
    const page = await context.newPage()
    await page.addInitScript(theme => localStorage.setItem('ciao-theme', theme), mobile ? 'light' : 'dark')
    await isolate(page, `mcp-status-${testInfo.workerIndex}-${mobile}`)
    await page.route('**/api/mcp/status*', route => route.fulfill({ json: {
      workspace: 'personal', agent_root: '/workspace/personal',
      providers: [{ provider: 'opencode', available: true, inventory_state: 'loaded',
        servers: [{ name: 'notion', scope: 'project', source: 'opencode.json', allowed: null,
          connections: [{ state: 'failed', chat_id: 'chat-example', error: 'MCP server connection failed. Check the provider logs.' }] }],
        setup: { add: 'cd /workspace/personal && opencode mcp add example --url https://example.com/mcp', authenticate: 'cd /workspace/personal && opencode mcp auth example' } }],
    } }))
    await boot(page, '/settings/mcp', '#mcp-servers')
    await expect(page.locator('.mcp-connection')).toContainText('connection failed')
    const disclosure = page.locator('.mcp-setup summary')
    await disclosure.focus()
    await expect(disclosure).toBeFocused()
    await page.keyboard.press('Enter')
    await expect(page.locator('.mcp-setup')).toHaveAttribute('open', '')
    if (mobile) {
      expect((await disclosure.boundingBox())!.height).toBeGreaterThanOrEqual(44)
      expect((await page.locator('.mcp-connection a').boundingBox())!.height).toBeGreaterThanOrEqual(44)
    } else {
      await page.screenshot({ path: testInfo.outputPath('mcp-status-desktop.png'), fullPage: true })
      await page.setViewportSize({ width: 640, height: 450 })
      await expect.poll(async () => (await disclosure.boundingBox())!.x).toBeGreaterThanOrEqual(0)
      await page.locator('.mcp-connection').scrollIntoViewIfNeeded()
    }
    expect((await horizontalOverflow(page)).overflow).toBeLessThanOrEqual(0)
    await page.screenshot({ path: testInfo.outputPath('mcp-status.png'), fullPage: true })
    await context.close()
  })
}
