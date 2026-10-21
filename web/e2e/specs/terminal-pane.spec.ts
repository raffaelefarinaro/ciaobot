import { expect, test } from '@playwright/test'
import { COMPOSER, activeWorkspaceButton, boot, isolate } from '../support/app'

/**
 * The browser terminal's pane, in a real browser.
 *
 * jsdom cannot answer most of what this pane does: it has no xterm, no
 * layout engine, no font metrics and no focus. A keystroke has to land on
 * the terminal's own hidden textarea, the shell's output has to be drawn by
 * a real renderer, and a resize has to be measured against real cells.
 *
 * The journey is the one a person takes: open the terminal from a chat,
 * start a shell, type at it, see the answer, resize the window, hide the
 * terminal and open it again — and find the shell still there.
 */
const CHAT_PATH = '/chat/alpha-chat-1'
const OUTPUT = 'ciao-terminal'

test.use({ viewport: { width: 1440, height: 900 } })

test.beforeEach(async ({ page }, testInfo) => {
  await isolate(page, `terminal-pane-${testInfo.workerIndex}`)
})

test('the terminal opens from a chat, takes a command, and keeps its session when hidden', async ({ page }, testInfo) => {
  await boot(page, CHAT_PATH, COMPOSER)

  // The chat's Repository section is the way into the browser terminal.
  const open = page.locator('.chat-repo-section').getByRole('button', { name: 'Open in Terminal' })
  await expect(open).toBeVisible()
  await open.click()

  const pane = page.locator('.terminal-pane')
  await expect(pane).toBeVisible()
  // The pane says what it is: a shell on the engine's own machine.
  await expect(pane.locator('.tp-warning')).toContainText('not a sandbox')
  await expect(pane.locator('.tp-status')).toContainText('fixture.local')

  // No shell is running yet, and starting one is an explicit act.
  await expect(pane.locator('.tp-session')).toHaveCount(0)
  await pane.getByRole('button', { name: 'New session' }).click()
  await expect(pane.locator('.tp-session')).toHaveCount(1)

  // The shell answers.
  const surface = pane.locator('.tp-surface')
  // The keystrokes only reach the shell once the session is live: xterm's
  // own field is read-only until the pane enables the input, so that is the
  // signal to wait for rather than a fixed delay.
  const field = pane.locator('.tp-surface textarea')
  await expect(field).toBeEditable()
  await surface.click()
  await page.keyboard.type(`echo ${OUTPUT}`)
  await page.keyboard.press('Enter')
  await expect(surface).toContainText(OUTPUT)
  await page.screenshot({ path: testInfo.outputPath('terminal.png'), animations: 'disabled' })

  // The keyboard belongs to the shell while it has the focus: a digit does
  // not switch the workspace, and Escape does not close the chat.
  await page.keyboard.press('2')
  await expect(activeWorkspaceButton(page)).toContainText('Alpha')
  await page.keyboard.press('Escape')
  await expect(page.locator(COMPOSER)).toBeVisible()
  await expect(page).toHaveURL(new RegExp(CHAT_PATH))

  // The documented way out: Shift+Tab hands the keyboard to the panel's own
  // controls, and the shell never sees the key.
  await page.keyboard.press('Shift+Tab')
  await expect(pane.getByRole('button', { name: 'Hide' })).toBeFocused()

  // Hide detaches; it never ends. The shell keeps running on the engine.
  await pane.getByRole('button', { name: 'Hide' }).click()
  await expect(pane).toBeHidden()
  const sessions = await page.evaluate(async () => {
    const res = await fetch('/__fixture__/terminal')
    return (await res.json()).sessions.length
  })
  expect(sessions).toBe(1)

  // Opening it again finds the same shell, with what it printed still on
  // the screen.
  await open.click()
  await expect(pane).toBeVisible()
  await expect(pane.locator('.tp-session')).toHaveCount(1)
  await expect(pane.locator('.tp-surface')).toContainText(OUTPUT)

  // A resize is measured against real cells and told to the engine.
  const readSession = async () => page.evaluate(async () => {
    const res = await fetch('/__fixture__/terminal')
    return (await res.json()).sessions[0] as { cols: number; rows: number }
  })
  const before = await readSession()
  await page.setViewportSize({ width: 1000, height: 700 })
  await expect.poll(async () => (await readSession()).cols).not.toBe(before.cols)
})

test.describe('on a phone', () => {
  test.use({ viewport: { width: 375, height: 667 }, hasTouch: true, isMobile: true })

  test('the terminal is a full-height drawer with 44px controls', async ({ page }) => {
    await isolate(page, 'terminal-pane-phone')
    await boot(page, CHAT_PATH, COMPOSER)

    // On a narrow pane the chat details are a drawer, and the terminal's
    // button lives in it.
    await page.locator('.work-inspector-trigger').click()
    const details = page.locator('#chat-work-rail')
    await expect(details).toBeVisible()
    await details.getByRole('button', { name: 'Open in Terminal' }).click()

    const pane = page.locator('.terminal-pane')
    await expect(pane).toBeVisible()
    // A drawer, not a strip: the pane covers the page it is on.
    const box = (await pane.boundingBox())!
    expect(box.height).toBeGreaterThanOrEqual(600)
    expect(box.width).toBeGreaterThanOrEqual(360)

    // Every control in it is a 44px target.
    const controls = pane.locator('button')
    const count = await controls.count()
    expect(count).toBeGreaterThan(0)
    for (let i = 0; i < count; i += 1) {
      const size = (await controls.nth(i).boundingBox())!
      expect(size.height, `control ${i} is ${size.height}px tall`).toBeGreaterThanOrEqual(44 - 0.5)
      expect(size.width, `control ${i} is ${size.width}px wide`).toBeGreaterThanOrEqual(44 - 0.5)
    }
  })
})
