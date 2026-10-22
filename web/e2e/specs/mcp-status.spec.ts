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
      shell: { kind: 'posix', cd_prefix: 'cd -- /workspace/personal && ' },
      providers: [
        { provider: 'opencode', available: true, inventory_state: 'loaded',
          servers: [{ name: 'notion', scope: 'project', source: 'opencode.json', allowed: null,
            connections: [{ state: 'failed', chat_id: 'chat-example', error: 'MCP server connection failed. Check the provider logs.' }] }] },
        { provider: 'claude', available: true, inventory_state: 'loaded', servers: [] },
      ],
    } }))
    await boot(page, '/settings/mcp', '#mcp-servers')
    const tabs = page.locator('[role="tab"]')
    await expect(tabs).toHaveCount(2)
    await tabs.first().focus()
    await page.keyboard.press('ArrowRight')
    await expect(tabs.nth(1)).toHaveAttribute('aria-selected', 'true')
    await page.keyboard.press('ArrowLeft')
    await expect(tabs.first()).toHaveAttribute('aria-selected', 'true')
    await page.getByRole('tab', { name: /OpenCode/ }).click()
    await expect(page.locator('.mcp-connection')).toContainText('connection failed')

    const ask = page.getByRole('button', { name: 'Ask Ciao to add one' })
    await expect(ask).toBeVisible()
    await ask.focus()
    await expect(ask).toBeFocused()

    await page.getByLabel('Server name').fill('notion')
    await page.getByLabel('Server URL').fill('https://example.com/mcp')
    const addRow = page.locator('.mcp-commands li[data-row="add"]')
    await expect(addRow.locator('code')).toContainText('opencode mcp add notion --url https://example.com/mcp')

    if (mobile) {
      const targets = [ask, tabs.first(), addRow.getByRole('button', { name: 'Copy' }), addRow.getByRole('button', { name: 'Run in terminal' }), page.getByLabel('Server name'), page.locator('.mcp-connection a')]
      for (const target of targets) {
        await target.scrollIntoViewIfNeeded()
        expect((await target.boundingBox())!.height).toBeGreaterThanOrEqual(44)
      }
    } else {
      await page.screenshot({ path: testInfo.outputPath('mcp-status-desktop.png'), fullPage: true })
      await page.setViewportSize({ width: 640, height: 450 })
      await page.locator('.mcp-connection').scrollIntoViewIfNeeded()
    }
    await addRow.scrollIntoViewIfNeeded()
    expect((await horizontalOverflow(page)).overflow).toBeLessThanOrEqual(0)
    await page.screenshot({ path: testInfo.outputPath('mcp-status.png'), fullPage: true })
    await context.close()
  })
}
