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
    await boot(page, '/settings/providers', '.provider-inline-defaults')
    await expect(page.locator('.startup-overlay')).toHaveCount(0)
    const defaults = page.locator('.provider-inline-defaults').first()
    // The picker names the model the way every picker does; the raw id is
    // what gets saved.
    await expect(defaults.locator('.model-selector__trigger').nth(0)).toContainText('Sonnet')
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

  test(`provider sign-in stays manual on ${mobile ? 'touch' : 'desktop'}`, async ({ browser }, testInfo) => {
    const context = await browser.newContext({
      viewport: mobile ? { width: 390, height: 844 } : { width: 1280, height: 900 },
      hasTouch: mobile,
      isMobile: mobile,
    })
    const page = await context.newPage()
    const inputFrames: string[] = []
    const connects: string[] = []
    page.on('websocket', socket => socket.on('framesent', frame => {
      if (typeof frame.payload === 'string' && frame.payload.includes('"type":"input"')) inputFrames.push(frame.payload)
    }))
    page.on('request', request => {
      if (/\/api\/settings\/providers\/[^/]+\/connect$/.test(request.url())) connects.push(request.url())
    })
    await isolate(page, `provider-manual-${testInfo.workerIndex}-${mobile}`)
    await page.route('**/api/settings/providers', route => route.fulfill({ json: { connections: {
      claude: { name: 'claude', label: 'Claude Code', ok: true, auth: 'oauth', command: 'ignored', account: 'Claude OAuth' },
      opencode: { name: 'opencode', label: 'OpenCode', ok: true, auth: 'oauth', command: 'ignored', credential_inventory: {
        status: 'complete', providers: [{ id: 'custom', name: 'Example', auth_types: ['oauth'], configured: true, verified: null }],
      } },
    } } }))
    await boot(page, '/settings/providers', '.provider-connections')
    await expect(page.getByText('Configured · verification unknown')).toBeVisible()
    const claudeDisclosure = page.getByText('Reconnect Claude Code', { exact: true })
    await claudeDisclosure.focus()
    await page.keyboard.press('Enter')
    await expect(claudeDisclosure.locator('..')).toHaveAttribute('open', '')
    await page.keyboard.press('Enter')
    await expect(claudeDisclosure.locator('..')).not.toHaveAttribute('open', '')
    await expect(claudeDisclosure).toBeFocused()
    const disclosure = page.getByText('Reconnect Example (custom)', { exact: true })
    await disclosure.focus()
    await page.keyboard.press('Enter')
    await expect(disclosure.locator('..')).toHaveAttribute('open', '')
    await page.keyboard.press('Enter')
    await expect(disclosure.locator('..')).not.toHaveAttribute('open', '')
    await expect(disclosure).toBeFocused()
    await page.keyboard.press('Enter')
    const guidance = disclosure.locator('..')
    await expect(guidance.locator('code')).toHaveText('opencode auth login')
    await expect(guidance).toContainText('choose the provider in OpenCode')
    const open = guidance.getByRole('button', { name: 'Open browser terminal for Example (custom)' })
    if (mobile) {
      const summaries = await page.locator('.provider-connections summary').all()
      for (const target of [...summaries, open, guidance.getByRole('button', { name: 'Copy sign-in command for Example (custom)' })]) {
        const box = await target.boundingBox()
        expect(box!.width).toBeGreaterThanOrEqual(44)
        expect(box!.height).toBeGreaterThanOrEqual(44)
      }
    }
    await page.screenshot({ path: testInfo.outputPath('provider-screen.png'), fullPage: true })
    if (!mobile) {
      await page.setViewportSize({ width: 640, height: 450 })
      expect((await horizontalOverflow(page)).overflow).toBeLessThanOrEqual(0)
      await page.setViewportSize({ width: 1280, height: 900 })
    }
    await open.click()
    await expect(page.locator('.terminal-pane--open')).toBeVisible()
    expect(inputFrames).toEqual([])
    expect(connects).toEqual([])
    await page.screenshot({ path: testInfo.outputPath('provider-manual.png'), fullPage: true })
    const { overflow } = await horizontalOverflow(page)
    expect(overflow).toBeLessThanOrEqual(0)
    await context.close()
  })
}
