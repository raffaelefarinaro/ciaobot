import { expect, test, type APIRequestContext, type Page } from '@playwright/test'
import { boot, isolate } from '../support/app'

/**
 * An open task on the board's route is a page, like an open chat or
 * automation: the shared header with close and a title rail over the board's
 * tasks in board order within its filter, swipe / arrow keys / Option+Arrow
 * between them, pull to close back to /tasks, and a sidebar listing the lanes
 * with SidebarRows and New on the To do label.
 */

const NOW = new Date().toISOString()
const BASE = {
  project_id: '', due: '', assignee: 'user', chat_id: '', attempt_id: '', linked_chats: [],
  created_at: '2026-01-01T09:00:00+00:00', updated_at: NOW, revision: 'a'.repeat(64),
  attempt_state: '', attempt_outcome: '', attempt_summary: '', attempt_detail: '', live_attempt_id: '',
}
const id = (c: string) => c.repeat(32)
const TASKS = [
  { ...BASE, id: id('1'), title: 'Plan the offsite', status: 'backlog', relative_path: 'Workspace/Tasks/a.md' },
  { ...BASE, id: id('2'), title: 'Book the venue', status: 'backlog', relative_path: 'Workspace/Tasks/b.md' },
  { ...BASE, id: id('3'), title: 'Draft the agenda', status: 'in_progress', relative_path: 'Workspace/Tasks/c.md' },
]
const CURRENT = '.task-detail-page .title-rail-item[aria-current="page"]'

async function seed(page: Page, request: APIRequestContext, session: string) {
  await isolate(page, session)
  await request.post('/__fixture__/inbox', { headers: { cookie: `e2e_session=${session}` }, data: { tasks: { alpha: TASKS } } })
}

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

test.describe('the task page, desktop', () => {
  test.use({ viewport: { width: 1280, height: 800 } })

  test('opens from a card as a page with the shared header, and Escape returns focus to the card', async ({ page, request }, ti) => {
    await seed(page, request, `task-page-open-${ti.workerIndex}`)
    await boot(page, '/tasks', '.task-card')
    const opener = page.locator('.task-card', { hasText: 'Book the venue' }).locator('.task-open')
    await opener.focus()
    await page.keyboard.press('Enter')
    await expect(page).toHaveURL(new RegExp(`/tasks/${id('2')}$`))
    await expect(page.locator('#task-detail-name')).toBeFocused()
    // A page, not a dialog: the board steps aside and the sidebar stays usable.
    await expect(page.locator('.task-lanes')).toBeHidden()
    await expect(page.locator('[role="dialog"]')).toHaveCount(0)
    expect(await page.locator('.sidebar').evaluate(el => (el as HTMLElement).inert)).toBe(false)
    await expect(page.locator('.task-detail-page .close-btn svg')).toBeVisible()
    await expect(page.locator('.task-detail-page .title-rail')).toHaveAttribute('aria-label', 'Tasks on this board, 2 of 3')

    await page.keyboard.press('Escape')
    await expect(page).toHaveURL(/\/tasks$/)
    await expect(page.locator('.task-lanes')).toBeVisible()
    await expect(opener).toBeFocused()
  })

  test('arrow keys on the title and Option+arrows step through the tasks in board order, with no wrap-around', async ({ page, request }, ti) => {
    await seed(page, request, `task-page-keys-${ti.workerIndex}`)
    await boot(page, `/tasks/${id('1')}`, CURRENT)
    await page.locator(CURRENT).focus()
    await page.keyboard.press('ArrowRight')
    await expect(page).toHaveURL(new RegExp(`/tasks/${id('2')}$`))
    await expect(page.locator('#task-detail-name')).toHaveValue('Book the venue')

    await page.locator('.task-detail-page .pane-header').click({ position: { x: 5, y: 5 } })
    await page.keyboard.press('Alt+ArrowRight')
    await expect(page).toHaveURL(new RegExp(`/tasks/${id('3')}$`))
    await page.keyboard.press('Alt+ArrowRight')
    await page.waitForTimeout(300)
    await expect(page).toHaveURL(new RegExp(`/tasks/${id('3')}$`))
    await page.keyboard.press('Alt+ArrowLeft')
    await expect(page).toHaveURL(new RegExp(`/tasks/${id('2')}$`))
  })

  test('Option+Left at the first task is consumed, never the browser\'s Back', async ({ page, request }, ti) => {
    await seed(page, request, `task-page-back-${ti.workerIndex}`)
    // Home, then Tasks from the sidebar: a history entry a fallen-through
    // Alt+Left would go Back to.
    await boot(page, '/')
    await page.locator('.nav-links a[href="/tasks"]').click()
    const opener = page.locator('.task-card', { hasText: 'Plan the offsite' }).locator('.task-open')
    await opener.focus()
    await page.keyboard.press('Enter')
    await expect(page).toHaveURL(new RegExp(`/tasks/${id('1')}$`))
    await page.locator('.task-detail-page .pane-header').click({ position: { x: 5, y: 5 } })
    await page.keyboard.press('Alt+ArrowLeft')
    await page.waitForTimeout(400)
    await expect(page).toHaveURL(new RegExp(`/tasks/${id('1')}$`))
    await expect(page.locator('.task-detail-page')).toBeVisible()
  })

  test('Option+arrows do nothing from the title field or under a confirm; a blank title keeps the task', async ({ page, request }, ti) => {
    await seed(page, request, `task-page-gates-${ti.workerIndex}`)
    await boot(page, `/tasks/${id('1')}`, CURRENT)
    const stays = async () => {
      await page.waitForTimeout(400)
      await expect(page).toHaveURL(new RegExp(`/tasks/${id('1')}$`))
    }
    // From the title field: the keys are the field's.
    await page.locator('#task-detail-name').focus()
    await page.keyboard.press('Alt+ArrowRight')
    await stays()

    // Under the delete confirm: stepping away under it would delete the wrong task.
    await page.locator('.task-detail-page .task-delete').click()
    const confirm = page.getByRole('dialog')
    await expect(confirm).toBeVisible()
    await page.keyboard.press('Alt+ArrowRight')
    await stays()
    await confirm.getByRole('button', { name: 'Cancel' }).click()
    await expect(confirm).toHaveCount(0)

    // A blank title is not written, and a switch would lose it: the rail keeps
    // the task, says why, and the draft stays.
    await page.locator('#task-detail-name').fill('')
    await page.locator('.task-detail-page .pane-header').click({ position: { x: 5, y: 5 } })
    await page.keyboard.press('Alt+ArrowRight')
    await stays()
    await expect(page.locator('.task-detail-page')).toContainText('Add a title before closing')
    await expect(page.locator('#task-detail-name')).toHaveValue('')
  })

  test('the sidebar lists the lanes as rows, opens a task, and offers New on To do', async ({ page, request }, ti) => {
    await seed(page, request, `task-page-sidebar-${ti.workerIndex}`)
    await boot(page, '/tasks', '.task-card')
    const list = page.locator('.task-sidebar-list')
    await expect(list.locator('.schedule-group-header')).toHaveText(['To do', 'In progress'])
    const rows = list.locator('.sidebar-row')
    await expect(rows).toHaveCount(3)
    expect(await rows.first().evaluate(el => getComputedStyle(el).borderRadius)).toBe('7px')
    await rows.nth(2).click()
    await expect(page).toHaveURL(new RegExp(`/tasks/${id('3')}$`))
    await expect(rows.nth(2)).toHaveClass(/active/)
    await expect(page.locator('#task-detail-name')).toHaveValue('Draft the agenda')

    await list.getByRole('button', { name: 'New task' }).click()
    await expect(page.locator('.task-create-sheet, [role="dialog"]').first()).toBeVisible()
  })
})

test.describe('the task page, phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

  test('a swipe moves between tasks; a pull down on the title closes to the board; the header is one row', async ({ page, request }, ti) => {
    await seed(page, request, `task-page-phone-${ti.workerIndex}`)
    await boot(page, `/tasks/${id('1')}`, CURRENT)
    const header = page.locator('.task-detail-page .pane-header')
    await expect(header.locator('.close-btn')).toBeHidden()
    const [title, trail] = await Promise.all([
      page.locator(`${CURRENT} .title-rail-title`).boundingBox(),
      header.locator('.header-trail').boundingBox(),
    ])
    expect(Math.abs((title!.y + title!.height / 2) - (trail!.y + trail!.height / 2))).toBeLessThan(trail!.height)
    // Delete and the save line fold into the ⋯ menu, so the title keeps the row.
    await expect(header.locator('.task-delete')).toBeHidden()
    await header.getByRole('button', { name: 'More task actions' }).click()
    await expect(page.getByRole('menuitem', { name: 'Delete task…' })).toBeVisible()
    await expect(page.locator('.item-header-menu-note')).toHaveText('Changes save as you go.')
    await page.keyboard.press('Escape')
    await expect(page.getByRole('menuitem', { name: 'Delete task…' })).toHaveCount(0)
    await expect(page.locator('.task-detail-page')).toBeVisible()

    const strip = (await page.locator('.task-detail-page .title-rail-strip').boundingBox())!
    await touchDrag(page, { x: strip.x + strip.width * 0.95, y: strip.y + strip.height / 2 }, { x: -strip.width * 0.9, y: 0 })
    await expect(page).toHaveURL(new RegExp(`/tasks/${id('2')}$`))
    expect(await page.evaluate(() => document.scrollingElement!.scrollWidth <= window.innerWidth)).toBe(true)

    await expect.poll(() => page.locator('.task-view').evaluate(el => (el as HTMLElement).style.transform)).toBe('')
    const box = (await page.locator(`${CURRENT} .title-rail-title`).boundingBox())!
    await touchDrag(page, { x: box.x + box.width / 2, y: box.y + 5 }, { x: 0, y: 200 }, async () => {
      await expect(page.locator('.chat-pull-home')).toContainText('Release for Tasks')
    })
    await expect(page).toHaveURL(/\/tasks$/)
    await expect(page.locator('.task-detail-page')).toHaveCount(0)
  })
})
