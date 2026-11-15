import { expect, test, type Page, type TestInfo } from '@playwright/test'
import { COMPOSER, openChatDetails, settled } from '../support/app'

/**
 * The needs-you stack's key control, in a real browser.
 *
 * The generic stack is capped and scrolls inside itself. MCP sign-in lives in
 * Chat details; its remote callback must remain accessible inside that panel.
 *
 * The page is served on a host name that is not loopback, which is what makes
 * the paste field appear. A session cookie for that host is set on the context;
 * the seeding POST goes to 127.0.0.1 with the cookie sent explicitly.
 */
const HOST = 'ciao.test'
const CHAT_ID = 'alpha-chat-1'
const CHAT_PATH = `/chat/${CHAT_ID}`

async function openMcpTestSurface(page: Page): Promise<ReturnType<Page['locator']>> {
  const detailsAside = page.locator('aside').filter({ has: page.getByRole('heading', { name: 'Chat details' }) })
  const hideDetails = detailsAside.getByRole('button', { name: 'Hide chat details' })
  if (!(await detailsAside.isVisible())) {
    await settled(page.locator('.chat-layout'))
    if (!(await detailsAside.isVisible())) await openChatDetails(page)
    await expect(detailsAside).toBeVisible()
  }
  await expect(hideDetails).toBeVisible()
  const rail = detailsAside.locator('.chat-mcp-connections')
  await expect(rail).toBeVisible()
  return rail
}

test.use({
  viewport: { width: 390, height: 844 },
  hasTouch: true,
  isMobile: true,
  // Let the page's host name resolve to the loopback fixture server.
  launchOptions: { args: [`--host-resolver-rules=MAP ${HOST} 127.0.0.1`] },
})

function port(): string {
  return process.env.CIAO_E2E_PORT ?? ''
}

/** Seed an action on the chat, through the loopback address with the session cookie. */
async function raise(page: Page, session: string, data: Record<string, unknown>): Promise<void> {
  const res = await page.request.post(`http://127.0.0.1:${port()}/__fixture__/chat-action`, {
    headers: { cookie: `e2e_session=${session}` },
    data: { chat_id: CHAT_ID, ...data },
  })
  expect(res.ok(), 'the fixture refused the action').toBe(true)
}

async function openOffLoopback(page: Page, session: string): Promise<void> {
  await page.context().addCookies([{ name: 'e2e_session', value: session, domain: HOST, path: '/' }])
  await page.goto(`http://${HOST}:${port()}${CHAT_PATH}`)
  await expect(page.locator(COMPOSER)).toBeVisible()
  expect(await page.evaluate(() => location.hostname)).toBe(HOST)
}

/** A remote sign-in with a callback field. */
const SIGN_IN = {
  kind: 'mcp_auth',
  state: 'waiting',
  params: {
    server: 'linear',
    provider: 'claude',
    session_id: 'sess-1',
    url: 'https://auth.example.invalid/authorize?state=w',
  },
}

test.describe('the needs-you stack on a phone', () => {
  test('an off-loopback sign-in keeps Finish sign-in within the sidebar', async ({ page }, testInfo: TestInfo) => {
    const session = `nys-primary-${testInfo.workerIndex}`
    await raise(page, session, SIGN_IN)
    await openOffLoopback(page, session)

    const root = await openMcpTestSurface(page)
    const stack = root
    const finish = root.locator('.needs-you-card').getByRole('button', { name: 'Finish sign-in' })
    await expect(finish).toBeVisible()

    // The scroll is smooth, so poll until the control is inside the stack.
    await expect.poll(async () => {
      const s = (await stack.boundingBox())!
      const f = (await finish.boundingBox())!
      return f.y + f.height <= s.y + s.height + 0.5
    }).toBe(true)

    const button = (await finish.boundingBox())!
    expect(button.y).toBeGreaterThanOrEqual(0)
    const rail = (await root.boundingBox())!
    expect(button.y + button.height).toBeLessThanOrEqual(rail.y + rail.height + 0.5)

    // Nothing covers it: the element at its centre is the button itself.
    const covered = await finish.evaluate(
      (el, [x, y]) => el.contains(document.elementFromPoint(x, y)),
      [button.x + button.width / 2, button.y + button.height / 2],
    )
    expect(covered).toBe(true)
  })

  test('a device code under a command card is scrolled into view', async ({ page }, testInfo: TestInfo) => {
    const session = `nys-device-${testInfo.workerIndex}`
    await raise(page, session, { kind: 'command', state: 'open', params: { command: 'echo review-only', note: 'Review this command' } })
    await openOffLoopback(page, session)

    const stack = page.locator('.needs-you-stack')
    await settled(stack)
    const beforeScroll = await page.evaluate(() => window.scrollY)
    const beforeFocus = await page.evaluateHandle(() => document.activeElement)
    const response = await page.request.post(`http://127.0.0.1:${port()}/__fixture__/chat-action`, {
      headers: { cookie: `e2e_session=${session}` },
      data: { chat_id: CHAT_ID, kind: 'device_code', state: 'waiting', params: { tool: 'gh', code: 'ABCD-1234', url: 'https://github.com/login/device', session_id: 's2' } },
    })
    expect(response.ok()).toBe(true)
    const { action } = await response.json() as { action: { id: string } }
    const card = page.locator(`.needs-you-card[data-action-id="${action.id}"]`)
    const code = card.locator('.needs-you-device-code')
    await expect(code).toBeVisible()

    await expect.poll(async () => {
      const s = (await stack.boundingBox())!
      const c = (await code.boundingBox())!
      return c.y + c.height <= s.y + s.height + 0.5
    }).toBe(true)

    const composer = (await page.locator(COMPOSER).boundingBox())!
    const box = (await code.boundingBox())!
    expect(box.y + box.height, 'the one-time code sits under the composer').toBeLessThanOrEqual(composer.y + 0.5)
    expect(await page.evaluate(() => window.scrollY)).toBe(beforeScroll)
    expect(await page.evaluate(el => document.activeElement === el, beforeFocus)).toBe(true)
    await beforeFocus.dispose()
    expect(await page.evaluate(() => matchMedia('(pointer: coarse)').matches)).toBe(true)
    await testInfo.attach('phone-generic-stack', { body: await page.screenshot(), contentType: 'image/png' })
  })
})

test.describe('the needs-you stack on desktop', () => {
  test.use({ viewport: { width: 1440, height: 900 }, hasTouch: false, isMobile: false })

  test('keeps a generic command above the composer', async ({ page }, testInfo: TestInfo) => {
    const session = `nys-desktop-${testInfo.workerIndex}`
    await raise(page, session, { kind: 'command', state: 'open', params: { command: 'echo review-only', note: 'Review this command' } })
    await openOffLoopback(page, session)
    const stack = page.locator('.needs-you-stack')
    await expect(stack.locator('.needs-you-card')).toBeVisible()
    const box = (await stack.boundingBox())!
    const composer = (await page.locator(COMPOSER).boundingBox())!
    expect(box.y + box.height).toBeLessThanOrEqual(composer.y + 0.5)
    await testInfo.attach('desktop-generic-stack', { body: await page.screenshot(), contentType: 'image/png' })
  })
})
