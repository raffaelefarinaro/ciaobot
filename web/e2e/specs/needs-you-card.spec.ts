import { existsSync } from 'node:fs'
import { expect, test, type Page } from '@playwright/test'
import { COMPOSER, boot, horizontalOverflow, isolate, openChatDetails, settled } from '../support/app'

/**
 * The needs-you card, in a real browser.
 *
 * A Claude MCP server that needs a sign-in shows sidebar-managed OAuth.
 * jsdom cannot answer the layout, the pop-up or touch targets.
 */
const CHAT_PATH = '/chat/alpha-chat-1'
const CHAT_ID = 'alpha-chat-1'
const TOUCH_MIN = 44
const HAS_MCP_SIDEBAR = existsSync(new URL('../../src/components/ChatMcpConnections.vue', import.meta.url))

/** Put an `mcp_auth` action on the chat, before the app boots. */
async function raiseSignIn(page: Page, state?: 'waiting'): Promise<void> {
  const params: Record<string, string> = { server: 'linear', provider: 'claude' }
  if (state === 'waiting') params.url = 'https://auth.example.invalid/authorize?state=waiting'
  const res = await page.request.post('/__fixture__/chat-action', {
    data: { chat_id: CHAT_ID, kind: 'mcp_auth', state, params },
  })
  expect(res.ok(), 'the fixture refused the action').toBe(true)
}

async function openMcpTestSurface(page: Page): Promise<{ root: ReturnType<Page['locator']>; sidebar: boolean }> {
  const detailsAside = page.locator('aside').filter({ has: page.getByRole('heading', { name: 'Chat details' }) })
  const hideDetails = detailsAside.getByRole('button', { name: 'Hide chat details' })
  if (!(await detailsAside.isVisible())) {
    await settled(page.locator('.chat-layout'))
    if (!(await detailsAside.isVisible())) await openChatDetails(page)
    await expect(detailsAside).toBeVisible()
  }
  await expect(hideDetails).toBeVisible()
  const rail = detailsAside.locator('.chat-mcp-connections')
  if (HAS_MCP_SIDEBAR) {
    await expect(rail).toBeVisible()
    return { root: rail, sidebar: true }
  }
  await expect(rail).toHaveCount(0)
  await hideDetails.click()
  return { root: page.locator('.chat-column .needs-you-stack'), sidebar: false }
}

interface PopupRecord { args: unknown[]; href: string; opener: boolean }
type PopupTab = { closed: boolean; opener: unknown; location: { href: string }; close(): void }

/** Record the pop-ups the page opens, instead of opening them. The stub hands back a fake tab. */
async function recordPopups(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const opened: { args: unknown[]; tab: PopupTab }[] = []
    ;(window as Window & { __opened?: typeof opened }).__opened = opened
    window.open = ((...args: unknown[]) => {
      const tab = { closed: false, opener: window as unknown, location: { href: '' }, close() { this.closed = true } }
      opened.push({ args, tab })
      return tab
    }) as unknown as typeof window.open
  })
}

async function popups(page: Page): Promise<PopupRecord[]> {
  return page.evaluate(() => {
    const opened = (window as Window & { __opened?: { args: unknown[]; tab: PopupTab }[] }).__opened ?? []
    return opened.map(({ args, tab }) => ({ args, href: tab.location.href, opener: tab.opener === null }))
  })
}

test.describe('sidebar-managed OAuth', () => {
  test.use({ viewport: { width: 1440, height: 900 } })

  test('asks for a sign-in, waits for it, and goes when dismissed', async ({ page }, testInfo) => {
    await isolate(page, `needs-you-card-${testInfo.workerIndex}`)
    await recordPopups(page)
    await raiseSignIn(page)
    await boot(page, CHAT_PATH, COMPOSER)

    const { root, sidebar } = await openMcpTestSurface(page)
    const card = root.locator('.needs-you-card')
    await expect(card).toBeVisible()
    await expect(card.locator('.needs-you-card-title')).toHaveText('Sign in to linear')
    if (sidebar) {
      const rail = await root.boundingBox()
      const box = await card.boundingBox()
      expect(box!.y).toBeGreaterThanOrEqual(rail!.y)
      expect(box!.y + box!.height).toBeLessThanOrEqual(rail!.y + rail!.height)
    } else {
      const cardBox = await card.boundingBox()
      const composerBox = await page.locator(COMPOSER).boundingBox()
      expect(cardBox!.y + cardBox!.height).toBeLessThanOrEqual(composerBox!.y + 1)
    }

    await card.getByRole('button', { name: 'Sign in', exact: true }).click()

    // A tab opened in the click, lost its opener, and went to the address the engine sent.
    await expect.poll(() => popups(page)).toHaveLength(1)
    expect((await popups(page))[0].args).toEqual(['', '_blank'])
    await expect.poll(async () => (await popups(page))[0].href).toMatch(/^https:\/\/auth\.example\.invalid\/authorize/)
    expect((await popups(page))[0].opener).toBe(true)
    await expect(card.locator('.needs-you-steps li').first()).toHaveText('Sign-in page opened')

    // The wait is shown, with a link the user can use again.
    await expect(card.locator('.needs-you-card-title')).toHaveText('Waiting for linear to connect…')
    await expect(card.getByRole('link', { name: 'Open the sign-in page' })).toHaveAttribute('target', '_blank')

    await card.getByRole('button', { name: 'Not now', exact: true }).click()
    await expect(root.locator('.needs-you-card')).toHaveCount(0)
  })

  test('Turn off takes the card away', async ({ page }, testInfo) => {
    await isolate(page, `needs-you-turn-off-${testInfo.workerIndex}`)
    await raiseSignIn(page)
    await boot(page, CHAT_PATH, COMPOSER)

    const { root } = await openMcpTestSurface(page)
    const card = root.locator('.needs-you-card')
    await expect(card).toBeVisible()
    await expect(card.getByRole('button', { name: 'Turn off linear' })).toBeVisible()
    await expect(card.locator('.needs-you-turn-off-scope')).toContainText('Claude Code')

    await card.getByRole('button', { name: 'Turn off linear' }).click()
    await expect(root.locator('.needs-you-card')).toHaveCount(0)
  })
})

test.describe('sidebar-managed OAuth on a phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true })

  test('offers 44px controls and does not scroll sideways', async ({ page }, testInfo) => {
    await isolate(page, `needs-you-card-phone-${testInfo.workerIndex}`)
    await raiseSignIn(page)
    await boot(page, CHAT_PATH, COMPOSER)

    // A coarse pointer, so the touch rules are the ones under test.
    expect(await page.evaluate(() => matchMedia('(pointer: coarse)').matches)).toBe(true)

    const { root } = await openMcpTestSurface(page)
    const card = root.locator('.needs-you-card')
    await expect(card).toBeVisible()
    const { overflow, culprits } = await horizontalOverflow(page)
    expect(
      overflow,
      `the phone layout scrolls ${overflow}px sideways; widest unclipped: ${JSON.stringify(culprits)}`,
    ).toBeLessThanOrEqual(0)

    const controls = card.locator('button')
    const count = await controls.count()
    expect(count).toBeGreaterThan(0)
    for (let i = 0; i < count; i += 1) {
      const size = (await controls.nth(i).boundingBox())!
      expect(size.height, `control ${i} is ${size.height}px tall`).toBeGreaterThanOrEqual(TOUCH_MIN - 0.5)
      expect(size.width, `control ${i} is ${size.width}px wide`).toBeGreaterThanOrEqual(TOUCH_MIN - 0.5)
    }
  })

  test('keeps the waiting link 44px and clear of the step list', async ({ page }, testInfo) => {
    await isolate(page, `needs-you-card-phone-waiting-${testInfo.workerIndex}`)
    await raiseSignIn(page, 'waiting')
    await boot(page, CHAT_PATH, COMPOSER)

    const { root } = await openMcpTestSurface(page)
    const card = root.locator('.needs-you-card')
    await expect(card).toBeVisible()
    const { overflow, culprits } = await horizontalOverflow(page)
    expect(
      overflow,
      `the phone layout scrolls ${overflow}px sideways; widest unclipped: ${JSON.stringify(culprits)}`,
    ).toBeLessThanOrEqual(0)

    const link = card.locator('a.needs-you-btn')
    const linkBox = (await link.boundingBox())!
    expect(linkBox.height, `the sign-in link is ${linkBox.height}px tall`).toBeGreaterThanOrEqual(TOUCH_MIN - 0.5)
    expect(linkBox.width, `the sign-in link is ${linkBox.width}px wide`).toBeGreaterThanOrEqual(TOUCH_MIN - 0.5)

    const stepsBox = (await card.locator('.needs-you-steps').boundingBox())!
    const gap = linkBox.y - (stepsBox.y + stepsBox.height)
    expect(gap, `the link sits ${gap}px below the steps`).toBeGreaterThanOrEqual(8)

    const controls = card.locator('button')
    const count = await controls.count()
    for (let i = 0; i < count; i += 1) {
      const size = (await controls.nth(i).boundingBox())!
      expect(size.height, `control ${i} is ${size.height}px tall`).toBeGreaterThanOrEqual(TOUCH_MIN - 0.5)
      expect(size.width, `control ${i} is ${size.width}px wide`).toBeGreaterThanOrEqual(TOUCH_MIN - 0.5)
    }
  })
})
