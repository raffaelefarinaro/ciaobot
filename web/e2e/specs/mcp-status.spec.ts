import { expect, test } from '@playwright/test'
import { boot, horizontalOverflow, isolate, openChatDetails, seedInbox, settled } from '../support/app'

const previews = `${process.env.HOME}/repos/ciaobot/.loop-engineering/2026-11-13-mcp-sidebar-plan/state/screenshots-2971`

for (const provider of ['claude', 'opencode'] as const) {
  test(`${provider} chat workspace and CLI roundtrip requires a sidebar gesture`, async ({ page }, testInfo) => {
    const chatId = 'alpha-chat-1'
    await isolate(page, `mcp-roundtrip-${provider}-${testInfo.workerIndex}`)
    await seedInbox(page, { chats: { [chatId]: { provider } } })
    await page.setViewportSize({ width: 1440, height: 900 })
    let raised = 0
    let started = 0
    await page.addInitScript(() => {
      const state = window as Window & { __mcpPopupCount?: number }
      state.__mcpPopupCount = 0
      window.open = (() => {
        state.__mcpPopupCount = (state.__mcpPopupCount ?? 0) + 1
        return { closed: false, opener: null, location: { href: '' }, close() {} }
      }) as unknown as typeof window.open
    })
    await page.route('**/api/mcp/status*', route => {
      const workspace = new URL(route.request().url()).searchParams.get('workspace') || 'alpha'
      return route.fulfill({ json: {
        workspace, agent_root: `/synthetic/${workspace}`, shell: { kind: 'posix', cd_prefix: 'cd' },
        providers: (['claude', 'opencode'] as const).map(cli => ({
          provider: cli, available: true, inventory_state: 'loaded',
          servers: cli === provider && workspace === 'alpha' ? [{
            name: 'linear', scope: 'project', source: 'settings', kind: 'remote', enabled: true, allowed: true,
            connections: [{ chat_id: chatId, state: 'needs_auth' }],
          }] : [],
        })),
      } })
    })
    await page.route(`**/api/chats/${chatId}/actions/mcp-auth`, async route => {
      raised++
      expect(route.request().postDataJSON()).toEqual({ server: 'linear' })
      const response = await page.request.post('/__fixture__/chat-action', { data: {
        chat_id: chatId, kind: 'mcp_auth', state: 'open', params: { provider, server: 'linear' },
      } })
      expect(response.ok()).toBe(true)
      const { action } = await response.json()
      await route.fulfill({ json: { action } })
    })
    await page.route(`**/api/chats/${chatId}/actions/*/start`, async route => {
      started++
      await route.fallback()
    })
    await boot(page, `/chat/${chatId}`, 'textarea.chat-input')
    const rail = page.locator('.chat-rail')
    await settled(page.locator('.chat-layout'))
    if (!(await rail.isVisible())) await openChatDetails(page)
    await rail.getByRole('button', { name: 'Chat workspace MCP settings' }).click()
    await expect(page).toHaveURL(`/settings/mcp?workspace=alpha&provider=${provider}`)
    await expect(page.getByRole('tab', { name: provider === 'claude' ? /Claude Code/ : /OpenCode/ })).toHaveAttribute('aria-selected', 'true')
    const other = page.getByRole('tab', { name: provider === 'claude' ? /OpenCode/ : /Claude Code/ })
    await other.click()
    await page.getByRole('button', { name: 'Refresh' }).click()
    await expect(other).toHaveAttribute('aria-selected', 'true')
    await page.getByRole('tab', { name: provider === 'claude' ? /Claude Code/ : /OpenCode/ }).click()
    await page.locator('#mcp-servers').getByRole('button', { name: 'Sign in', exact: true }).click()
    await expect(page).toHaveURL(`/chat/${chatId}?details=mcp`)
    await expect(page.locator('.chat-mcp-heading')).toBeFocused()
    expect(raised).toBe(1)
    expect(started).toBe(0)
    expect(await page.evaluate(() => (window as Window & { __mcpPopupCount?: number }).__mcpPopupCount)).toBe(0)
    if (provider === 'claude') await page.screenshot({ path: `${previews}/desktop.png`, animations: 'disabled' })
    await page.locator('.chat-mcp-connections').getByRole('button', { name: 'Sign in', exact: true }).click()
    await expect.poll(() => started).toBe(1)
    expect(await page.evaluate(() => (window as Window & { __mcpPopupCount?: number }).__mcpPopupCount)).toBe(1)
    expect(raised).toBe(1)
    const detail = await (await page.request.get(`/api/chats/${chatId}`)).json()
    expect(detail.actions[0]).toMatchObject({ kind: 'mcp_auth', state: 'waiting', params: { provider, server: 'linear' } })
    const beforeReconnect = (await (await page.request.get('/__fixture__/ws-count')).json()).connections as number
    await page.request.post('/__fixture__/drop-ws')
    await expect.poll(async () => (await (await page.request.get('/__fixture__/ws-count')).json()).connections as number).toBeGreaterThan(beforeReconnect)
    await expect(page.locator('.chat-mcp-connections .needs-you-card a')).toHaveAttribute('href', /auth\.example\.invalid/)
  })
}

for (const viewport of [
  { label: 'phone', width: 390, height: 844, touch: true },
  { label: 'narrow', width: 640, height: 450, touch: false },
] as const) {
  test(`MCP deep link focuses the visible heading at ${viewport.label} size`, async ({ browser }, testInfo) => {
    const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, hasTouch: viewport.touch, isMobile: viewport.touch })
    const page = await context.newPage()
    await isolate(page, `mcp-link-${viewport.label}-${testInfo.workerIndex}`)
    const seeded = await page.request.post('/__fixture__/chat-action', { data: {
      chat_id: 'alpha-chat-1', kind: 'mcp_auth', state: 'open', params: { provider: 'claude', server: 'linear' },
    } })
    expect(seeded.ok()).toBe(true)
    await page.route('**/api/mcp/status*', route => route.fulfill({ json: {
      workspace: 'alpha', agent_root: '/synthetic/alpha', shell: { kind: 'posix', cd_prefix: 'cd' },
      providers: [{ provider: 'claude', available: true, inventory_state: 'loaded', servers: [{
        name: 'linear', scope: 'project', source: 'settings', kind: 'remote', enabled: true, allowed: true,
        connections: [{ chat_id: 'alpha-chat-1', state: 'needs_auth' }],
      }] }],
    } }))
    await boot(page, '/chat/alpha-chat-1?details=mcp', 'textarea.chat-input')
    const rail = page.locator('.chat-rail--drawer')
    const heading = rail.locator('.chat-mcp-heading')
    await expect(heading).toBeFocused()
    await settled(rail)
    const bounds = await rail.boundingBox()
    const header = await rail.locator('.chat-rail-head').boundingBox()
    const title = await heading.boundingBox()
    expect(bounds).not.toBeNull()
    expect(title).not.toBeNull()
    expect(title!.y).toBeGreaterThanOrEqual(bounds!.y)
    expect(title!.y).toBeGreaterThanOrEqual(header!.y + header!.height)
    expect(title!.y + title!.height).toBeLessThanOrEqual(bounds!.y + bounds!.height)
    if (viewport.touch) {
      expect(await page.evaluate(() => matchMedia('(pointer: coarse)').matches)).toBe(true)
      for (const selector of ['.chat-mcp-refresh', '.chat-mcp-settings', '.needs-you-btn--primary', '.needs-you-turn-off']) {
        const control = rail.locator(selector)
        const box = await control.boundingBox()
        expect(box!.width).toBeGreaterThanOrEqual(44)
        expect(box!.height).toBeGreaterThanOrEqual(44)
        await control.click({ trial: true })
      }
      await heading.focus()
    }
    expect((await horizontalOverflow(page)).overflow).toBeLessThanOrEqual(0)
    await page.screenshot({ path: `${previews}/${viewport.label}.png`, animations: 'disabled' })
    await context.close()
  })
}

for (const view of [
  { label: 'desktop', width: 1440, height: 900, split: false, touch: false },
  { label: 'split', width: 1440, height: 900, split: true, touch: false },
  { label: 'phone', width: 390, height: 844, split: false, touch: true },
  { label: 'narrow', width: 640, height: 450, split: false, touch: false },
] as const) {
test(`MCP reveal scrolls only its own rail at ${view.label} size`, async ({ browser }, testInfo) => {
  const context = await browser.newContext({ viewport: { width: view.width, height: view.height }, hasTouch: view.touch, isMobile: view.touch })
  const page = await context.newPage()
  await isolate(page, `mcp-scroll-isolation-${view.label}-${testInfo.workerIndex}-${testInfo.repeatEachIndex}`)
  await page.addInitScript(split => {
    if (split) localStorage.setItem('ciao:beside-chat', JSON.stringify({ alpha: 'alpha-chat-2' }))
    const original = EventTarget.prototype.addEventListener
    EventTarget.prototype.addEventListener = function (this: EventTarget, ...args: Parameters<EventTarget['addEventListener']>) {
      if (args[0] === 'wheel' && this instanceof HTMLElement && this.matches('.messages')) this.dataset.wheelReady = 'true'
      return original.apply(this, args)
    } as typeof EventTarget.prototype.addEventListener
  }, view.split)
  await page.route('**/api/mcp/status*', route => route.fulfill({ json: {
    workspace: 'alpha', agent_root: '/synthetic/alpha', shell: { kind: 'posix', cd_prefix: 'cd' },
    providers: [{ provider: 'claude', available: true, inventory_state: 'loaded', servers: [] }],
  } }))
  await boot(page, '/chat/alpha-chat-1', 'textarea.chat-input')
  await expect(page.locator('.chat-panel')).toHaveCount(view.split ? 2 : 1)
  if (view.split) await expect(page.locator('.chat-panel').nth(1).locator('.messages')).toHaveAttribute('data-wheel-ready', 'true')
  await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))))
  await page.evaluate(() => {
    const app = document.querySelector<HTMLElement>('#app')!
    const host = document.createElement('div')
    host.id = 'test-scroll-host'
    host.style.cssText = 'height:100vh;overflow:auto'
    app.before(host)
    const top = document.createElement('div')
    top.style.height = '120px'
    const bottom = document.createElement('div')
    bottom.style.height = '500px'
    host.append(top, app, bottom)
    host.scrollTop = 120
    const style = document.createElement('style')
    style.textContent = '.chat-rail-open { min-height: 650px !important; } .chat-mcp-rows::before { content: ""; display: block; height: 500px; } .messages[data-test-scroll-other]::after { content: ""; display: block; height: 1500px; min-height: 1500px; flex: 0 0 1500px; }'
    document.head.append(style)
  })
  await settled(page.locator('.chat-layout'))
  await expect(page.locator('.chat-panel')).toHaveCount(view.split ? 2 : 1)
  if (view.split) await expect(page.locator('.chat-split-side .messages')).toHaveAttribute('data-wheel-ready', 'true')
  const before = await page.evaluate(() => {
    const host = document.querySelector<HTMLElement>('#test-scroll-host')!
    host.scrollTop = 120
    const messages = document.querySelectorAll<HTMLElement>('.chat-panel .messages')[1]
    if (messages) {
      messages.setAttribute('data-test-scroll-other', '')
      messages.dispatchEvent(new WheelEvent('wheel', { deltaY: -1 }))
      messages.scrollTop = 180
      messages.dispatchEvent(new Event('scroll'))
    }
    return { page: window.scrollY, ancestor: host.scrollTop, other: messages?.scrollTop ?? null }
  })
  expect(before.ancestor).toBeGreaterThan(0)
  if (view.split) expect(before.other!).toBeGreaterThan(0)
  await page.evaluate(async () => {
    const app = document.querySelector('#app') as HTMLElement & {
      __vue_app__: { config: { globalProperties: { $router: { push(path: string): Promise<unknown> } } } }
    }
    await app.__vue_app__.config.globalProperties.$router.push('/chat/alpha-chat-1?details=mcp')
  })
  const heading = page.locator('.chat-panel').first().locator('.chat-mcp-heading')
  await expect(heading).toBeFocused()
  const after = await page.evaluate(() => ({
    page: window.scrollY,
    ancestor: document.querySelector<HTMLElement>('#test-scroll-host')!.scrollTop,
    other: document.querySelector<HTMLElement>('.chat-split-side .messages[data-test-scroll-other]')?.scrollTop ?? null,
  }))
  expect(after).toEqual(before)
  const rail = page.locator('.chat-panel').first().locator('.chat-rail')
  const [section, header, bounds] = await Promise.all([heading.boundingBox(), rail.locator('.chat-rail-head').boundingBox(), rail.boundingBox()])
  expect(section!.y).toBeGreaterThanOrEqual(header!.y + header!.height)
  expect(section!.y + section!.height).toBeLessThanOrEqual(bounds!.y + bounds!.height)
  expect(await rail.evaluate(el => el.scrollTop)).toBeGreaterThan(0)
  if (view.label !== 'desktop') {
    const headingScroll = await rail.evaluate(el => el.scrollTop)
    const seeded = await page.request.post('/__fixture__/chat-action', { data: {
      chat_id: 'alpha-chat-1', kind: 'mcp_auth', state: 'open', params: { provider: 'claude', server: 'linear' },
    } })
    expect(seeded.ok()).toBe(true)
    const card = rail.locator('.needs-you-card')
    await expect(card).toBeVisible()
    await expect.poll(() => rail.evaluate(el => el.scrollTop)).toBeGreaterThan(headingScroll)
    const final = await page.evaluate(() => ({
      page: window.scrollY,
      ancestor: document.querySelector<HTMLElement>('#test-scroll-host')!.scrollTop,
      other: document.querySelector<HTMLElement>('.chat-split-side .messages[data-test-scroll-other]')?.scrollTop ?? null,
    }))
    expect(final).toEqual(before)
    const signIn = card.getByRole('button', { name: 'Sign in', exact: true })
    const [buttonBox, railBox] = await Promise.all([signIn.boundingBox(), rail.boundingBox()])
    expect(buttonBox!.y).toBeGreaterThanOrEqual(railBox!.y)
    expect(buttonBox!.y + buttonBox!.height).toBeLessThanOrEqual(railBox!.y + railBox!.height)
    await signIn.click({ trial: true })
    await page.screenshot({ path: `${previews}/${view.label}-later-action.png`, animations: 'disabled' })
  }
  await context.close()
})
}

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
          servers: [{ name: 'notion', scope: 'project', source: 'opencode.json', allowed: null, applies_to: '/workspace/personal/opencode.json',
            connections: [{ state: 'failed', chat_id: 'chat-example', error: 'MCP server connection failed. Check the provider logs.' }] }] },
        { provider: 'claude', available: true, inventory_state: 'loaded', servers: [] },
      ],
    } }))
    await page.route('**/api/mcp/servers/enabled', route => route.fulfill({ json: { workspace: 'personal', provider: 'opencode', server: 'notion', enabled: false, changed: true, applies_to: '/workspace/personal/opencode.json', warning: null, reload: { reloaded: [], pending: [] } } }))
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
    const toggle = page.getByRole('switch', { name: 'notion for OpenCode' })
    await expect(toggle).toHaveAttribute('aria-checked', 'true')

    const ask = page.getByRole('button', { name: 'Ask Ciao to add one' })
    await expect(ask).toBeVisible()
    await ask.focus()
    await expect(ask).toBeFocused()

    await page.locator('details.mcp-manual > summary').click()
    await page.getByLabel('Server name').fill('notion')
    await page.getByLabel('Server URL').fill('https://example.com/mcp')
    const addRow = page.locator('.mcp-commands li[data-row="add"]')
    await expect(addRow.locator('code')).toContainText('opencode mcp add notion --url https://example.com/mcp')

    if (mobile) {
      const targets = [ask, tabs.first(), addRow.getByRole('button', { name: 'Copy' }), addRow.getByRole('button', { name: 'Run in terminal' }), toggle, page.getByLabel('Server name'), page.locator('.mcp-connection a')]
      for (const target of targets) {
        await target.scrollIntoViewIfNeeded()
        expect((await target.boundingBox())!.height).toBeGreaterThanOrEqual(44)
      }
    } else {
      await page.screenshot({ path: testInfo.outputPath('mcp-status-desktop.png'), fullPage: true })
      await page.setViewportSize({ width: 640, height: 450 })
      await page.locator('.mcp-connection').scrollIntoViewIfNeeded()
    }
    await toggle.focus()
    await page.keyboard.press('Enter')
    await expect(toggle).toHaveAttribute('aria-checked', 'false')
    await expect(toggle).toBeFocused()
    await expect(page.locator('.mcp-toggle-note')).toContainText('notion is off in /workspace/personal/opencode.json.')
    await addRow.scrollIntoViewIfNeeded()
    expect((await horizontalOverflow(page)).overflow).toBeLessThanOrEqual(0)
    await page.screenshot({ path: testInfo.outputPath('mcp-status.png'), fullPage: true })
    await context.close()
  })
}
