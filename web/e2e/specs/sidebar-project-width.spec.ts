import { expect, test } from '@playwright/test'
import { boot, isolate } from '../support/app'

const LONG_PROJECT = 'alpha-notes-a-very-long-unbreakable-project-identifier'
const READY = '.project-header'

test.describe('sidebar project header width', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `sidebar-project-width-${testInfo.workerIndex}`)
  })

  test('a fine-pointer row gives its idle name the space reserved only for hidden actions', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await boot(page, '/', READY)

    const row = page.locator('.project-header').filter({ hasText: LONG_PROJECT })
    const name = row.locator('.project-name')
    const measure = () => row.evaluate((header) => {
      const text = header.querySelector<HTMLElement>('.project-name-text')!
      const controls = [...header.querySelectorAll<HTMLElement>('.project-actions-btn, .add-chat-btn')]
      return {
        header: header.getBoundingClientRect().width,
        name: header.querySelector<HTMLElement>('.project-name')!.getBoundingClientRect().width,
        textContent: text.textContent,
        textScroll: text.scrollWidth,
        hiddenActionWidth: controls.reduce((total, control) => total + control.getBoundingClientRect().width, 0),
      }
    })

    for (const [viewport, scale] of [[1440, 1], [1024, 1], [1024, 1.5]] as const) {
      await page.setViewportSize({ width: viewport, height: 900 })
      await page.evaluate((fontScale) => document.documentElement.style.setProperty('--font-scale', String(fontScale)), scale)
      const width = await measure()
      expect(width.header).toBeGreaterThan(300)
      expect(width.hiddenActionWidth).toBeLessThan(8)
      expect(width.name).toBeGreaterThan(250)
      expect(width.textScroll).toBeGreaterThan(width.name)
    }

    await page.screenshot({ path: test.info().outputPath('sidebar-project-width.png'), fullPage: true })
    await row.hover()
    for (const control of await row.locator('.project-actions-btn, .add-chat-btn').all()) {
      const box = await control.boundingBox()
      expect(box?.width).toBeGreaterThanOrEqual(44)
      expect(box?.height).toBeGreaterThanOrEqual(44)
    }
    expect(await name.isVisible()).toBe(true)
  })

  test('project menu and new-chat controls remain keyboard reachable at a narrow width', async ({ page }) => {
    await page.setViewportSize({ width: 1024, height: 768 })
    await boot(page, '/', READY)

    const row = page.locator('.project-header').filter({ hasText: LONG_PROJECT })
    const menu = row.getByRole('button', { name: 'Project actions' })
    const newChat = row.getByRole('button', { name: `New chat in ${LONG_PROJECT}` })

    await row.locator('.project-icon').focus()
    await page.keyboard.press('Tab') // project name
    await page.keyboard.press('Tab') // project actions
    await expect(menu).toBeFocused()
    expect((await menu.boundingBox())?.width).toBeGreaterThanOrEqual(44)
    await page.keyboard.press('Tab') // new chat
    await expect(newChat).toBeFocused()
    expect((await newChat.boundingBox())?.width).toBeGreaterThanOrEqual(44)

    await menu.focus()
    await page.keyboard.press('Enter')
    await expect(page.getByRole('menuitem', { name: 'Rename' })).toBeVisible()
    await page.keyboard.press('Escape')
  })

  test('coarse-pointer project controls retain 44px targets', async ({ browser }) => {
    const context = await browser.newContext({
      viewport: { width: 1440, height: 900 },
      isMobile: true,
      hasTouch: true,
    })
    const page = await context.newPage()
    await isolate(page, `sidebar-project-width-touch-${test.info().workerIndex}`)
    await boot(page, '/', READY)

    const coarse = await page.evaluate(() => matchMedia('(pointer: coarse)').matches)
    expect(coarse).toBe(true)

    const row = page.locator('.project-header').filter({ hasText: LONG_PROJECT })
    const targets = await row.locator('.project-actions-btn, .add-chat-btn').evaluateAll((buttons) =>
      buttons.map((button) => {
        const { width, height } = button.getBoundingClientRect()
        return { width, height }
      }),
    )
    expect(targets).toHaveLength(2)
    for (const target of targets) {
      expect(target.width).toBeGreaterThanOrEqual(44)
      expect(target.height).toBeGreaterThanOrEqual(44)
    }
    await context.close()
  })
})
