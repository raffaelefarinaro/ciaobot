import { expect, test } from '@playwright/test'
import { boot, horizontalOverflow, isolate } from '../support/app'

for (const mobile of [false, true]) {
  test(`provider defaults and effort remain usable on ${mobile ? 'touch' : 'desktop'}`, async ({ browser }, testInfo) => {
    const context = await browser.newContext({
      viewport: mobile ? { width: 390, height: 844 } : { width: 1280, height: 900 },
      hasTouch: mobile,
      isMobile: mobile,
    })
    const page = await context.newPage()
    await page.addInitScript((theme) => localStorage.setItem('ciao-theme', theme), mobile ? 'light' : 'dark')
    await isolate(page, `provider-settings-${testInfo.workerIndex}-${mobile}`)
    await page.route('**/api/models*', (route) => route.fulfill({ json: {
      models: ['haiku', 'sonnet', 'opus'],
      default: 'sonnet',
      provider_models: { claude: ['haiku', 'sonnet', 'opus'] },
      provider_defaults: { claude: 'sonnet' },
      thinking_levels: { claude: ['low', 'medium', 'high', 'xhigh', 'max'] },
    } }))
    let settings = {
      insights_enabled: true,
      critique_models: '',
      critique_models_effective: '',
      provider_default_models: { claude: 'sonnet' },
      provider_default_thinking: {},
      provider_insights_models: {},
      backends: { anthropic: true, opencode: false },
      model_options: { anthropic: ['haiku', 'sonnet', 'opus'] },
      workspace_context: { workspace_root: '/fixture/workspace', vault_root: '/fixture/vault' },
    }
    await page.route('**/api/settings/routines', async (route) => {
      if (route.request().method() === 'PATCH') {
        settings = { ...settings, ...route.request().postDataJSON() }
      }
      await route.fulfill({ json: settings })
    })
    await boot(page, '/settings/models', '.provider-inline-defaults')
    await expect(page.locator('.startup-overlay')).toHaveCount(0)
    const defaults = page.locator('.provider-inline-defaults').first()
    await expect(defaults.locator('.model-selector__trigger').nth(0)).toContainText('sonnet')
    await expect(defaults.locator('.model-selector__trigger').nth(1)).toContainText('Automatic — same as chat model')
    const effort = page.locator('select[aria-labelledby="thinking-label-claude"]')
    await effort.focus()
    await expect(effort).toBeFocused()
    await effort.selectOption('high')
    await expect(effort).toHaveValue('high')
    if (mobile) {
      const box = await effort.boundingBox()
      expect(box!.height).toBeGreaterThanOrEqual(44)
    }
    await page.screenshot({ path: testInfo.outputPath('settings.png'), fullPage: true })
    if (!mobile) await page.setViewportSize({ width: 640, height: 450 })
    const { overflow } = await horizontalOverflow(page)
    expect(overflow).toBeLessThanOrEqual(0)
    await context.close()
  })
}
