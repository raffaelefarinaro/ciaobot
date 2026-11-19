import { expect, test, type Page } from '@playwright/test'
import { boot, horizontalOverflow, isolate } from '../support/app'

const command = 'curl -fsSL https://claude.ai/install.sh | bash'

async function setup(page: Page) {
  await isolate(page, 'native-login-setup')
  await page.route('**/api/setup-status', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
    configured: false, bootstrap: true, mode: 'bootstrap', workspace_root: '/fixture/workspace',
    vault_root: '/fixture/workspace/memory-vault', checks: [],
    providers: { claude: { name: 'claude', ok: false, auth: 'not_installed', command,
      path_command: 'echo \'export PATH="$HOME/.local/bin:$PATH"\' >> ~/.zshrc && source ~/.zshrc', detail: 'Claude Code not installed' } },
    provider_ready: false,
  }) }))
  await page.route('**/api/setup/inspect-folder?*', route => route.fulfill({ json: { mode: 'scratch', existing_workspaces: [] } }))
  await boot(page, '/login', '#setup-password')
}

test('native setup reflows at 200 percent zoom', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 })
  await setup(page)
  await page.setViewportSize({ width: 640, height: 450 })
  await expect(page.locator('#setup-password')).toBeVisible()
  expect((await horizontalOverflow(page)).overflow).toBeLessThanOrEqual(0)
})
