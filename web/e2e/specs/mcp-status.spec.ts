import { expect, test, type Page } from '@playwright/test'
import { boot, horizontalOverflow, isolate, openChatDetails, seedInbox, settled } from '../support/app'

/** Provider rows the MCP settings page turns into one button each. A row with no `mcp_command` is not a button. */
function providerConnections() {
  return {
    connections: {
      claude: { name: 'claude', label: 'Claude Code', ok: true, auth: 'ok', command: 'claude', mcp_command: 'claude mcp' },
      opencode: { name: 'opencode', label: 'OpenCode', ok: true, auth: 'ok', command: 'opencode', mcp_command: 'opencode mcp' },
      missing: { name: 'missing', label: 'Missing', ok: false, auth: 'not_installed', command: '' },
    },
  }
}

function agentMcp(page: Page, label: string) {
  return page.locator('.mcp-open', { has: page.getByRole('button', { name: `Open MCP settings in ${label}` }) })
}

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
    await page.route('**/api/settings/providers', route => route.fulfill({ json: providerConnections() }))
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
    const label = provider === 'claude' ? 'Claude Code' : 'OpenCode'
    const otherLabel = provider === 'claude' ? 'OpenCode' : 'Claude Code'
    const command = provider === 'claude' ? 'claude mcp' : 'opencode mcp'
    const nav = page.getByRole('navigation', { name: 'Settings sections' })
    await expect(nav.getByRole('link', { name: 'MCP servers' })).toHaveAttribute('aria-current', 'page')
    await expect(nav.getByRole('heading', { name: 'Alpha' })).toBeVisible()
    const mine = agentMcp(page, label)
    const other = agentMcp(page, otherLabel)
    await expect(mine.locator('code')).toHaveText(command)
    await expect(other.locator('code')).toHaveText(provider === 'claude' ? 'opencode mcp' : 'claude mcp')
    await expect(page.getByRole('button', { name: 'Open MCP settings in Missing' })).toHaveCount(0)
    await expect(page.getByRole('tab', { name: /Claude Code|OpenCode/ })).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Ask Ciao to add one' })).toBeVisible()
    if (provider === 'claude') await page.screenshot({ path: `${previews}/desktop.png`, animations: 'disabled' })
    await page.goBack()
    await expect(page).toHaveURL(new RegExp(`/chat/${chatId}$`))
    if (!(await rail.isVisible())) await openChatDetails(page)
    await rail.locator('.chat-mcp-connections').getByRole('button', { name: 'Sign in', exact: true }).click()
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
    await expect(heading).toBeFocused()
    const seeded = await page.request.post('/__fixture__/chat-action', { data: {
      chat_id: 'alpha-chat-1', kind: 'mcp_auth', state: 'open', params: { provider: 'claude', server: 'linear' },
    } })
    expect(seeded.ok()).toBe(true)
    const card = rail.locator('.needs-you-card')
    await expect(card).toBeVisible()
    await expect(heading).toBeFocused()
    expect(await rail.evaluate(el => el.scrollTop)).toBe(headingScroll)
    const final = await page.evaluate(() => ({
      page: window.scrollY,
      ancestor: document.querySelector<HTMLElement>('#test-scroll-host')!.scrollTop,
      other: document.querySelector<HTMLElement>('.chat-split-side .messages[data-test-scroll-other]')?.scrollTop ?? null,
    }))
    expect(final).toEqual(before)
  }
  await context.close()
})
}

for (const mobile of [false, true]) {
  test(`an agent MCP command stays visible on ${mobile ? 'touch' : 'desktop and zoom'} when the terminal cannot run it`, async ({ browser }, testInfo) => {
    const context = await browser.newContext({ viewport: mobile ? { width: 390, height: 844 } : { width: 1280, height: 900 }, hasTouch: mobile, isMobile: mobile })
    const page = await context.newPage()
    await page.addInitScript(theme => {
      localStorage.setItem('ciao-theme', theme)
      const copied: string[] = []
      ;(window as Window & { __copied?: string[] }).__copied = copied
      Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
        writeText: async (text: string) => { copied.push(text) },
      } })
    }, mobile ? 'light' : 'dark')
    await isolate(page, `mcp-status-${testInfo.workerIndex}-${mobile}`)
    const statusReads: string[] = []
    page.on('request', request => { if (request.url().includes('/api/mcp/status')) statusReads.push(request.url()) })
    await page.route('**/api/settings/providers', route => route.fulfill({ json: providerConnections() }))
    await page.route('**/api/terminal/status*', route => route.fulfill({ json: {
      available: false, reason: 'unavailable', platform: 'linux', host: 'fixture', unlock_expires_in: 0, detached_ttl_seconds: 600,
    } }))
    await boot(page, '/settings/mcp', 'button:has-text("Open MCP settings in Claude Code")')
    await expect(page.getByRole('heading', { name: 'MCP servers' })).toBeVisible()
    if (mobile) await page.getByRole('button', { name: 'Open sidebar' }).click()
    const nav = page.getByRole('navigation', { name: 'Settings sections' })
    await expect(nav.getByRole('link', { name: 'MCP servers' })).toHaveAttribute('aria-current', 'page')
    await expect(nav.getByRole('heading', { name: 'Alpha' })).toBeVisible()
    if (mobile) await page.locator('.sidebar-backdrop').click()
    const claude = agentMcp(page, 'Claude Code')
    const opencode = agentMcp(page, 'OpenCode')
    await expect(claude.locator('code')).toHaveText('claude mcp')
    await expect(opencode.locator('code')).toHaveText('opencode mcp')
    await expect(page.getByRole('button', { name: 'Open MCP settings in Missing' })).toHaveCount(0)
    await expect(page.getByRole('tab')).toHaveCount(0)
    expect(statusReads).toEqual([])

    const ask = page.getByRole('button', { name: 'Ask Ciao to add one' })
    await ask.focus()
    await expect(ask).toBeFocused()
    const openOpencode = opencode.getByRole('button', { name: 'Open MCP settings in OpenCode' })
    if (mobile) {
      for (const target of [claude.getByRole('button', { name: 'Open MCP settings in Claude Code' }), claude.getByRole('button', { name: 'Copy' }), openOpencode, opencode.getByRole('button', { name: 'Copy' }), ask]) {
        await target.scrollIntoViewIfNeeded()
        const box = (await target.boundingBox())!
        expect(box.width).toBeGreaterThanOrEqual(44)
        expect(box.height).toBeGreaterThanOrEqual(44)
        await target.click({ trial: true })
      }
    } else {
      await page.screenshot({ path: testInfo.outputPath('mcp-status-desktop.png'), fullPage: true })
      await page.setViewportSize({ width: 640, height: 450 })
      await openOpencode.scrollIntoViewIfNeeded()
    }
    await openOpencode.focus()
    await page.keyboard.press('Enter')
    await expect(opencode.getByRole('status')).toContainText('terminal was not ready')
    await expect(opencode.locator('code')).toHaveText('opencode mcp')
    await expect(claude.locator('code')).toHaveText('claude mcp')
    await expect.poll(() => page.evaluate(() => (window as Window & { __copied?: string[] }).__copied ?? [])).toEqual(['opencode mcp'])
    const pane = page.locator('.terminal-pane')
    if (await pane.isVisible()) await pane.getByRole('button', { name: 'Close' }).click()
    await claude.getByRole('button', { name: 'Copy' }).click()
    await expect(claude.getByRole('button', { name: 'Copied' })).toBeVisible()
    await expect.poll(() => page.evaluate(() => (window as Window & { __copied?: string[] }).__copied ?? [])).toEqual(['opencode mcp', 'claude mcp'])
    expect(statusReads).toEqual([])
    expect((await horizontalOverflow(page)).overflow).toBeLessThanOrEqual(0)
    await page.screenshot({ path: testInfo.outputPath('mcp-status.png'), fullPage: true })
    await context.close()
  })
}
