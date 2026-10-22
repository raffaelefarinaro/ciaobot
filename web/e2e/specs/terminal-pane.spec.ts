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
/**
 * A chat whose project has no linked repository — the fixture's notes project.
 * The terminal is not a repository feature, so this chat has one too.
 */
const NO_REPO_CHAT_PATH = '/chat/alpha-chat-2'
const OUTPUT = 'ciao-terminal'

test.use({ viewport: { width: 1440, height: 900 } })

test.beforeEach(async ({ page }, testInfo) => {
  await isolate(page, `terminal-pane-${testInfo.workerIndex}`)
})

test('the terminal opens from a chat, takes a command, and keeps its session when hidden', async ({ page }, testInfo) => {
  await boot(page, CHAT_PATH, COMPOSER)

  // The chat's own Terminal section is the way into the browser terminal: it
  // is outside the repository's, so a chat without one has it as well.
  const open = page.locator('.chat-rail-terminal').getByRole('button', { name: 'Open in Terminal' })
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

test('the terminal docks on the right of the chat when the pane holds both columns', async ({ page }) => {
  // 1280×900 with the sidebar open leaves the pane 940px: room for a chat
  // column and the dock's floor, so the dock is the shape it takes.
  await page.setViewportSize({ width: 1280, height: 900 })
  await boot(page, CHAT_PATH, COMPOSER)

  // At this width the chat details are a drawer, so the terminal's way in
  // is one click deeper.
  await page.locator('.work-inspector-trigger').click()
  const details = page.locator('#chat-work-rail')
  await expect(details).toBeVisible()
  await details.locator('.chat-rail-terminal').getByRole('button', { name: 'Open in Terminal' }).click()

  const pane = page.locator('.terminal-pane')
  await expect(pane).toBeVisible()
  // The wide shape is a column beside the chat, the way the pinned file
  // panel is one — not a strip under the conversation.
  await expect(pane).toHaveClass(/terminal-pane--dock/)

  // The chat column is the box the dock stands beside. ChatPanel has a
  // .chat-body of its own, so the column is named by where it sits.
  const column = page.locator('.chat-main > .chat-body')
  const main = page.locator('.chat-main')
  const columnBox = (await column.boundingBox())!
  const paneBox = (await pane.boundingBox())!
  const mainBox = (await main.boundingBox())!

  // To the right of the chat, not under it.
  expect(paneBox.x).toBeGreaterThanOrEqual(columnBox.x + columnBox.width - 1)
  // The full height of the pane: a shell wants the vertical room.
  expect(paneBox.height).toBeGreaterThanOrEqual(mainBox.height - 1)
  // The width it asks for: the clamp, between its floor and its ceiling.
  expect(paneBox.width).toBeGreaterThanOrEqual(360)
  expect(paneBox.width).toBeLessThanOrEqual(620)
  // And the chat keeps a column worth reading: the dock never squeezes the
  // conversation to a sliver.
  expect(columnBox.width).toBeGreaterThanOrEqual(360)
})

test('a pane too narrow for both columns gets the drawer, not a squeezed chat', async ({ page }) => {
  // 900×800 with the sidebar open leaves the pane 560px: too narrow for a
  // chat column and the dock's 360px floor side by side.
  await page.setViewportSize({ width: 900, height: 800 })
  await boot(page, CHAT_PATH, COMPOSER)
  await page.locator('.work-inspector-trigger').click()
  const details = page.locator('#chat-work-rail')
  await expect(details).toBeVisible()
  await details.locator('.chat-rail-terminal').getByRole('button', { name: 'Open in Terminal' }).click()

  const pane = page.locator('.terminal-pane')
  await expect(pane).toBeVisible()
  // The drawer, not a dock in a pane that cannot hold it.
  await expect(pane).toHaveClass(/terminal-pane--drawer/)

  const main = page.locator('.chat-main')
  const column = page.locator('.chat-main > .chat-body')
  const mainBox = (await main.boundingBox())!
  const paneBox = (await pane.boundingBox())!
  const columnBox = (await column.boundingBox())!

  // The drawer covers the pane it is in.
  expect(paneBox.width).toBeGreaterThanOrEqual(mainBox.width - 1)
  expect(paneBox.height).toBeGreaterThanOrEqual(mainBox.height - 1)
  // The chat keeps the width it had: the drawer is over the chat, not beside
  // it, so nothing is taken from the conversation.
  expect(columnBox.width).toBeGreaterThanOrEqual(mainBox.width - 1)

  // And hiding the terminal gives the chat back exactly what it had.
  await pane.getByRole('button', { name: 'Hide' }).click()
  await expect(pane).toBeHidden()
  const after = (await column.boundingBox())!
  expect(after.width).toBeGreaterThanOrEqual(columnBox.width - 1)
})

test('a chat with no repository opens the terminal too', async ({ page }) => {
  // The engine is asked about this chat's own sessions, so the requests are
  // watched rather than the answer's shape.
  const scopeQueries: string[] = []
  page.on('request', request => {
    const url = new URL(request.url())
    if (url.pathname === '/api/terminal/sessions') scopeQueries.push(url.search)
  })

  await boot(page, NO_REPO_CHAT_PATH, COMPOSER)

  // The chat's project has no linked folder, so there is no Repository
  // section — and the terminal has a section of its own.
  await expect(page.locator('.chat-repo-section')).toHaveCount(0)
  const section = page.locator('.chat-rail-terminal')
  await expect(section).toBeVisible()
  const open = section.getByRole('button', { name: 'Open in Terminal' })
  await expect(open).toBeVisible()
  await open.click()

  const pane = page.locator('.terminal-pane')
  await expect(pane).toBeVisible()
  await expect(pane.locator('.tp-warning')).toContainText('not a sandbox')
  // The pane is scoped to this chat, not to another one.
  await expect.poll(() => scopeQueries.join(' ')).toContain('chat_id=alpha-chat-2')

  // A shell starts here like any other: opening the panel starts nothing.
  await expect(pane.locator('.tp-session')).toHaveCount(0)
  await pane.getByRole('button', { name: 'New session' }).click()
  await expect(pane.locator('.tp-session')).toHaveCount(1)
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
    await details.locator('.chat-rail-terminal').getByRole('button', { name: 'Open in Terminal' }).click()

    const pane = page.locator('.terminal-pane')
    await expect(pane).toBeVisible()
    // The chat details step aside: the terminal is the whole screen now, and
    // a modal drawer on top of it would be a dead, inert pane.
    await expect(details).toHaveCount(0)
    // The keyboard goes back to the control that opened the drawer — the
    // chat details tab — because the button that was pressed left with it.
    // The terminal takes the keyboard only once it has a screen, which is
    // when a session starts; the desktop journey covers that.
    await expect(page.locator('.work-inspector-trigger')).toBeFocused()
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
