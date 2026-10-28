import { expect, test, type Locator, type Page } from '@playwright/test'
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

/** The window the slot's tile is drawn in: its chrome, measured. */
interface TileChrome {
  margin: string
  radius: string
  border: string
  background: string
  /** The shadow as a value, not as a yes: a frame of the terminal's own
   *  would be a different shadow, and a boolean would call it equal. */
  shadow: string
}

/** The computed chrome of the slot's tile. */
async function readTileChrome(tile: Locator): Promise<TileChrome> {
  return tile.evaluate(el => {
    const style = getComputedStyle(el)
    return {
      margin: style.margin,
      radius: style.borderRadius,
      border: style.borderTopWidth,
      background: style.backgroundColor,
      shadow: style.boxShadow,
    }
  })
}

/** What a design token resolves to on screen, so a tile can be compared to it. */
async function readTokenBackground(page: Page, token: string): Promise<string> {
  return page.evaluate((name) => {
    const probe = document.createElement('div')
    probe.style.background = `var(${name})`
    document.body.appendChild(probe)
    const value = getComputedStyle(probe).backgroundColor
    probe.remove()
    return value
  }, token)
}

/** Drag the slot's divider to an absolute x, the way a person drags it. */
async function dragDivider(page: Page, divider: Locator, toX: number): Promise<void> {
  const box = (await divider.boundingBox())!
  const y = box.y + box.height / 2
  await page.mouse.move(box.x + box.width / 2, y)
  await page.mouse.down()
  await page.mouse.move(toX, y, { steps: 8 })
  await page.mouse.up()
}

test.use({ viewport: { width: 1440, height: 900 } })

test.beforeEach(async ({ page }, testInfo) => {
  await isolate(page, `terminal-pane-${testInfo.workerIndex}`)
})

test('the terminal opens from a chat, takes a command, and keeps its session when closed', async ({ page }, testInfo) => {
  await boot(page, CHAT_PATH, COMPOSER)

  // The chat's own Terminal section is the way into the browser terminal: it
  // is outside the repository's, so a chat without one has it as well.
  const open = page.locator('.chat-rail-terminal').getByRole('button', { name: 'Open terminal' })
  await expect(open).toBeVisible()
  await open.click()

  const pane = page.locator('.terminal-pane')
  await expect(pane).toBeVisible()
  // The pane says what it is: a shell on the engine's own machine.
  await expect(pane.locator('.tp-warning')).toContainText('not a sandbox')
  await expect(pane.locator('.tp-status')).toContainText('fixture.local')

  // No shell is running yet, and starting one is an explicit act.
  await expect(pane.getByRole('tab')).toHaveCount(0)
  await pane.getByRole('button', { name: 'New terminal session' }).click()
  await expect(pane.getByRole('tab')).toHaveCount(1)

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

  // A pointer with a keyboard behind it has no use for on-screen keys: the
  // row is rendered for the layout but hidden until a coarse pointer.
  await expect(pane.locator('.tp-keys')).toBeHidden()

  // The documented way out: Shift+Tab hands the keyboard to the panel's own
  // controls, and the shell never sees the key.
  await page.keyboard.press('Shift+Tab')
  await expect(pane.getByRole('button', { name: 'Close' })).toBeFocused()

  // Closing detaches; it never ends. The shell keeps running on the engine.
  await pane.getByRole('button', { name: 'Close' }).click()
  await expect(pane).toBeHidden()
  // The slot goes back to the chat: no divider, and no tile beside it.
  await expect(page.locator('.chat-split-resizer')).toHaveCount(0)
  await expect(page.locator('.chat-split')).toHaveCount(0)
  await expect(page.locator('.chat-split-side')).toBeHidden()
  const sessions = await page.evaluate(async () => {
    const res = await fetch('/__fixture__/terminal')
    return (await res.json()).sessions.length
  })
  expect(sessions).toBe(1)

  // Opening it again finds the same shell, with what it printed still on
  // the screen.
  await open.click()
  await expect(pane).toBeVisible()
  await expect(pane.getByRole('tab')).toHaveCount(1)
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

test('the terminal is the pinned file tile, with the same divider', async ({ page }) => {
  // 1280×900 with the sidebar open leaves the pane 940px: room for a chat
  // column and the tile, so the tile is the shape the terminal takes.
  await page.setViewportSize({ width: 1280, height: 900 })
  await boot(page, CHAT_PATH, COMPOSER)

  // At this width the chat details are a drawer, so the terminal's way in
  // is one click deeper.
  await page.locator('.work-inspector-trigger').click()
  const details = page.locator('#chat-work-rail')
  await expect(details).toBeVisible()
  await details.locator('.chat-rail-terminal').getByRole('button', { name: 'Open terminal' }).click()

  const pane = page.locator('.terminal-pane')
  await expect(pane).toBeVisible()
  // The terminal is in the slot: the same window the pinned file uses.
  await expect(pane).toHaveClass(/terminal-pane--tile/)

  // And the tile is that window: the same inset, radius and border the
  // file panel's tile has.
  const tile = page.locator('.chat-split-side')
  const chrome = await readTileChrome(tile)
  expect(chrome.margin).toBe('16px 16px 16px 0px')
  // The radius is the shared --radius-lg token.
  expect(chrome.radius).toBe('14px')
  expect(chrome.border).toBe('1px')
  // The tile carries the shadow — the window's own.
  expect(chrome.shadow).not.toBe('none')
  // And the tone is the tile's own --bg2, not a frame of the terminal's.
  expect(chrome.background).toBe(await readTokenBackground(page, '--bg2'))
  // The terminal inside it draws no shadow of its own: the window it sits
  // in already has one, and a second would be a frame of its own.
  expect(await pane.evaluate(el => getComputedStyle(el).boxShadow)).toBe('none')

  // The tile slides in (chat-tile-in); boxes read on different frames of it
  // disagree by up to the slide's 12px, so measure once it has settled.
  await tile.evaluate(el => Promise.all(el.getAnimations({ subtree: true }).map(a => a.finished)))
  // Close sits top right of the header, beside the title, as the pinned
  // file tile's does — not on a row of its own under it.
  const headBox = (await pane.locator('.tp-head').boundingBox())!
  const titleBox = (await pane.locator('.tp-title').boundingBox())!
  const closeBox = (await pane.locator('.tp-close').boundingBox())!
  expect(closeBox.x).toBeGreaterThan(titleBox.x + titleBox.width)
  expect(headBox.x + headBox.width - (closeBox.x + closeBox.width)).toBeLessThanOrEqual(16)
  expect(closeBox.y).toBeLessThan(titleBox.y + titleBox.height)
  await pane.locator('.tp-head').screenshot({ path: test.info().outputPath('terminal-head.png') })

  // The divider between the chat and the tile, and it is a separator.
  const divider = page.locator('.chat-split-resizer')
  await expect(divider).toBeVisible()
  expect(await divider.getAttribute('role')).toBe('separator')
  expect(await divider.getAttribute('aria-orientation')).toBe('vertical')

  // The tile stands to the right of the chat, and it is a window of its own.
  const column = page.locator('.chat-main > .chat-body')
  const columnBox = (await column.boundingBox())!
  const paneBox = (await pane.boundingBox())!
  const mainBox = (await page.locator('.chat-main').boundingBox())!
  expect(paneBox.x).toBeGreaterThanOrEqual(columnBox.x + columnBox.width - 1)
  expect(paneBox.height).toBeLessThan(mainBox.height)
  // The chat keeps a column worth reading.
  expect(columnBox.width).toBeGreaterThanOrEqual(360)

  // The divider drags: the chat gives up the room and the tile takes it.
  const dividerBox = (await divider.boundingBox())!
  await dragDivider(page, divider, dividerBox.x - 100)

  const columnAfter = (await column.boundingBox())!
  const paneAfter = (await pane.boundingBox())!
  expect(columnAfter.width).toBeLessThan(columnBox.width - 50)
  expect(paneAfter.width).toBeGreaterThan(paneBox.width + 50)
  // And the chat never goes below the floor the drag holds it to.
  expect(columnAfter.width).toBeGreaterThanOrEqual(360)

  // Dragged as far left as it goes, the chat column stops at its 360px
  // floor: the divider cannot squeeze the conversation to a sliver.
  await dragDivider(page, divider, 120)
  const columnLeft = (await column.boundingBox())!
  expect(columnLeft.width).toBeGreaterThanOrEqual(360)
  expect(columnLeft.width).toBeLessThanOrEqual(362)
  // And the drag stopped there: the remembered ratio is that split.
  const leftRatio = await page.evaluate(() => Number(localStorage.getItem('ciao:chat-split-ratio')))
  expect(leftRatio * mainBox.width).toBeCloseTo(columnLeft.width, 0)

  // Dragged as far right as it goes, the tile stops at its 360px floor.
  await dragDivider(page, divider, 2000)
  const tileBox = (await tile.boundingBox())!
  expect(tileBox.width).toBeGreaterThanOrEqual(360)
  expect(tileBox.width).toBeLessThanOrEqual(362)
  // And it is inside the pane: the floor is a floor, not a clipping.
  expect(tileBox.x + tileBox.width).toBeLessThanOrEqual(mainBox.x + mainBox.width)
  // The drag stopped at that split and wrote it down: the remembered ratio
  // and the split on screen are the same one.
  const columnRight = (await column.boundingBox())!
  const ratio = await page.evaluate(() => Number(localStorage.getItem('ciao:chat-split-ratio')))
  expect(ratio * mainBox.width).toBeCloseTo(columnRight.width, 0)

  // The terminal's tile is the pinned file's tile: the same window, with the
  // same chrome, so a frame of the terminal's own cannot slip in.
  await pane.getByRole('button', { name: 'Close' }).click()
  await expect(pane).toBeHidden()
  await boot(page, '/chat/beta-chat-1', COMPOSER)
  const fileTile = page.locator('.chat-split-side')
  await expect(fileTile).toBeVisible()
  const fileChrome = await readTileChrome(fileTile)
  expect(fileChrome.radius).toBe(chrome.radius)
  expect(fileChrome.margin).toBe(chrome.margin)
  expect(fileChrome.border).toBe(chrome.border)
  // The shadow as a value: a shadow of the terminal's own would be a
  // different one, and a yes/no would call it the same.
  expect(fileChrome.shadow).toBe(chrome.shadow)
  expect(fileChrome.background).toBe(chrome.background)
})

test('a pane too narrow for both columns gets the drawer, not a squeezed chat', async ({ page }) => {
  // 900×800 with the sidebar open leaves the pane 560px: too narrow for a
  // chat column and the tile's 360px floor side by side.
  await page.setViewportSize({ width: 900, height: 800 })
  await boot(page, CHAT_PATH, COMPOSER)
  await page.locator('.work-inspector-trigger').click()
  const details = page.locator('#chat-work-rail')
  await expect(details).toBeVisible()
  await details.locator('.chat-rail-terminal').getByRole('button', { name: 'Open terminal' }).click()

  const pane = page.locator('.terminal-pane')
  await expect(pane).toBeVisible()
  // The drawer, not a tile in a pane that cannot hold it.
  await expect(pane).toHaveClass(/terminal-pane--drawer/)

  const main = page.locator('.chat-main')
  const column = page.locator('.chat-main > .chat-body')
  const mainBox = (await main.boundingBox())!
  const paneBox = (await pane.boundingBox())!
  const columnBox = (await column.boundingBox())!

  // The drawer covers exactly the pane it is in — its containing block — and
  // not the window: a drawer the size of the viewport would reach over the
  // sidebar too.
  expect(Math.abs(paneBox.x - mainBox.x)).toBeLessThanOrEqual(1)
  expect(Math.abs(paneBox.y - mainBox.y)).toBeLessThanOrEqual(1)
  expect(Math.abs(paneBox.width - mainBox.width)).toBeLessThanOrEqual(1)
  expect(Math.abs(paneBox.height - mainBox.height)).toBeLessThanOrEqual(1)
  // The chat keeps the width it had: the drawer is over the chat, not beside
  // it, so nothing is taken from the conversation.
  expect(columnBox.width).toBeGreaterThanOrEqual(mainBox.width - 1)

  // And hiding the terminal gives the chat back exactly what it had.
  await pane.getByRole('button', { name: 'Close' }).click()
  await expect(pane).toBeHidden()
  const after = (await column.boundingBox())!
  expect(Math.abs(after.width - columnBox.width)).toBeLessThanOrEqual(1)
})

test('a pinned file keeps the slot and the terminal falls back to the drawer', async ({ page }) => {
  // This chat's own file is pinned by the server, so the slot is taken
  // before the terminal is ever asked for.
  await page.setViewportSize({ width: 1280, height: 900 })
  await boot(page, '/chat/beta-chat-1', COMPOSER)

  const tile = page.locator('.chat-split-side')
  await expect(tile).toBeVisible()
  await expect(tile.locator('.pinned-file-panel')).toBeVisible()

  await page.locator('.work-inspector-trigger').click()
  const details = page.locator('#chat-work-rail')
  await expect(details).toBeVisible()
  await details.locator('.chat-rail-terminal').getByRole('button', { name: 'Open terminal' }).click()

  // The terminal cannot have the slot — the file has it — so it is the
  // drawer over the pane, and the file keeps the tile.
  const pane = page.locator('.terminal-pane')
  await expect(pane).toBeVisible()
  await expect(pane).toHaveClass(/terminal-pane--drawer/)
  await expect(tile.locator('.terminal-pane')).toHaveCount(0)
  await expect(tile.locator('.pinned-file-panel')).toBeVisible()
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
  const open = section.getByRole('button', { name: 'Open terminal' })
  await expect(open).toBeVisible()
  await open.click()

  const pane = page.locator('.terminal-pane')
  await expect(pane).toBeVisible()
  await expect(pane.locator('.tp-warning')).toContainText('not a sandbox')
  // The pane is scoped to this chat, not to another one.
  await expect.poll(() => scopeQueries.join(' ')).toContain('chat_id=alpha-chat-2')

  // A shell starts here like any other: opening the panel starts nothing.
  await expect(pane.getByRole('tab')).toHaveCount(0)
  await pane.getByRole('button', { name: 'New terminal session' }).click()
  await expect(pane.getByRole('tab')).toHaveCount(1)
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
    await details.locator('.chat-rail-terminal').getByRole('button', { name: 'Open terminal' }).click()

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

    // A shell is started, and with it the keys a soft keyboard cannot
    // produce appear under the screen. Enter is tapped rather than typed, so
    // the byte provably came from the row.
    await pane.getByRole('button', { name: 'New terminal session' }).click()
    const keys = pane.getByRole('toolbar', { name: 'Terminal keys' })
    await expect(keys).toBeVisible()
    const field = pane.locator('.tp-surface textarea')
    await expect(field).toBeEditable()
    await expect(keys.getByRole('button', { name: 'Enter' })).toBeEnabled()
    await pane.locator('.tp-surface').click()
    await page.keyboard.type(`echo ${OUTPUT}-keys`)
    await keys.getByRole('button', { name: 'Enter' }).click()
    await expect(pane.locator('.tp-surface')).toContainText(`${OUTPUT}-keys`)
    // The tap left the keyboard on the shell.
    await expect(field).toBeFocused()
    // A hung command is interruptible from the row: Ctrl+C ends `sleep` and
    // the prompt answers again.
    await page.keyboard.type('sleep 30')
    await keys.getByRole('button', { name: 'Enter' }).click()
    await keys.getByRole('button', { name: 'Control C' }).click()
    await page.keyboard.type(`echo ${OUTPUT}-after`)
    await keys.getByRole('button', { name: 'Enter' }).click()
    await expect(pane.locator('.tp-surface')).toContainText(`${OUTPUT}-after`)

    // Every control in it is a 44px target, the key row included.
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
