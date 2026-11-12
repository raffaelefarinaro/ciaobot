import { expect, test, type Page } from '@playwright/test'
import { boot, isolate } from '../support/app'

/**
 * A project page has the same header as an open chat and an open automation: a
 * title rail over the workspace's projects in sidebar order, swiped (or
 * arrow-keyed) between, and pulled down to close back to Home. The fixture
 * gives alpha two projects in the sidebar (General, then the notes project;
 * the Memory project is hidden), so the rail has two items.
 */

const CURRENT = '.project-view .title-rail-item[aria-current="page"]'
const NOTES = 'alpha-notes'

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

test.describe('project title rail, phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `project-rail-phone-${testInfo.workerIndex}-${testInfo.title.length}`)
  })

  test('a swipe moves between projects; a pull down on the title closes to Home', async ({ page }) => {
    await boot(page, '/project/alpha-general', CURRENT)
    await expect(page.locator(CURRENT)).toContainText('General')
    await expect(page.locator('.project-view .title-rail')).toHaveAttribute('aria-label', 'Projects in this workspace, 1 of 2')
    expect(await page.locator('.project-view .title-rail-strip').evaluate(el => getComputedStyle(el).overflowX)).toBe('hidden')

    const strip = (await page.locator('.project-view .title-rail-strip').boundingBox())!
    const y = strip.y + strip.height / 2
    await touchDrag(page, { x: strip.x + strip.width * 0.95, y }, { x: -strip.width * 0.9, y: 0 })
    await expect(page).toHaveURL(new RegExp(`/project/${NOTES}$`))
    await expect(page.locator('.project-view .title-rail')).toHaveAttribute('aria-label', 'Projects in this workspace, 2 of 2')
    expect(await page.evaluate(() => document.scrollingElement!.scrollWidth <= window.innerWidth)).toBe(true)

    // A short pull shows the hint and springs back.
    const title = (await page.locator(`${CURRENT} .title-rail-title`).boundingBox())!
    const at = { x: title.x + title.width / 2, y: title.y + 5 }
    await touchDrag(page, at, { x: 0, y: 60 }, async () => {
      await expect(page.locator('.chat-pull-home')).toContainText('Home')
    })
    await expect(page.locator('.chat-pull-home')).toHaveCount(0)
    await expect(page).toHaveURL(new RegExp(`/project/${NOTES}$`))
    await expect.poll(() => page.locator('.project-view').evaluate(el => (el as HTMLElement).style.transform)).toBe('')

    // A long pull drops the project and lands Home. The title is measured again
    // once the spring-back has settled.
    const again = (await page.locator(`${CURRENT} .title-rail-title`).boundingBox())!
    await touchDrag(page, { x: again.x + again.width / 2, y: again.y + 5 }, { x: 0, y: 200 }, async () => {
      await expect(page.locator('.chat-pull-home')).toContainText('Release for Home')
    })
    await expect(page).toHaveURL(/127\.0\.0\.1:\d+\/$/)
    await expect(page.locator('.project-view')).toHaveCount(0)
  })

  test('a pull with an unsaved context draft asks first; refused, the page and the draft stay', async ({ page }) => {
    await boot(page, '/project/alpha-general', CURRENT)
    await page.locator('.project-context').getByRole('button', { name: 'Edit' }).click()
    const field = page.locator('.project-context textarea')
    await field.fill('A draft to keep')
    const box = (await page.locator(`${CURRENT} .title-rail-title`).boundingBox())!
    await touchDrag(page, { x: box.x + box.width / 2, y: box.y + 5 }, { x: 0, y: 200 })
    const confirm = page.getByRole('dialog', { name: 'Unsaved changes' })
    await expect(confirm).toBeVisible()
    await confirm.getByRole('button', { name: 'Cancel' }).click()
    await expect(confirm).toHaveCount(0)
    await expect(page).toHaveURL(/\/project\/alpha-general$/)
    await expect(field).toHaveValue('A draft to keep')
    await expect.poll(() => page.locator('.project-view').evaluate(el => (el as HTMLElement).style.transform)).toBe('')
  })

  test('the header is one row, with close hidden and New chat in the ⋯ menu', async ({ page }) => {
    await boot(page, '/project/alpha-general', CURRENT)
    const header = page.locator('.project-view .pane-header')
    const [title, trail] = await Promise.all([
      page.locator(`${CURRENT} .title-rail-title`).boundingBox(),
      header.locator('.header-trail').boundingBox(),
    ])
    expect(Math.abs((title!.y + title!.height / 2) - (trail!.y + trail!.height / 2))).toBeLessThan(trail!.height)
    await expect(header.locator('.close-btn')).toBeHidden()
    await expect(header.getByRole('button', { name: 'New chat' })).toHaveCount(0)
    await header.getByRole('button', { name: 'Project actions' }).click()
    await expect(page.getByRole('menuitem', { name: 'New chat' })).toBeVisible()
  })
})

test.describe('project title rail, desktop', () => {
  test.use({ viewport: { width: 1280, height: 800 } })

  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `project-rail-desktop-${testInfo.workerIndex}-${testInfo.title.length}`)
  })

  test('arrow keys on the title and Option+arrows step along the rail, with no wrap-around', async ({ page }) => {
    await boot(page, '/project/alpha-general', CURRENT)
    await expect(page.locator('.project-view .close-btn svg')).toBeVisible()
    await page.locator(CURRENT).focus()
    await page.keyboard.press('ArrowRight')
    await expect(page).toHaveURL(new RegExp(`/project/${NOTES}$`))
    // An arrow at the end keeps focus on the title and stays.
    await page.locator(CURRENT).focus()
    await page.keyboard.press('ArrowRight')
    await expect(page).toHaveURL(new RegExp(`/project/${NOTES}$`))
    await expect(page.locator(CURRENT)).toBeFocused()

    await page.locator('body').click({ position: { x: 900, y: 700 } })
    await page.keyboard.press('Alt+ArrowLeft')
    await expect(page).toHaveURL(/\/project\/alpha-general$/)
    await page.keyboard.press('Alt+ArrowLeft')
    await expect(page).toHaveURL(/\/project\/alpha-general$/)
    await page.keyboard.press('Alt+ArrowRight')
    await expect(page).toHaveURL(new RegExp(`/project/${NOTES}$`))
  })

  test('Option+arrows do nothing with the actions menu or a confirm open, or while the context is edited', async ({ page }) => {
    await boot(page, '/project/alpha-general', CURRENT)
    const stays = async () => {
      await page.waitForTimeout(400)
      await expect(page).toHaveURL(/\/project\/alpha-general$/)
    }

    // The ⋯ menu (the sidebar's project rows carry menus of the same name).
    await page.locator('.project-view .pane-header').getByRole('button', { name: 'Project actions' }).click()
    await expect(page.getByRole('menuitem', { name: 'Delete project' })).toBeVisible()
    await page.keyboard.press('Alt+ArrowRight')
    await stays()

    // The delete confirm: stepping away under it would delete the wrong project.
    await page.getByRole('menuitem', { name: 'Delete project' }).click()
    const confirm = page.getByRole('dialog')
    await expect(confirm).toBeVisible()
    await page.keyboard.press('Alt+ArrowRight')
    await stays()
    await confirm.getByRole('button', { name: 'Cancel' }).click()
    await expect(confirm).toHaveCount(0)

    // The context editor: the draft survives, from the field and from outside it.
    await page.locator('.project-context').getByRole('button', { name: 'Edit' }).click()
    const field = page.locator('.project-context textarea')
    await field.fill('A draft to keep')
    await page.keyboard.press('Alt+ArrowRight')
    await stays()
    await page.locator('.project-context h3').click()
    await page.keyboard.press('Alt+ArrowRight')
    await stays()
    await expect(field).toHaveValue('A draft to keep')
  })
})
