import { expect, test, type Page } from '@playwright/test'
import { COMPOSER, boot, horizontalOverflow, isolate } from '../support/app'

/**
 * The command card, in a real browser.
 *
 * The assistant suggests a shell command. The card shows it as text and types
 * it into this chat's terminal at the prompt, without Enter. The user presses
 * Enter. The fixture shell holds what it was sent until a newline arrives, so
 * a command still in `pending` has not run.
 */
const CHAT_PATH = '/chat/alpha-chat-1'
const CHAT_ID = 'alpha-chat-1'
const TITLE = 'Suggested command — review before running'
const TOUCH_MIN = 44

/** Put a `command` action on the chat, before the app boots. */
async function raiseCommand(page: Page, command: string): Promise<void> {
  const res = await page.request.post('/__fixture__/chat-action', {
    data: { chat_id: CHAT_ID, kind: 'command', params: { command } },
  })
  expect(res.ok(), 'the fixture refused the action').toBe(true)
}

/** The input each shell in this browser context holds without a newline yet. */
async function pendingInputs(page: Page): Promise<string[]> {
  return page.evaluate(async () => {
    const res = await fetch('/__fixture__/terminal')
    const body = await res.json() as { sessions: { pending?: string }[] }
    return body.sessions.map(session => session.pending ?? '')
  })
}

test.describe('the command card', () => {
  test.use({ viewport: { width: 1440, height: 900 } })

  test('types the command at the prompt and waits for the user to press Enter', async ({ page }, testInfo) => {
    await isolate(page, `needs-you-command-${testInfo.workerIndex}`)
    await raiseCommand(page, 'echo needs-you')
    await boot(page, CHAT_PATH, COMPOSER)

    const card = page.locator('.needs-you-card')
    await expect(card.locator('.needs-you-card-title')).toHaveText(TITLE)
    await expect(card.locator('.needs-you-command-text code')).toHaveText('echo needs-you')

    await card.getByRole('button', { name: 'Type it in a terminal', exact: true }).click()
    await expect(page.locator('.terminal-pane')).toBeVisible()
    await expect.poll(() => pendingInputs(page)).toContain('echo needs-you')

    await expect(card.locator('.needs-you-card-title'))
      .toHaveText('Review it, then press Enter in the terminal')
    await expect(card.getByRole('button', { name: 'I ran it', exact: true })).toBeVisible()
  })

  test('shows markup in a command as text, never as an element', async ({ page }, testInfo) => {
    await isolate(page, `needs-you-command-markup-${testInfo.workerIndex}`)
    await raiseCommand(page, '<img src=x onerror=alert(1)>')
    await boot(page, CHAT_PATH, COMPOSER)

    const card = page.locator('.needs-you-card')
    await expect(card.locator('.needs-you-command-text code')).toHaveText('<img src=x onerror=alert(1)>')
    await expect(card.locator('img')).toHaveCount(0)
  })
})

test.describe('the command card on a phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true })

  test('offers 44px controls and does not scroll sideways', async ({ page }, testInfo) => {
    await isolate(page, `needs-you-command-phone-${testInfo.workerIndex}`)
    await raiseCommand(page, 'echo needs-you')
    await boot(page, CHAT_PATH, COMPOSER)

    const card = page.locator('.needs-you-card')
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
})
