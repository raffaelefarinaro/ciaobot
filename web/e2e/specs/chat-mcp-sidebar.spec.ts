import { expect, test, type Page } from '@playwright/test'
import { boot, horizontalOverflow, isolate, openChatDetails, settled } from '../support/app'

const screenshots = `${process.env.HOME}/repos/ciaobot/.loop-engineering/2026-11-13-mcp-sidebar-plan/state/screenshots-2970-r2`
test.use({ launchOptions: { args: ['--host-resolver-rules=MAP ciao.test 127.0.0.1'] } })

test('Chat details shows MCP connectivity separately from tool-use history', async ({ page }) => {
  await isolate(page, 'chat-mcp-sidebar')
  await page.route('**/api/mcp/status*', route => route.fulfill({ json: {
    workspace: 'alpha',
    agent_root: '/synthetic/alpha',
    shell: { kind: 'posix', cd_prefix: 'cd -- /synthetic/alpha && ' },
    providers: [
      { provider: 'claude', available: true, inventory_state: 'loaded', servers: [
        { name: 'linear', scope: 'project', source: 'settings', kind: 'remote', enabled: true, allowed: true,
          connections: [
            { chat_id: 'alpha-chat-1', state: 'needs_auth' },
            { chat_id: 'another-chat', state: 'connected' },
          ] },
      ] },
      { provider: 'opencode', available: true, inventory_state: 'loaded', servers: [] },
    ],
  } }))
  await page.setViewportSize({ width: 1440, height: 900 })
  await boot(page, '/chat/alpha-chat-1')
  const details = page.locator('.chat-rail')
  await settled(page.locator('.chat-layout'))
  if (!(await details.isVisible())) await openChatDetails(page)
  await expect(details.locator('.chat-mcp-connections')).toBeVisible()
  await expect(details.locator('.chat-mcp-heading')).toHaveText('MCP')
  await expect(details.locator('.chat-mcp-name')).toHaveText('linear')
  await expect(details.locator('.chat-mcp-state')).toHaveText('Needs sign-in')
  await expect(details.locator('.chat-mcp-sign-in')).toBeVisible()
  // This fixture chat has no historical tool usage; live connectivity must
  // stay a separate section rather than manufacturing a history row.
  await expect(details.locator('.chat-rail-tools')).toHaveCount(0)
  await expect(details.getByRole('button', { name: 'Chat workspace MCP settings' })).toBeVisible()
  await settled(page.locator('.chat-layout'))
  await page.screenshot({ path: `${screenshots}/desktop.png`, animations: 'disabled' })

  await page.setViewportSize({ width: 390, height: 844 })
  await openChatDetails(page)
  await expect(page.locator('.chat-rail--drawer .chat-mcp-connections')).toBeVisible()
  const phone = page.locator('.chat-rail--drawer .chat-mcp-connections')
  for (const selector of ['.chat-mcp-refresh', '.chat-mcp-settings', '.chat-mcp-sign-in']) {
    const control = phone.locator(selector)
    const box = (await control.boundingBox())!
    expect(box.height).toBeGreaterThanOrEqual(44)
    expect(box.width).toBeGreaterThanOrEqual(44)
    await control.scrollIntoViewIfNeeded()
    await expect(control).toBeInViewport()
    await expect(control).toBeVisible()
    await control.click({ trial: true })
  }
  expect((await horizontalOverflow(page)).overflow).toBeLessThanOrEqual(0)

  await page.setViewportSize({ width: 640, height: 450 })
  await expect(page.locator('.chat-mcp-connections')).toBeVisible()
  expect((await horizontalOverflow(page)).overflow).toBeLessThanOrEqual(0)
  await page.screenshot({ path: `${screenshots}/zoom.png`, animations: 'disabled' })
})

test('an unreadable MCP inventory stays explicit and retryable', async ({ page }) => {
  await isolate(page, 'chat-mcp-sidebar-error')
  let reads = 0
  await page.route('**/api/mcp/status*', async route => {
    reads++
    await route.fulfill({ status: 503, json: { error: 'provider unavailable' } })
  })
  await page.setViewportSize({ width: 1280, height: 800 })
  await boot(page, '/chat/alpha-chat-1')
  const details = page.locator('.chat-rail')
  await settled(page.locator('.chat-layout'))
  if (!(await details.isVisible())) await openChatDetails(page)
  const section = details.locator('.chat-mcp-connections')
  await expect(section.locator('.chat-mcp-message')).toContainText(/could not load|unavailable/i)
  await expect(section.locator('.chat-mcp-sign-in')).toHaveCount(0)
  await section.getByRole('button', { name: 'Retry' }).click()
  await expect.poll(() => reads).toBe(2)
  await expect(section.locator('.chat-mcp-message')).toContainText(/could not load|unavailable/i)
})

test.describe('real touch geometry', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true })
  test('MCP controls have 44px targets and receive hits without horizontal overflow', async ({ page }) => {
    await isolate(page, 'mcp-touch-geometry')
    await page.route('**/api/mcp/status*', route => route.fulfill({ json: {
      workspace: 'alpha', agent_root: '/synthetic/alpha', shell: { kind: 'posix', cd_prefix: 'cd' },
      providers: [{ provider: 'claude', available: true, inventory_state: 'loaded', servers: [{
        name: 'linear', scope: 'project', source: 'settings', kind: 'remote', enabled: true, allowed: true,
        connections: [{ chat_id: 'alpha-chat-1', state: 'needs_auth' }],
      }] }],
    } }))
    await boot(page, '/chat/alpha-chat-1')
    await openChatDetails(page)
    expect(await page.evaluate(() => matchMedia('(pointer: coarse)').matches)).toBe(true)
    const section = page.locator('.chat-rail--drawer .chat-mcp-connections')
    for (const selector of ['.chat-mcp-refresh', '.chat-mcp-settings', '.chat-mcp-sign-in']) {
      const control = section.locator(selector)
      await control.scrollIntoViewIfNeeded()
      const box = (await control.boundingBox())!
      expect(box.width).toBeGreaterThanOrEqual(44)
      expect(box.height).toBeGreaterThanOrEqual(44)
      await expect(control).toBeVisible()
      await control.click({ trial: true })
    }
    expect((await horizontalOverflow(page)).overflow).toBeLessThanOrEqual(0)
    await page.locator('.chat-rail--drawer').screenshot({ path: `${screenshots}/phone.png`, animations: 'disabled' })
  })
})

test('a delayed MCP authorization URL survives drawer close, reconnect and reload without reopening a popup', async ({ page }) => {
  const chatId = 'alpha-chat-1'
  await isolate(page, 'chat-mcp-sidebar-delayed-url')
  const seeded = await page.request.post('/__fixture__/chat-action', {
    data: { chat_id: chatId, kind: 'mcp_auth', state: 'open', params: { provider: 'claude', server: 'linear' } },
  })
  expect(seeded.ok()).toBe(true)
  const created = await seeded.json()
  let canonicalActions = [created.action as Record<string, unknown>]
  const copyActions = () => structuredClone(canonicalActions)
  const publishActions = async () => {
    const response = await page.request.post('/__fixture__/inbox', {
      data: { events: [{ type: 'chat_actions_changed', chat_id: chatId, actions: copyActions() }] },
    })
    expect(response.ok()).toBe(true)
  }

  await page.addInitScript(() => {
    const opened: Array<{ args: unknown[]; tab: { closed: boolean; opener: unknown; location: { href: string }; close(): void } }> = []
    ;(window as Window & { __mcpPopups?: typeof opened }).__mcpPopups = opened
    window.open = ((...args: unknown[]) => {
      const tab = { closed: false, opener: window as unknown, location: { href: '' }, close() { this.closed = true } }
      opened.push({ args, tab })
      return tab
    }) as unknown as typeof window.open
  })
  await page.routeWebSocket('**/ws/events', ws => {
    const server = ws.connectToServer()
    server.onMessage(message => {
      if (typeof message === 'string') {
        try {
          const frame = JSON.parse(message)
          if (frame.type === 'snapshot') {
            frame.chat_actions ??= {}
            frame.chat_actions[chatId] = copyActions()
          } else if (frame.type === 'chat_actions_changed' && frame.chat_id === chatId) {
            frame.actions = copyActions()
          }
          ws.send(JSON.stringify(frame))
          return
        } catch { /* Forward non-JSON fixture frames unchanged. */ }
      }
      ws.send(message)
    })
    ws.onMessage(message => server.send(message))
  })

  const chatPaths = new Set(['/api/chats', `/api/chats/${chatId}`, '/api/projects/alpha-general/chats'])
  await page.route('**/api/**', async route => {
    const request = route.request()
    const url = new URL(request.url())
    if (request.method() === 'GET' && chatPaths.has(url.pathname)) {
      const upstream = new URL(request.url())
      upstream.hostname = '127.0.0.1'
      const response = await route.fetch({ url: upstream.toString() })
      const data = await response.json() as Record<string, unknown> | Array<Record<string, unknown>>
      const add = (chat: Record<string, unknown>) => chat.chat_id === chatId ? { ...chat, actions: copyActions() } : chat
      await route.fulfill({ response, json: Array.isArray(data) ? data.map(add) : add(data) })
      return
    }
    if (request.method() === 'POST' && url.pathname === `/api/chats/${chatId}/actions/${String(canonicalActions[0]?.id)}/start`) {
      expect(request.postDataJSON()).toEqual({})
      canonicalActions = [{ ...canonicalActions[0]!, state: 'working', params: { provider: 'claude', server: 'linear', session_id: 'sess-1' } }]
      await route.fulfill({ json: { action: copyActions()[0] } })
      await publishActions()
      return
    }
    await route.fallback()
  })
  await page.route('**/api/mcp/status*', route => route.fulfill({ json: {
    workspace: 'alpha', agent_root: '/synthetic/alpha', shell: { kind: 'posix', cd_prefix: 'cd -- /synthetic/alpha && ' },
    providers: [{ provider: 'claude', available: true, inventory_state: 'loaded', servers: [
      { name: 'linear', scope: 'project', source: 'settings', kind: 'remote', enabled: true, allowed: true,
        connections: [{ chat_id: chatId, state: 'needs_auth' }] },
    ] }, { provider: 'opencode', available: true, inventory_state: 'loaded', servers: [] }],
  } }))

  await page.setViewportSize({ width: 390, height: 844 })
  await boot(page, `/chat/${chatId}`)
  await openChatDetails(page)
  const section = page.locator('.chat-rail--drawer .chat-mcp-connections')
  await expect(section).toBeVisible()
  await section.getByRole('button', { name: 'Sign in' }).click()
  await expect.poll(() => page.evaluate(() => (window as Window & { __mcpPopups?: unknown[] }).__mcpPopups?.length ?? 0)).toBe(1)
  await expect.poll(() => canonicalActions[0]?.state).toBe('working')
  const hide = page.getByRole('button', { name: 'Hide chat details' })
  await hide.click()
  await expect.poll(() => page.evaluate(() => (window as Window & { __mcpPopups?: Array<{ tab: { closed: boolean } }> }).__mcpPopups?.[0]?.tab.closed)).toBe(true)

  canonicalActions = [{ ...canonicalActions[0]!, state: 'waiting', params: {
    provider: 'claude', server: 'linear', session_id: 'sess-1',
    url: 'https://auth.example.invalid/authorize?state=waiting',
  } }]
  await publishActions()
  await openChatDetails(page)
  await expect(page.locator('.chat-rail--drawer .needs-you-card a')).toHaveAttribute('href', /auth\.example\.invalid/)
  expect(await page.evaluate(() => (window as Window & { __mcpPopups?: Array<{ tab: { location: { href: string } } }> }).__mcpPopups?.[0]?.tab.location.href)).toBe('')

  const before = (await (await page.request.get('/__fixture__/ws-count')).json()).connections as number
  await page.request.post('/__fixture__/drop-ws')
  await expect.poll(async () => (await (await page.request.get('/__fixture__/ws-count')).json()).connections as number).toBeGreaterThan(before)
  await expect(page.locator('.chat-rail--drawer .needs-you-card a')).toHaveAttribute('href', /auth\.example\.invalid/)
  await page.reload()
  await expect(page.locator('textarea.chat-input')).toBeVisible()
  await openChatDetails(page)
  await expect(page.locator('.chat-rail--drawer .needs-you-card a')).toHaveAttribute('href', /auth\.example\.invalid/)
  expect(await page.locator('.chat-rail--drawer .needs-you-card').getAttribute('data-action-id')).toBe(created.action.id)
})

async function canonicalRecovery(page: Page, provider: 'claude' | 'opencode') {
  const chatId = 'alpha-chat-1'
  const seeded = await page.request.post('/__fixture__/chat-action', {
    data: { chat_id: chatId, kind: 'mcp_auth', state: 'open', params: { provider, server: 'linear' } },
  })
  expect(seeded.ok()).toBe(true)
  const initial = (await seeded.json()).action
  type Action = { id: string; state: string; params: Record<string, string>; [key: string]: unknown }
  let actions: Action[] = [initial]
  let refuseCallback = true
  const copy = () => structuredClone(actions)
  const publish = async () => {
    const response = await page.request.post('/__fixture__/inbox', {
      data: { events: [{ type: 'chat_actions_changed', chat_id: chatId, actions: copy() }] },
    })
    expect(response.ok()).toBe(true)
  }
  const update = async (state: string, params: Record<string, string>) => {
    actions = [{ ...actions[0]!, state, params }]
    await publish()
  }
  await page.addInitScript(() => {
    ;(window as Window & { __popupCalls?: number }).__popupCalls = 0
    window.open = (() => {
      const target = window as Window & { __popupCalls?: number }
      target.__popupCalls = (target.__popupCalls ?? 0) + 1
      return null
    }) as typeof window.open
  })
  await page.routeWebSocket('**/ws/events', ws => {
    const server = ws.connectToServer()
    server.onMessage(message => {
      if (typeof message === 'string') {
        try {
          const frame = JSON.parse(message)
          if (frame.type === 'snapshot') {
            frame.chat_actions ??= {}
            frame.chat_actions[chatId] = copy()
          } else if (frame.type === 'chat_actions_changed' && frame.chat_id === chatId) {
            frame.actions = copy()
          }
          ws.send(JSON.stringify(frame))
          return
        } catch { /* Pass other fixture frames through unchanged. */ }
      }
      ws.send(message)
    })
    ws.onMessage(message => server.send(message))
  })
  const chatPaths = new Set(['/api/chats', `/api/chats/${chatId}`, '/api/projects/alpha-general/chats'])
  await page.route('**/api/**', async route => {
    const request = route.request()
    const pathname = new URL(request.url()).pathname
    if (request.method() === 'GET' && chatPaths.has(pathname)) {
      const upstream = new URL(request.url())
      upstream.hostname = '127.0.0.1'
      const response = await route.fetch({ url: upstream.toString() })
      const data = await response.json() as Record<string, unknown> | Array<Record<string, unknown>>
      const amend = (chat: Record<string, unknown>) => chat.chat_id === chatId
        ? { ...chat, provider, actions: copy() } : chat
      await route.fulfill({ response, json: Array.isArray(data) ? data.map(amend) : amend(data) })
      return
    }
    const base = `/api/chats/${chatId}/actions/`
    if (request.method() !== 'POST' || !pathname.startsWith(base)) return route.fallback()
    if (pathname === `${base}mcp-auth`) {
      expect(request.postDataJSON()).toEqual({ server: 'linear' })
      await route.fulfill({ json: { action: copy()[0] } })
      return
    }
    const match = pathname.slice(base.length).match(/^([^/]+)\/(start|callback|dismiss|continue|turn-off)$/)
    if (!match) return route.fallback()
    if (!actions[0] || match[1] !== actions[0].id) {
      await route.fulfill({ status: 409, json: { error: 'chat_action_conflict', actions: copy() } })
      return
    }
    if (match[2] === 'start') {
      expect(request.postDataJSON()).toEqual({})
      actions = [{ ...actions[0], state: 'working', params: { provider, server: 'linear', session_id: 'sess-1' } }]
      await route.fulfill({ json: { action: copy()[0] } })
    } else if (match[2] === 'callback') {
      expect(request.postDataJSON()).toEqual({ url: 'http://localhost:1234/callback?code=dummy&state=test' })
      if (refuseCallback) {
        refuseCallback = false
        await route.fulfill({ status: 400, json: { error: 'state_mismatch', action: copy()[0] } })
        return
      }
      actions = [{ ...actions[0], params: { ...actions[0].params, callback_used: '1' } }]
      await route.fulfill({ json: { action: copy()[0] } })
    } else {
      expect(request.postDataJSON()).toEqual({})
      actions = []
      await route.fulfill({ json: { actions: [], delivery: 'sent', reload: {} } })
    }
    await publish()
  })
  await page.route('**/api/mcp/status*', route => route.fulfill({ json: {
    workspace: 'alpha', agent_root: '/synthetic/alpha', shell: { kind: 'posix', cd_prefix: 'cd -- /synthetic/alpha && ' },
    providers: [{ provider, available: true, inventory_state: 'loaded', servers: [
      { name: 'linear', scope: 'project', source: 'settings', kind: 'remote', enabled: true, allowed: true,
        connections: [{ chat_id: chatId, state: 'needs_auth' }, { chat_id: 'other-chat', state: 'connected' }] },
    ] }],
  } }))
  return { update, publish, actions: () => copy(), id: initial.id as string }
}

for (const provider of ['claude', 'opencode'] as const) {
  test.describe(`${provider} canonical managed recovery`, () => {
    test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true })

    test('blocked popup, callback refusal and success, verification and continue survive reload', async ({ page }) => {
      const session = `mcp-canonical-${provider}`
      await isolate(page, session)
      const fixture = await canonicalRecovery(page, provider)
      await page.context().addCookies([{ name: 'e2e_session', value: session, domain: 'ciao.test', path: '/' }])
      await page.goto(`http://ciao.test:${process.env.CIAO_E2E_PORT}/chat/alpha-chat-1`)
      await expect(page.locator('textarea.chat-input')).toBeVisible()
      await openChatDetails(page)
      const section = page.locator('.chat-rail--drawer .chat-mcp-connections')
      await section.getByRole('button', { name: 'Sign in', exact: true }).click()
      expect(await page.evaluate(() => (window as Window & { __popupCalls?: number }).__popupCalls)).toBe(1)
      await expect.poll(() => fixture.actions()[0]?.state).toBe('working')
      await fixture.update('waiting', { provider, server: 'linear', session_id: 'sess-1', url: 'https://auth.example.invalid/authorize?state=waiting' })
      await expect(section.locator('.needs-you-card a')).toHaveAttribute('href', /auth\.example\.invalid/)
      expect(await page.evaluate(() => (window as Window & { __popupCalls?: number }).__popupCalls)).toBe(1)
      const field = section.locator('input[type=url]')
      const redirect = 'http://localhost:1234/callback?code=dummy&state=test'
      await field.fill(redirect)
      await section.getByRole('button', { name: 'Finish sign-in' }).click()
      await expect(section.getByRole('alert')).toContainText('different sign-in')
      await expect(field).toHaveValue('')
      await field.fill(redirect)
      await section.getByRole('button', { name: 'Finish sign-in' }).click()
      await expect.poll(() => fixture.actions()[0]?.params.callback_used).toBe('1')
      expect(fixture.actions()[0]?.state).toBe('waiting')
      const before = (await (await page.request.get('/__fixture__/ws-count')).json()).connections as number
      await page.request.post('/__fixture__/drop-ws')
      await expect.poll(async () => (await (await page.request.get('/__fixture__/ws-count')).json()).connections as number).toBeGreaterThan(before)
      await expect(section.locator('.needs-you-card a')).toHaveAttribute('href', /auth\.example\.invalid/)
      await page.reload()
      await expect(page.locator('textarea.chat-input')).toBeVisible()
      await openChatDetails(page)
      await expect(section.locator('.needs-you-card')).toHaveAttribute('data-action-id', fixture.id)
      await expect(section.locator('.needs-you-card a')).toHaveAttribute('href', /auth\.example\.invalid/)
      await fixture.update('done', { provider, server: 'linear' })
      await section.getByRole('button', { name: /Continue/ }).click()
      await expect(section.locator('.needs-you-card')).toHaveCount(0)
      expect(fixture.actions()).toEqual([])
    })

    for (const verb of ['dismiss', 'turn-off'] as const) {
      test(`${verb} removes canonical action and restores focus`, async ({ page }) => {
        await isolate(page, `mcp-canonical-${provider}-${verb}`)
        const fixture = await canonicalRecovery(page, provider)
        await boot(page, '/chat/alpha-chat-1')
        await openChatDetails(page)
        const section = page.locator('.chat-rail--drawer .chat-mcp-connections')
        const button = verb === 'dismiss' ? section.getByRole('button', { name: 'Not now' }) : section.getByRole('button', { name: 'Turn off linear' })
        await button.focus()
        await button.click()
        await expect(section.locator('.needs-you-card')).toHaveCount(0)
        expect(fixture.actions()).toEqual([])
        await expect(section.locator('.chat-mcp-heading')).toBeFocused()
        await page.reload()
        await expect(page.locator('textarea.chat-input')).toBeVisible()
        await openChatDetails(page)
        await expect(section.locator('.needs-you-card')).toHaveCount(0)
      })
    }
  })
}
