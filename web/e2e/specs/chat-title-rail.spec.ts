import { expect, test } from '@playwright/test'
import { COMPOSER, boot, isolate } from '../support/app'

/**
 * The chat title rail needs a real layout: jsdom has no scroll snapping, no
 * touch scrolling and no box geometry. The fixture gives workspace alpha two
 * active chats with the same timestamps, both read, so the rail order is
 * [alpha-chat-1, alpha-chat-2].
 */

test.describe('chat title rail, desktop', () => {
  test.use({ viewport: { width: 1280, height: 800 } })

  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `title-rail-desktop-${testInfo.workerIndex}`)
  })

  test('centres the title with the neighbours off-screen, puts the state track on the header\'s bottom edge, and slides to the next chat with the arrow key', async ({ page }) => {
    await boot(page, '/chat/alpha-chat-1', COMPOSER)

    const current = page.locator('.title-rail-item[aria-current="page"]')
    await expect(current).toContainText('alpha first conversation')
    await expect(page.getByRole('button', { name: 'Next chat' })).toHaveCount(0)
    await expect(page.locator('.chat-code-badge')).toHaveCount(0)

    const header = page.locator('.chat-panel .pane-header')
    expect(await header.evaluate(el => el.getBoundingClientRect().height)).toBe(61)

    const box = (selector: string) => page.locator(selector).first().evaluate(el => {
      const b = el.getBoundingClientRect()
      return { left: b.left, right: b.right, top: b.top, bottom: b.bottom, centreX: b.left + b.width / 2, centreY: b.top + b.height / 2 }
    })
    const close = await box('.chat-title-row .close-btn')
    const title = await box('.title-rail-item[aria-current="page"] .chat-title')
    const block = await box('.title-rail-item[aria-current="page"]')
    const strip = await box('.title-rail-strip')
    const track = await box('.title-rail-track')
    const head = await box('.chat-panel .pane-header')
    // Centred on the chat's reading column (not the header, which also spans the
    // actions and Chat details), with close on the block's vertical centre.
    const column = await box('.messages-content')
    expect(Math.abs(title.centreX - column.centreX)).toBeLessThanOrEqual(2)
    expect(Math.abs(title.centreX - strip.centreX)).toBeLessThanOrEqual(2)
    expect(Math.abs(close.centreY - block.centreY)).toBeLessThanOrEqual(2)
    // The neighbour is off-screen until a swipe or step brings it in.
    const next = await box('.title-rail-item:not([aria-current]) .title-rail-title')
    expect(next.left).toBeGreaterThanOrEqual(strip.right - 1)
    // The track sits on the header's bottom edge, centred under the title, below the project line.
    expect(Math.abs(track.bottom - head.bottom)).toBeLessThanOrEqual(2)
    expect(Math.abs(track.centreX - column.centreX)).toBeLessThanOrEqual(2)
    const project = await box('.title-rail-item[aria-current="page"] .title-rail-project')
    expect(track.top).toBeGreaterThan(project.bottom)
    await expect(page.locator('.title-rail-track .title-rail-seg')).toHaveCount(2)

    await current.focus()
    await page.keyboard.press('ArrowRight')
    await expect(page).toHaveURL(/\/chat\/alpha-chat-2$/)
    await expect(page.locator('.title-rail-item[aria-current="page"]')).toContainText('alpha second conversation')
  })

  test('with 13 chats the track keeps one segment per chat inside the rail and the header stays 61px', async ({ page }) => {
    // Eleven more chats in alpha, copied from alpha-chat-2 so they share its
    // project. The fixture itself stays as it is for the other specs.
    await page.route(/\/api\/chats(\?.*)?$/, async route => {
      const response = await route.fetch()
      const chats = await response.json() as Array<Record<string, unknown>>
      const base = chats.find(c => c.chat_id === 'alpha-chat-2')!
      const extra = Array.from({ length: 11 }, (_, i) => ({ ...base, chat_id: `alpha-extra-${i}`, title: `alpha extra ${i}` }))
      await route.fulfill({ response, json: [...chats, ...extra] })
    })
    await boot(page, '/chat/alpha-chat-1', COMPOSER)

    await expect(page.locator('.title-rail-track .title-rail-seg')).toHaveCount(13)
    expect(await page.locator('.chat-panel .pane-header').evaluate(el => el.getBoundingClientRect().height)).toBe(61)
    const [strip, track] = await page.locator('.title-rail-strip, .title-rail-track').evaluateAll(
      els => els.map(el => { const b = el.getBoundingClientRect(); return { left: b.left, right: b.right } }),
    )
    expect(track.left).toBeGreaterThanOrEqual(strip.left)
    expect(track.right).toBeLessThanOrEqual(strip.right)
  })

  test('a chat still being named shows a title-sized shimmer in the rail, and the header stays 61px', async ({ page }) => {
    // The rail's items are flex columns: the shared shimmer's `flex-basis: 10rem`
    // once became a 10rem-tall block there.
    const naming = (c: Record<string, unknown>) => c.chat_id === 'alpha-chat-1' ? { ...c, title: 'New Chat', title_status: 'pending' } : c
    // The list and the open chat's own record both carry the title.
    await page.route(/\/api\/chats(\/alpha-chat-1)?(\?.*)?$/, async route => {
      const response = await route.fetch()
      const body = await response.json() as Record<string, unknown> | Array<Record<string, unknown>>
      await route.fulfill({ response, json: Array.isArray(body) ? body.map(naming) : naming(body) })
    })
    await boot(page, '/chat/alpha-chat-1', COMPOSER)

    const shimmer = page.locator('.title-rail-item[aria-current="page"] .chat-title-shimmer')
    await expect(shimmer).toBeVisible()
    const box = await shimmer.evaluate(el => { const b = el.getBoundingClientRect(); return { w: b.width, h: b.height } })
    expect(box.h).toBeLessThanOrEqual(14)
    expect(box.w).toBeGreaterThan(40)
    expect(await page.locator('.chat-panel .pane-header').evaluate(el => el.getBoundingClientRect().height)).toBe(61)
  })

  test('pulling the title down closes the chat to Home; a short pull springs back; a mouse drag sideways switches', async ({ page }) => {
    await boot(page, '/chat/alpha-chat-1', COMPOSER)
    const title = page.locator('.title-rail-item[aria-current="page"] .title-rail-title')
    const grab = async () => { const b = (await title.boundingBox())!; return { x: b.x + b.width / 2, y: b.y + 5 } }

    // A short pull: the Home hint shows, then the chat springs back.
    let at = await grab()
    await page.mouse.move(at.x, at.y)
    await page.mouse.down()
    await page.mouse.move(at.x, at.y + 60, { steps: 6 })
    await expect(page.locator('.chat-pull-home')).toBeVisible()
    await page.waitForTimeout(400)
    await page.mouse.up()
    await expect(page.locator('.chat-pull-home')).toHaveCount(0)
    await expect(page).toHaveURL(/\/chat\/alpha-chat-1$/)
    // Let the spring-back finish before the next grab: mid-transition the title is
    // still moving under the pointer.
    await expect.poll(() => page.locator('.chat-panel').evaluate(el => (el as HTMLElement).style.transform)).toBe('')

    // A mouse drag sideways switches to the next chat.
    at = await grab()
    await page.mouse.move(at.x, at.y)
    await page.mouse.down()
    await page.mouse.move(at.x - 260, at.y + 2, { steps: 10 })
    await page.mouse.up()
    await expect(page).toHaveURL(/\/chat\/alpha-chat-2$/)

    // A long pull closes the chat and lands on Home.
    at = await grab()
    await page.mouse.move(at.x, at.y)
    await page.mouse.down()
    await page.mouse.move(at.x, at.y + 180, { steps: 8 })
    await expect(page.locator('.chat-pull-home')).toContainText('Release for Home')
    await page.mouse.up()
    await expect(page).toHaveURL(/\/$/)
  })

  test('a mouse swipe carries straight on to the next chat, never flashing back to the old title first', async ({ page }) => {
    await boot(page, '/chat/alpha-chat-1', COMPOSER)
    // A slow engine stretches the gap between the release and the switch.
    await page.route(/\/api\//, async route => { await new Promise(r => setTimeout(r, 300)); await route.continue() })
    await page.evaluate(() => {
      const w = window as unknown as { __railPath: number[] }
      w.__railPath = []
      const t0 = performance.now()
      const tick = () => {
        const strip = document.querySelector('.title-rail-strip') as HTMLElement | null
        if (strip && location.pathname.endsWith('alpha-chat-1')) w.__railPath.push(strip.scrollLeft)
        if (performance.now() - t0 < 1500) requestAnimationFrame(tick)
      }
      requestAnimationFrame(tick)
    })
    const b = (await page.locator('.title-rail-item[aria-current="page"] .title-rail-title').boundingBox())!
    await page.mouse.move(b.x + b.width / 2, b.y + 5)
    await page.mouse.down()
    await page.mouse.move(b.x + b.width / 2 - 300, b.y + 6, { steps: 10 })
    await page.mouse.up()
    await expect(page).toHaveURL(/\/chat\/alpha-chat-2$/)
    const path = await page.evaluate(() => (window as unknown as { __railPath: number[] }).__railPath)
    // From the end of the drag on, the strip only moves forward until the switch.
    const from = path.findIndex(x => x >= 290)
    const after = path.slice(from)
    expect(from).toBeGreaterThanOrEqual(0)
    expect(after.every((x, i) => i === 0 || x >= after[i - 1] - 1), `strip path ${after.join(',')}`).toBe(true)
  })

  test('Option+arrows step the rail from the composer and preserve its draft', async ({ page }) => {
    await boot(page, '/chat/alpha-chat-1', COMPOSER)

    await page.locator(COMPOSER).fill('A draft to keep')
    await page.keyboard.press('Alt+ArrowRight')
    await expect(page).toHaveURL(/\/chat\/alpha-chat-2$/)
    await expect(page.locator(COMPOSER)).toBeFocused()
    await page.keyboard.press('Alt+ArrowLeft')
    await expect(page).toHaveURL(/\/chat\/alpha-chat-1$/)
    await expect(page.locator(COMPOSER)).toHaveValue('A draft to keep')
  })
})

test.describe('chat title rail, phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `title-rail-phone-${testInfo.workerIndex}`)
  })

  test('has no arrow buttons, snaps the strip, switches on a finished swipe, and never scrolls the document sideways', async ({ page }) => {
    await boot(page, '/chat/alpha-chat-1', COMPOSER)

    await expect(page.getByRole('button', { name: 'Next chat' })).toHaveCount(0)
    const snap = await page.locator('.title-rail-strip').evaluate(el => getComputedStyle(el).scrollSnapType)
    expect(snap).toContain('x mandatory')

    await page.locator('.title-rail-strip').evaluate(el => {
      el.dispatchEvent(new Event('touchstart'))
      el.dispatchEvent(new Event('touchend'))
      el.scrollLeft = el.scrollWidth
    })
    await expect(page).toHaveURL(/\/chat\/alpha-chat-2$/)

    const overflow = await page.evaluate(() => document.scrollingElement!.scrollWidth <= window.innerWidth)
    expect(overflow).toBe(true)
  })

  test('a native touch pan switches chats after browser pointer cancellation', async ({ page }) => {
    await boot(page, '/chat/alpha-chat-1', COMPOSER)
    const strip = page.locator('.title-rail-strip')
    const box = (await strip.boundingBox())!
    await page.locator('.title-rail').evaluate(el => {
      el.addEventListener('pointercancel', () => el.setAttribute('data-test-pointer-canceled', 'yes'))
    })
    const cdp = await page.context().newCDPSession(page)
    const y = box.y + box.height / 2
    const startX = box.x + box.width * 0.85
    const point = (x: number) => ({ x, y, id: 1 })
    try {
      await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [point(startX)] })
      for (let i = 1; i <= 8; i++) {
        await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [point(startX - box.width * 0.7 * i / 8)] })
      }
      await expect(page.locator('.title-rail')).toHaveAttribute('data-test-pointer-canceled', 'yes')
      await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] })
      await expect(page).toHaveURL(/\/chat\/alpha-chat-2$/)
    } finally { await cdp.detach() }
  })
})
