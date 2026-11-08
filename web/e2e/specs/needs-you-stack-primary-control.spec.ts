import { expect, test, type Page, type TestInfo } from '@playwright/test'
import { COMPOSER } from '../support/app'

/**
 * The needs-you stack's key control, in a real browser.
 *
 * The stack is capped at min(42vh, 380px) and scrolls inside itself. On a
 * phone a sign-in card taller than the cap showed its top only, so Finish
 * sign-in sat under the composer. jsdom cannot measure that cap, so this is a
 * browser spec: it needs the real layout of the stack against the composer.
 *
 * The page is served on a host name that is not loopback, which is what makes
 * the paste field appear. A session cookie for that host is set on the context;
 * the seeding POST goes to 127.0.0.1 with the cookie sent explicitly.
 */
const HOST = 'ciao.test'
const CHAT_ID = 'alpha-chat-1'
const CHAT_PATH = `/chat/${CHAT_ID}`

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

/** The sign-in whose card is taller than the stack's cap, as on a phone. */
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
  test('an off-loopback sign-in shows Finish sign-in above the composer', async ({ page }, testInfo: TestInfo) => {
    const session = `nys-primary-${testInfo.workerIndex}`
    await raise(page, session, SIGN_IN)
    await openOffLoopback(page, session)

    const stack = page.locator('.needs-you-stack')
    const finish = page.locator('.needs-you-card').getByRole('button', { name: 'Finish sign-in' })
    await expect(finish).toBeVisible()

    // The scroll is smooth, so poll until the control is inside the stack.
    await expect.poll(async () => {
      const s = (await stack.boundingBox())!
      const f = (await finish.boundingBox())!
      return f.y + f.height <= s.y + s.height + 0.5
    }).toBe(true)

    const composer = (await page.locator(COMPOSER).boundingBox())!
    const button = (await finish.boundingBox())!
    expect(button.y + button.height, 'Finish sign-in sits under the composer').toBeLessThanOrEqual(composer.y + 0.5)
    expect(button.y).toBeGreaterThanOrEqual(0)

    // Nothing covers it: the element at its centre is the button itself.
    const covered = await finish.evaluate(
      (el, [x, y]) => el.contains(document.elementFromPoint(x, y)),
      [button.x + button.width / 2, button.y + button.height / 2],
    )
    expect(covered).toBe(true)
  })

  test('a device code under a sign-in card is scrolled into view', async ({ page }, testInfo: TestInfo) => {
    const session = `nys-device-${testInfo.workerIndex}`
    await raise(page, session, SIGN_IN)
    await raise(page, session, {
      kind: 'device_code',
      state: 'waiting',
      params: { tool: 'gh', code: 'ABCD-1234', url: 'https://github.com/login/device', session_id: 's2' },
    })
    await openOffLoopback(page, session)

    const stack = page.locator('.needs-you-stack')
    const code = page.locator('.needs-you-device-code')
    await expect(code).toBeVisible()

    await expect.poll(async () => {
      const s = (await stack.boundingBox())!
      const c = (await code.boundingBox())!
      return c.y + c.height <= s.y + s.height + 0.5
    }).toBe(true)

    const composer = (await page.locator(COMPOSER).boundingBox())!
    const box = (await code.boundingBox())!
    expect(box.y + box.height, 'the one-time code sits under the composer').toBeLessThanOrEqual(composer.y + 0.5)
  })
})
