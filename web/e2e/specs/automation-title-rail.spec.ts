import { expect, test, type Page } from '@playwright/test'
import { boot, isolate } from '../support/app'

/**
 * An open automation has the same header as an open chat: a title rail over the
 * workspace's automations in sidebar order, swiped (or arrow-keyed) between, and
 * pulled down to close back to the automation list. The fixture gives alpha
 * three routines, so the rail order is Daily project briefing, Weekly review,
 * Inbox sweep.
 */

const CURRENT = '.schedule-panel .title-rail-item[aria-current="page"]'

async function touchDrag(page: Page, from: { x: number; y: number }, d: { x: number; y: number }, hold?: () => Promise<void>) {
  const cdp = await page.context().newCDPSession(page)
  const point = (i: number) => ({ x: from.x + d.x * i / 10, y: from.y + d.y * i / 10, id: 1 })
  try {
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [point(0)] })
    for (let i = 1; i <= 10; i++) await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [point(i)] })
    await hold?.()
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] })
  } finally { await cdp.detach() }
}

test.describe('automation title rail, phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `automation-rail-phone-${testInfo.workerIndex}`)
  })

  test('a touch swipe moves between automations; a pull down on the title closes to the list', async ({ page }) => {
    await boot(page, '/schedules/alpha-daily-brief', CURRENT)
    await expect(page.locator(CURRENT)).toContainText('Daily project briefing')
    await expect(page.locator('.schedule-panel .title-rail')).toHaveAttribute('aria-label', 'Automations in this workspace, 1 of 3')
    await expect(page.locator('.schedule-panel .title-rail-track .title-rail-seg')).toHaveCount(3)
    // Never a native scroller: iOS would blur the title under the status bar.
    expect(await page.locator('.schedule-panel .title-rail-strip').evaluate(el => getComputedStyle(el).overflowX)).toBe('hidden')

    const strip = (await page.locator('.schedule-panel .title-rail-strip').boundingBox())!
    const y = strip.y + strip.height / 2
    await touchDrag(page, { x: strip.x + strip.width * 0.95, y }, { x: -strip.width * 0.9, y: 0 })
    await expect(page).toHaveURL(/\/schedules\/alpha-weekly-review$/)
    await expect(page.locator(CURRENT)).toContainText('Weekly review')
    await expect(page.locator('.schedule-panel .title-rail')).toHaveAttribute('aria-label', 'Automations in this workspace, 2 of 3')
    // The swipe never scrolls the document sideways.
    expect(await page.evaluate(() => document.scrollingElement!.scrollWidth <= window.innerWidth)).toBe(true)

    // A short pull shows the hint and springs back.
    const title = (await page.locator(`${CURRENT} .title-rail-title`).boundingBox())!
    const at = { x: title.x + title.width / 2, y: title.y + 5 }
    await touchDrag(page, at, { x: 0, y: 60 }, async () => {
      await expect(page.locator('.chat-pull-home')).toContainText('Automations')
    })
    await expect(page.locator('.chat-pull-home')).toHaveCount(0)
    await expect(page).toHaveURL(/\/schedules\/alpha-weekly-review$/)
    await expect.poll(() => page.locator('.schedule-panel').evaluate(el => (el as HTMLElement).style.transform)).toBe('')

    // A long pull drops the automation and lands on the list.
    await touchDrag(page, at, { x: 0, y: 200 }, async () => {
      await expect(page.locator('.chat-pull-home')).toContainText('Release for Automations')
    })
    await expect(page).toHaveURL(/\/schedules$/)
    await expect(page.locator('.schedule-panel .title-rail')).toHaveCount(0)
  })
})

test.describe('automation header, phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

  test('is one row like a chat\'s, with Run now and Pause in the ⋯ menu', async ({ page }, testInfo) => {
    await isolate(page, `automation-header-phone-${testInfo.workerIndex}`)
    await boot(page, '/schedules/alpha-daily-brief', CURRENT)
    const header = page.locator('.schedule-panel .pane-header')
    const [title, trail] = await Promise.all([
      page.locator(`${CURRENT} .title-rail-title`).boundingBox(),
      page.locator('.schedule-panel .header-trail').boundingBox(),
    ])
    // Same row: the title's centre sits within the actions' band.
    expect(Math.abs((title!.y + title!.height / 2) - (trail!.y + trail!.height / 2))).toBeLessThan(trail!.height)
    await expect(header.getByRole('button', { name: 'Run now' })).toBeHidden()
    await header.getByRole('button', { name: 'More automation actions' }).click()
    await expect(page.getByRole('menuitem', { name: 'Run now' })).toBeVisible()
    await expect(page.getByRole('menuitem', { name: 'Pause' })).toBeVisible()
  })

  test('folds Back and the bell into the ⋯ menu, as an open chat does', async ({ page }, testInfo) => {
    await isolate(page, `automation-header-fold-${testInfo.workerIndex}`)
    await boot(page, '/schedules/alpha-daily-brief', CURRENT)
    const header = page.locator('.schedule-panel .pane-header')
    await expect(page.locator('.schedule-panel .header-back')).toBeHidden()
    await expect(page.locator('.schedule-panel .header-trail .notif-bell')).toBeHidden()
    await header.getByRole('button', { name: /^More automation actions/ }).click()
    await expect(page.getByRole('menuitem', { name: 'Notifications' })).toBeVisible()
    const menuItems = page.getByRole('menuitem')
    const count = await menuItems.count()
    expect(count).toBeGreaterThan(0)
    for (let i = 0; i < count; i++) {
      const box = await menuItems.nth(i).boundingBox()
      expect(box!.height).toBeGreaterThanOrEqual(44)
    }
  })
})

test.describe('automation title rail, desktop', () => {
  test.use({ viewport: { width: 1280, height: 800 } })

  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `automation-rail-desktop-${testInfo.workerIndex}`)
  })

  test('arrow keys on the title and Option+arrows step along the rail; sidebar rows are the chat rows', async ({ page }) => {
    await boot(page, '/schedules/alpha-daily-brief', CURRENT)
    // The sidebar lists automations with the chat row component, same box as a chat row.
    const rows = page.locator('.schedules-list .sidebar-row')
    await expect(rows).toHaveCount(3)
    await expect(rows.first()).toHaveClass(/active/)
    expect(await rows.first().evaluate(el => getComputedStyle(el).borderRadius)).toBe('7px')

    await page.locator(CURRENT).focus()
    await page.keyboard.press('ArrowRight')
    await expect(page).toHaveURL(/\/schedules\/alpha-weekly-review$/)
    await expect(page.locator(CURRENT)).toContainText('Weekly review')

    await page.locator('body').click({ position: { x: 900, y: 600 } })
    await page.keyboard.press('Alt+ArrowRight')
    await expect(page).toHaveURL(/\/schedules\/alpha-inbox-sweep$/)
    // No wrap-around at the end.
    await page.keyboard.press('Alt+ArrowRight')
    await expect(page).toHaveURL(/\/schedules\/alpha-inbox-sweep$/)
    await page.keyboard.press('Alt+ArrowLeft')
    await expect(page).toHaveURL(/\/schedules\/alpha-weekly-review$/)
  })

  test('Option+Left at the first automation is consumed, never the browser\'s Back', async ({ page }) => {
    await boot(page, '/schedules', '.schedules-list .schedule-item')
    await page.locator('.schedules-list .schedule-item').first().click()
    await expect(page).toHaveURL(/\/schedules\/alpha-daily-brief$/)
    await page.locator('body').click({ position: { x: 900, y: 600 } })
    await page.keyboard.press('Alt+ArrowLeft')
    await page.waitForTimeout(400)
    await expect(page).toHaveURL(/\/schedules\/alpha-daily-brief$/)
  })

  test('an arrow at the end of the rail keeps focus on the title', async ({ page }) => {
    await boot(page, '/schedules/alpha-inbox-sweep', CURRENT)
    await page.locator(CURRENT).focus()
    await page.keyboard.press('ArrowRight')
    await expect(page).toHaveURL(/\/schedules\/alpha-inbox-sweep$/)
    await expect(page.locator(CURRENT)).toBeFocused()
  })

  test('Option+arrows do nothing with the actions menu or a confirm open, or while a card is being edited', async ({ page }) => {
    await boot(page, '/schedules/alpha-weekly-review', CURRENT)
    const stays = async () => {
      await page.waitForTimeout(400)
      await expect(page).toHaveURL(/\/schedules\/alpha-weekly-review$/)
    }

    // The ⋯ menu.
    await page.getByRole('button', { name: 'More automation actions' }).click()
    await expect(page.getByRole('menuitem', { name: 'Delete automation…' })).toBeVisible()
    await page.keyboard.press('Alt+ArrowRight')
    await stays()

    // The delete confirm: stepping away under it would delete the wrong automation.
    await page.getByRole('menuitem', { name: 'Delete automation…' }).click()
    const confirm = page.getByRole('dialog', { name: 'Delete automation' })
    await expect(confirm).toBeVisible()
    await page.keyboard.press('Alt+ArrowRight')
    await stays()
    await confirm.getByRole('button', { name: 'Cancel' }).click()
    await expect(confirm).toHaveCount(0)

    // A card's textarea: the draft survives and the automation stays.
    await page.locator('.prompt-actions').getByRole('button', { name: 'Edit' }).click()
    const prompt = page.locator('.content-form textarea').last()
    await prompt.fill('A draft to keep')
    await page.keyboard.press('Alt+ArrowRight')
    await stays()
    await expect(prompt).toHaveValue('A draft to keep')
    // Focus outside the field, the card being edited still holds the rail.
    await page.locator('.content-form').click({ position: { x: 2, y: 2 } })
    await page.keyboard.press('Alt+ArrowRight')
    await stays()
    await expect(prompt).toHaveValue('A draft to keep')
  })
})
