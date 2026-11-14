import { expect, test, type Page } from '@playwright/test'
import { boot, fixtureWrites, horizontalOverflow, isolate, openChatDetails, seedInbox, settled } from '../support/app'

const screenshots = `${process.env.HOME}/repos/ciaobot/.loop-engineering/2026-11-13-mcp-sidebar-plan/state/screenshots-2972`
const shortcutScreenshots = `${process.env.HOME}/repos/ciaobot/.loop-engineering/2026-11-13-mcp-sidebar-plan/state/screenshots-2972`
test.use({ launchOptions: { args: ['--host-resolver-rules=MAP ciao.test 127.0.0.1'] } })

async function shortcutFixture(page: Page) {
  const frames: string[] = []
  let state = 'needs_auth'
  await page.routeWebSocket('**/ws/chat/**', ws => {
    const server = ws.connectToServer()
    ws.onMessage(message => { frames.push(String(message)); server.send(message) })
    server.onMessage(message => ws.send(message))
  })
  await page.route('**/api/mcp/status*', route => route.fulfill({ json: {
    workspace: 'alpha', providers: [{ provider: 'claude', available: true, inventory_state: 'loaded', servers: [{
      name: 'linear', scope: 'project', source: 'settings', enabled: true, allowed: true,
      connections: [{ chat_id: 'alpha-chat-1', state }],
    }] }],
  } }))
  return { frames, setState: (next: string) => { state = next } }
}

test.describe('local MCP shortcut on a touch device', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true })
  test('real bare Enter inserts a newline; Ctrl+Enter opens MCP without a message', async ({ page }) => {
    await isolate(page, 'mcp-shortcut-touch')
    const fixture = await shortcutFixture(page)
    await page.route('**/api/settings/keyboard', route => route.fulfill({ json: {
      keyboard_shortcuts: {}, keyboard_send_mode: 'enter', mid_turn_delivery: 'queue', revision: 'synthetic',
    } }))
    await boot(page, '/chat/alpha-chat-1')
    expect(await page.evaluate(() => matchMedia('(pointer: coarse)').matches)).toBe(true)
    const input = page.locator('textarea.chat-input')
    await input.fill('/McP')
    fixture.frames.length = 0
    await input.press('End')
    await input.press('Enter')
    await expect(input).toHaveValue('/McP\n')
    await expect(page.locator('.chat-rail--drawer')).toHaveCount(0)
    await input.press('Control+Enter')
    await expect(page.locator('.chat-mcp-heading')).toBeFocused()
    await expect(input).toHaveValue('')
    expect(fixture.frames.filter(frame => /"type":"(?:message|steer|queue)"/.test(frame))).toEqual([])
    expect((await fixtureWrites(page)).filter(w => /\/actions\/.*(?:start|mcp-auth)/.test(w.path))).toEqual([])
  })

  test('phone dual indicators remain independent and Escape returns focus to More', async ({ page }) => {
    await isolate(page, 'mcp-shortcut-phone')
    await seedInbox(page, { chats: { 'alpha-chat-2': { last_activity_at: '2026-01-02T10:00:00Z', last_read_at: '2026-01-01T09:05:00Z', last_snippet: 'Done' } } })
    const fixture = await shortcutFixture(page)
    fixture.setState('connected')
    await boot(page, '/chat/alpha-chat-1')
    const more = page.locator('.chat-more-btn')
    await expect(more).toHaveAttribute('aria-label', /unread notifications/)
    await expect(more.locator('.chat-more-dot')).toBeVisible()
    await expect(more.locator('.chat-mcp-indicator')).toHaveCount(0)
    const inbox = page.getByRole('dialog', { name: 'Notifications' })
    const readInbox = async () => ({
      summary: await inbox.getByTestId('inbox-summary').innerText(),
      rows: await inbox.locator('[data-testid^="row-"]').evaluateAll(rows =>
        rows.map(row => ({ id: row.getAttribute('data-testid'), text: row.textContent?.trim() })).sort((a, b) => (a.id ?? '').localeCompare(b.id ?? ''))),
    })
    await more.click()
    await page.getByRole('menuitem', { name: 'Notifications' }).click()
    await expect(inbox).toBeVisible()
    await expect(inbox.getByTestId('row-chat:alpha-chat-2')).toBeVisible()
    const baseline = await readInbox()
    await page.keyboard.press('Escape')
    await expect(inbox).toBeHidden()
    await expect(more).toBeFocused()
    fixture.setState('needs_auth')
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
    await expect(more.locator('.chat-mcp-indicator')).toBeVisible()
    await expect(more).toHaveAttribute('aria-label', /unread notifications.*1 MCP connections need attention/)
    const label = await more.getAttribute('aria-label')
    await more.click()
    await page.getByRole('menuitem', { name: 'Notifications' }).click()
    await expect(inbox).toBeVisible()
    expect(await readInbox()).toEqual(baseline)
    await page.keyboard.press('Escape')
    await expect(inbox).toBeHidden()
    await expect(more).toBeFocused()
    const box = (await more.boundingBox())!
    expect(box.width).toBeGreaterThanOrEqual(44)
    expect(box.height).toBeGreaterThanOrEqual(44)
    await more.click({ trial: true })
    await page.screenshot({ path: `${shortcutScreenshots}/phone-dual.png`, animations: 'disabled' })
    await more.click()
    const item = page.getByRole('menuitem', { name: 'Chat details', exact: true })
    await expect(item).toHaveAccessibleDescription('1 MCP connections need attention')
    await page.screenshot({ path: `${shortcutScreenshots}/phone-menu.png`, animations: 'disabled' })
    await item.click()
    await expect(page.locator('.chat-mcp-heading')).toBeFocused()
    await expect(more.locator('.chat-mcp-indicator')).toHaveCount(0)
    fixture.setState('connected')
    await page.locator('.chat-mcp-refresh').click()
    await expect(page.locator('.chat-mcp-state')).toHaveText('Connected')
    await page.keyboard.press('Escape')
    await expect(more).toBeFocused()
    await expect(more.locator('.chat-mcp-indicator')).toHaveCount(0)
    expect(await more.getAttribute('aria-label')).toBe(label?.replace(', 1 MCP connections need attention', ''))
    await more.click()
    await page.getByRole('menuitem', { name: 'Notifications' }).click()
    await expect(inbox).toBeVisible()
    expect(await readInbox()).toEqual(baseline)
    await page.keyboard.press('Escape')
    expect((await horizontalOverflow(page)).overflow).toBeLessThanOrEqual(0)
  })
})

test('marked desktop info opens MCP while unmarked opens general details', async ({ page }) => {
  await isolate(page, 'mcp-shortcut-desktop')
  const fixture = await shortcutFixture(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await boot(page, '/chat/alpha-chat-1')
  const rail = page.locator('.chat-rail')
  await expect(rail).toBeVisible()
  await page.getByRole('button', { name: 'Hide chat details' }).click()
  const info = page.getByRole('button', { name: /Show chat details, 1 MCP connections need attention/ })
  await expect(info.locator('.chat-mcp-indicator')).toBeVisible()
  await page.screenshot({ path: `${shortcutScreenshots}/desktop-marked.png`, animations: 'disabled' })
  await info.click()
  await expect(page.locator('.chat-mcp-heading')).toBeFocused()
  fixture.setState('connected')
  await page.locator('.chat-mcp-refresh').click()
  await expect(page.locator('.chat-mcp-state')).toHaveText('Connected')
  await page.getByRole('button', { name: 'Hide chat details' }).click()
  await page.getByRole('button', { name: 'Show chat details', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Hide chat details' })).toBeFocused()
  await page.setViewportSize({ width: 640, height: 450 })
  await page.getByRole('button', { name: /^More chat actions/ }).click()
  const zoomMenu = page.getByRole('menu')
  await expect(zoomMenu).toBeVisible()
  const zoomBox = (await zoomMenu.boundingBox())!
  expect(zoomBox.x + zoomBox.width).toBeLessThanOrEqual(640)
  expect(zoomBox.y + zoomBox.height).toBeLessThanOrEqual(450)
  await page.getByRole('menuitem', { name: 'Chat details', exact: true }).click()
  const close = page.getByRole('button', { name: 'Hide chat details' })
  await expect(close).toBeVisible()
  const closeBox = (await close.boundingBox())!
  expect(closeBox.width).toBeGreaterThanOrEqual(44)
  expect(closeBox.height).toBeGreaterThanOrEqual(44)
  await close.click({ trial: true })
  expect((await horizontalOverflow(page)).overflow).toBeLessThanOrEqual(0)
  await page.screenshot({ path: `${shortcutScreenshots}/narrow-640.png`, animations: 'disabled' })
})

test('streaming keeps Stop and Open MCP independent without creating a turn', async ({ page }) => {
  await isolate(page, 'mcp-shortcut-stream')
  await seedInbox(page, { chats: { 'alpha-chat-1': { pending_queue: [{ id: 'q1', text: 'already queued' }] } } })
  const fixture = await shortcutFixture(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await boot(page, '/chat/alpha-chat-1')
  await expect(page.locator('.chat-rail')).toBeVisible()
  await page.getByRole('button', { name: 'Hide chat details' }).click()
  await page.request.post('/__fixture__/chat-frame', { data: {
    chat_id: 'alpha-chat-1', frames: [{ type: 'tool_use', tool_use_id: 't1', tool_name: 'Read', tool_input: '/synthetic/file' }],
  } })
  await expect(page.getByRole('button', { name: 'Stop generation' })).toBeVisible()
  const input = page.locator('textarea.chat-input')
  await expect(page.locator('.queued-chip')).toContainText('already queued')
  const history = await page.evaluate(() => localStorage.getItem('ciao-chat-sent-prompts'))
  await input.fill(' \t/McP \n')
  await page.screenshot({ path: `${shortcutScreenshots}/streaming.png`, animations: 'disabled' })
  fixture.frames.length = 0
  await page.getByRole('button', { name: 'Stop generation' }).click()
  await expect.poll(() => fixture.frames.some(frame => JSON.parse(frame).type === 'stop')).toBe(true)
  await expect(input).toHaveValue(' \t/McP \n')
  await expect(page.locator('.chat-rail')).toHaveCount(0)
  const stopCount = fixture.frames.filter(frame => JSON.parse(frame).type === 'stop').length
  await page.getByRole('button', { name: 'Open MCP connections' }).click()
  await expect(page.locator('.chat-mcp-heading')).toBeFocused()
  expect(fixture.frames.filter(frame => JSON.parse(frame).type === 'stop')).toHaveLength(stopCount)
  expect(fixture.frames.filter(frame => ['message', 'queue', 'steer'].includes(JSON.parse(frame).type))).toEqual([])
  await expect(page.locator('.queued-chip')).toContainText('already queued')
  expect(await page.evaluate(() => localStorage.getItem('ciao-chat-sent-prompts'))).toBe(history)
  await expect(input).toHaveValue('')
})

test('local navigation remains available while an image upload is pending', async ({ page }) => {
  await isolate(page, 'mcp-shortcut-upload')
  const fixture = await shortcutFixture(page)
  let release!: () => void
  const released = new Promise<void>(resolve => { release = resolve })
  let uploads = 0
  await page.route('**/api/chats/alpha-chat-1/images', async route => {
    uploads++
    if (uploads === 1) {
      await route.fulfill({ json: [{ ref: 'img_first' }] })
      return
    }
    await released
    await route.fulfill({ json: [{ ref: 'img_e2e' }] })
  })
  await boot(page, '/chat/alpha-chat-1')
  const file = {
    name: 'shot.png', mimeType: 'image/png',
    buffer: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==', 'base64'),
  }
  await page.locator('.image-btn input').setInputFiles(file)
  await expect(page.locator('.image-preview')).toHaveCount(1)
  await page.locator('.image-btn input').setInputFiles(file)
  await expect(page.locator('.composer-upload')).toBeVisible()
  await page.locator('textarea.chat-input').fill('/mcp')
  fixture.frames.length = 0
  await page.getByRole('button', { name: 'Open MCP connections' }).click()
  await expect(page.locator('.chat-mcp-heading')).toBeFocused()
  await expect(page.locator('.composer-upload')).toBeVisible()
  await expect(page.locator('.image-preview')).toHaveCount(1)
  expect(fixture.frames.filter(frame => ['message', 'queue', 'steer'].includes(JSON.parse(frame).type))).toEqual([])
  release()
  await expect(page.locator('.image-preview')).toHaveCount(2)
})

test('local navigation works when the browser is offline', async ({ page, context }) => {
  await isolate(page, 'mcp-shortcut-offline')
  await shortcutFixture(page)
  await boot(page, '/chat/alpha-chat-1')
  await context.setOffline(true)
  await page.locator('textarea.chat-input').fill('/mcp')
  await page.getByRole('button', { name: 'Open MCP connections' }).click()
  await expect(page.locator('.chat-mcp-heading')).toBeFocused()
  await expect(page.locator('textarea.chat-input')).toHaveValue('')
  await context.setOffline(false)
})

test('a second pane opens its own provider and workspace through the chord', async ({ page }) => {
  await isolate(page, 'mcp-shortcut-two-panes')
  await seedInbox(page, { chats: { 'alpha-chat-2': { provider: 'opencode' } } })
  const fixture = await shortcutFixture(page)
  const requests: string[] = []
  page.on('request', request => { if (request.url().includes('/api/mcp/status')) requests.push(request.url()) })
  await page.setViewportSize({ width: 1440, height: 900 })
  await boot(page, '/chat/alpha-chat-1')
  const row = page.locator('.chat-item', { hasText: 'alpha second conversation' })
  await row.hover()
  await row.locator('.chat-beside-btn').click()
  const tile = page.locator('.chat-split-side--chat')
  const input = tile.locator('textarea.chat-input')
  await expect(input).toBeVisible()
  await input.fill(' \t/McP \n')
  fixture.frames.length = 0
  await input.press('Control+Enter')
  await expect(tile.locator('.chat-mcp-heading')).toBeFocused()
  await expect(tile.locator('.chat-mcp-provider')).toHaveText('OpenCode')
  await expect(input).toHaveValue('')
  expect(requests.some(url => new URL(url).searchParams.get('workspace') === 'alpha')).toBe(true)
  expect(fixture.frames.filter(frame => ['message', 'queue', 'steer'].includes(JSON.parse(frame).type))).toEqual([])
  await page.screenshot({ path: `${shortcutScreenshots}/two-panes.png`, animations: 'disabled' })
})

test('an unsupported actual provider reserves the raw token locally', async ({ page }) => {
  await isolate(page, 'mcp-shortcut-unsupported')
  await seedInbox(page, { chats: { 'alpha-chat-1': { provider: 'unsupported' } } })
  const fixture = await shortcutFixture(page)
  await boot(page, '/chat/alpha-chat-1')
  const input = page.locator('textarea.chat-input')
  const raw = ' \t/McP \n'
  await input.fill(raw)
  fixture.frames.length = 0
  await page.getByRole('button', { name: 'Why MCP details are unavailable' }).click()
  await expect(page.getByText("MCP details are unavailable for this chat's provider.")).toBeVisible()
  await input.press('Control+Enter')
  await expect(input).toHaveValue(raw)
  await page.reload()
  await expect(page.locator('textarea.chat-input')).toHaveValue(raw)
  expect(fixture.frames.filter(frame => ['message', 'queue', 'steer'].includes(JSON.parse(frame).type))).toEqual([])
  expect((await fixtureWrites(page)).filter(w => /\/actions\/.*(?:start|mcp-auth)/.test(w.path))).toEqual([])
})

test('an unresolved owning project is not rescued by the active workspace', async ({ page }) => {
  await isolate(page, 'mcp-shortcut-no-project')
  await seedInbox(page, { chats: { 'alpha-chat-1': { project_id: 'missing-project', provider: 'claude' } } })
  const fixture = await shortcutFixture(page)
  await boot(page, '/chat/alpha-chat-1', 'textarea.chat-input')
  const input = page.locator('textarea.chat-input')
  await input.fill(' \t/McP \n')
  fixture.frames.length = 0
  await page.getByRole('button', { name: 'Why MCP details are unavailable' }).click()
  await expect(page.getByText("This chat's workspace is not available yet. Try again after it loads.")).toBeVisible()
  await input.press('Control+Enter')
  await expect(input).toHaveValue(' \t/McP \n')
  expect(fixture.frames.filter(frame => ['message', 'queue', 'steer'].includes(JSON.parse(frame).type))).toEqual([])
})

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

test.describe('marked menu existing callback', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true })
  test('focuses MCP then reveals the existing waiting callback without scrolling the page', async ({ page }) => {
    const session = 'mcp-marked-existing-callback'
    await isolate(page, session)
    const fixture = await canonicalRecovery(page, 'claude')
    await fixture.update('waiting', { provider: 'claude', server: 'linear', session_id: 'sess-1', url: 'https://auth.example.invalid/authorize?state=waiting' })
    await page.context().addCookies([{ name: 'e2e_session', value: session, domain: 'ciao.test', path: '/' }])
    await page.goto(`http://ciao.test:${process.env.CIAO_E2E_PORT}/chat/alpha-chat-1`)
    await expect(page.locator('textarea.chat-input')).toBeVisible()
    const more = page.locator('.chat-more-btn')
    await expect(more.locator('.chat-mcp-indicator')).toBeVisible()
    const pageY = await page.evaluate(() => scrollY)
    await more.click()
    await page.getByRole('menuitem', { name: 'Chat details', exact: true }).click()
    await expect(page.locator('.chat-mcp-heading')).toBeFocused()
    const drawer = page.locator('.chat-rail--drawer')
    const finish = drawer.locator('.needs-you-card').getByRole('button', { name: 'Finish sign-in' })
    await expect(finish).toBeVisible()
    await expect.poll(async () => {
      const rail = await drawer.boundingBox()
      const button = await finish.boundingBox()
      if (!rail || !button || button.y < rail.y || button.y + button.height > rail.y + rail.height + 0.5) return false
      return finish.evaluate((el, [x, y]) => el.contains(document.elementFromPoint(x, y)), [
        button.x + button.width / 2, button.y + button.height / 2,
      ])
    }).toBe(true)
    expect(await page.evaluate(() => scrollY)).toBe(pageY)
  })
})

test('split-pane marked info reveals the existing callback only in its owning rail', async ({ page }) => {
  const session = 'mcp-split-existing-callback'
  await isolate(page, session)
  await page.addInitScript(() => {
    localStorage.setItem('ciao:beside-chat', JSON.stringify({ alpha: 'alpha-chat-2' }))
    const original = EventTarget.prototype.addEventListener
    EventTarget.prototype.addEventListener = function (this: EventTarget, ...args: Parameters<EventTarget['addEventListener']>) {
      if (args[0] === 'wheel' && this instanceof HTMLElement && this.matches('.messages')) this.dataset.wheelReady = 'true'
      return original.apply(this, args)
    } as typeof EventTarget.prototype.addEventListener
  })
  const fixture = await canonicalRecovery(page, 'claude')
  await fixture.update('waiting', { provider: 'claude', server: 'linear', session_id: 'sess-1', url: 'https://auth.example.invalid/authorize?state=waiting' })
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.context().addCookies([{ name: 'e2e_session', value: session, domain: 'ciao.test', path: '/' }])
  await page.goto(`http://ciao.test:${process.env.CIAO_E2E_PORT}/chat/alpha-chat-1`)
  await expect(page.locator('.chat-panel')).toHaveCount(2)
  await expect(page.locator('.chat-split-side .messages')).toHaveAttribute('data-wheel-ready', 'true')
  await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))))
  await page.evaluate(() => {
    const app = document.querySelector<HTMLElement>('#app')!
    const host = document.createElement('div')
    host.id = 'mcp-split-scroll-host'
    host.style.cssText = 'height:100vh;overflow:auto'
    app.before(host)
    const top = document.createElement('div')
    top.style.height = '120px'
    const bottom = document.createElement('div')
    bottom.style.height = '500px'
    host.append(top, app, bottom)
    const style = document.createElement('style')
    style.textContent = '.chat-rail-open { min-height: 650px !important; } .messages[data-test-scroll-other]::after { content: ""; display: block; height: 1500px; min-height: 1500px; flex: 0 0 1500px; }'
    document.head.append(style)
  })
  await settled(page.locator('.chat-layout'))
  const before = await page.evaluate(() => {
    const host = document.querySelector<HTMLElement>('#mcp-split-scroll-host')!
    host.scrollTop = 120
    const other = document.querySelector<HTMLElement>('.chat-split-side .messages')!
    other.setAttribute('data-test-scroll-other', '')
    other.dispatchEvent(new WheelEvent('wheel', { deltaY: -1 }))
    other.scrollTop = 180
    other.dispatchEvent(new Event('scroll'))
    return { page: scrollY, ancestor: host.scrollTop, other: other.scrollTop }
  })
  expect(before.ancestor).toBeGreaterThan(0)
  expect(before.other).toBeGreaterThan(0)
  const owner = page.locator('.chat-panel').first()
  const trigger = owner.getByRole('button', { name: /Show chat details, 1 MCP connections need attention/ })
  await trigger.click()
  const heading = owner.locator('.chat-mcp-heading')
  await expect(heading).toBeFocused()
  const rail = owner.locator('.chat-rail--drawer')
  const finish = rail.locator('.needs-you-card').getByRole('button', { name: 'Finish sign-in' })
  await expect.poll(async () => {
    const bounds = await rail.boundingBox()
    const button = await finish.boundingBox()
    if (!bounds || !button || button.y < bounds.y || button.y + button.height > bounds.y + bounds.height + 0.5) return false
    return finish.evaluate((el, [x, y]) => el.contains(document.elementFromPoint(x, y)), [button.x + button.width / 2, button.y + button.height / 2])
  }).toBe(true)
  expect(await rail.evaluate(el => el.scrollTop)).toBeGreaterThan(0)
  expect(await page.evaluate(() => ({
    page: scrollY,
    ancestor: document.querySelector<HTMLElement>('#mcp-split-scroll-host')!.scrollTop,
    other: document.querySelector<HTMLElement>('.chat-split-side .messages[data-test-scroll-other]')!.scrollTop,
  }))).toEqual(before)
})
