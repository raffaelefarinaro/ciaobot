import { expect, test, type Page } from '@playwright/test'
import { boot, horizontalOverflow, isolate } from '../support/app'

const command = 'curl -fsSL https://claude.ai/install.sh | bash'

async function setup(page: Page) {
  await isolate(page, 'native-login-setup')
  await page.route('**/api/setup-status', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
    configured: false, bootstrap: true, mode: 'bootstrap', workspace_root: '/fixture/workspace',
    vault_root: '/fixture/workspace/memory-vault', suggested_workspace: null, checks: [],
    providers: { claude: { name: 'claude', ok: false, auth: 'not_installed', command,
      path_command: 'echo \'export PATH="$HOME/.local/bin:$PATH"\' >> ~/.zshrc && source ~/.zshrc', detail: 'Claude Code not installed' } },
    provider_ready: false, native_bootstrap: true,
  }) }))
  await page.route('**/api/setup/inspect-folder?*', route => route.fulfill({ json: { mode: 'scratch', existing_workspaces: [] } }))
  await boot(page, '/login', '#setup-password')
}

test('native setup keeps controls and visible keyboard focus', async ({ page }) => {
  await setup(page)
  for (const selector of ['.login-shell-bar', '.setup-run-note', '.setup-tour', '.banner-meta']) await expect(page.locator(selector)).toHaveCount(0)
  await expect(page.getByText('Claude Code not installed', { exact: true })).toBeVisible()
  await expect(page.getByText('Not installed yet. Run this in your Terminal to install it:', { exact: true })).toBeVisible()
  await expect(page.getByText(command, { exact: true })).toBeVisible()
  await page.locator('body').evaluate(el => (el as HTMLElement).focus())
  for (const selector of ['#setup-workspace', '#setup-workspace-browse', '#setup-workspace-name', '#setup-password', '#setup-password-confirm', 'input[type=radio][value=claude]', 'input[type=radio][value=opencode]']) {
    await page.keyboard.press('Tab')
    await expect(page.locator(selector)).toBeFocused()
    const outline = await page.locator(selector).evaluate(el => { const s = getComputedStyle(el); return { style: s.outlineStyle, width: parseFloat(s.outlineWidth), color: s.outlineColor } })
    expect(outline.style, selector).not.toBe('none')
    expect(outline.width).toBeGreaterThanOrEqual(2)
    expect(outline.color).not.toBe('rgba(0, 0, 0, 0)')
  }
  await expect(page.locator('.prompt-submit.btn-primary')).toBeVisible()
})

test.describe('touch setup', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true })
  test('native setup has reachable touch targets', async ({ page }) => {
    await setup(page)
    if (!await page.evaluate(() => matchMedia('(pointer: coarse)').matches)) {
      expect(await page.evaluate(() => navigator.maxTouchPoints)).toBeGreaterThan(0)
      const session = await page.context().newCDPSession(page)
      await session.send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 5 })
    }
    await expect.poll(() => page.evaluate(() => matchMedia('(pointer: coarse)').matches)).toBe(true)
    const controls = page.locator('.form-input, .btn-small, .btn-primary')
    for (const control of await controls.all()) {
      if (!await control.isVisible()) continue
      const box = await control.boundingBox()
      expect(box!.width).toBeGreaterThanOrEqual(44)
      expect(box!.height).toBeGreaterThanOrEqual(44)
    }
    expect((await horizontalOverflow(page)).overflow).toBeLessThanOrEqual(0)
  })
})

test('native setup reflows at 200 percent zoom', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 })
  await setup(page)
  await page.setViewportSize({ width: 640, height: 450 })
  await expect(page.locator('#setup-password')).toBeVisible()
  expect((await horizontalOverflow(page)).overflow).toBeLessThanOrEqual(0)
})
