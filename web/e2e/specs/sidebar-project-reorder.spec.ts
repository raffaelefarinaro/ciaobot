import { expect, test, type Page } from '@playwright/test'
import { boot, isolate, quiesce } from '../support/app'

const READY = '.project-header'
const NOTES = 'alpha-notes'
const FILLERS = Array.from({ length: 30 }, (_, i) => `filler-${String(i + 1).padStart(2, '0')}`)

// General (pinned), the fixture's notes project (it holds a chat), then enough
// filler projects that the sidebar has to scroll.
const PROJECTS = [
  { project_id: 'alpha-general', name: 'General', workspace: 'alpha', context: '', order: 0, is_auto: true },
  { project_id: NOTES, name: 'Notes', workspace: 'alpha', context: '', order: 1 },
  ...FILLERS.map((id, i) => ({ project_id: id, name: id, workspace: 'alpha', context: '', order: i + 2 })),
]

async function seed(page: Page): Promise<void> {
  const response = await page.request.post('/__fixture__/projects', { data: { projects: PROJECTS } })
  expect(response.ok()).toBe(true)
}

const rowOrder = (page: Page) =>
  page.locator('.project-list > [data-project-id]').evaluateAll((els) =>
    els.map((el) => (el as HTMLElement).dataset.projectId),
  )

/** The bottom edge of the sidebar's scrolling ancestor of the project list. */
async function scroller(page: Page) {
  return page.locator('.project-list').evaluate((list) => {
    for (let el = list.parentElement; el; el = el.parentElement) {
      const overflow = getComputedStyle(el).overflowY
      if ((overflow === 'auto' || overflow === 'scroll') && el.scrollHeight > el.clientHeight) {
        const rect = el.getBoundingClientRect()
        return { bottom: rect.bottom, left: rect.left, width: rect.width }
      }
    }
    return null
  })
}

test.describe('sidebar project reordering', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `sidebar-project-reorder-${testInfo.workerIndex}`)
    await seed(page)
    await page.setViewportSize({ width: 1440, height: 700 })
    await boot(page, '/', READY)
  })

  test('a long list auto-scrolls during a drag, commits once, and a reload keeps the order', async ({ page }) => {
    const requests: Array<Record<string, unknown>> = []
    page.on('request', (request) => {
      if (request.method() === 'POST' && request.url().endsWith('/api/projects/reorder')) {
        requests.push(request.postDataJSON())
      }
    })
    const group = page.locator(`[data-project-id="${NOTES}"]`)
    await group.locator('.project-header').hover()
    const grip = group.locator('.drag-grip')
    const box = (await grip.boundingBox())!
    const edge = (await scroller(page))!
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
    await page.mouse.down()
    await page.mouse.move(edge.left + 80, edge.bottom - 12, { steps: 12 })
    await expect.poll(() =>
      page.locator('.project-list').evaluate((list) => {
        for (let el = list.parentElement; el; el = el.parentElement) {
          if (el.scrollTop > 0) return el.scrollTop
        }
        return 0
      }),
    ).toBeGreaterThan(0)
    await expect(page.locator('.drag-overlay')).toBeVisible()
    await page.mouse.up()
    await expect.poll(() => requests.length).toBe(1)
    await page.screenshot({ path: test.info().outputPath('sidebar-project-reorder.png') })

    expect(requests[0].expected_order).toEqual([NOTES, ...FILLERS])
    const order = requests[0].order as string[]
    expect(order).not.toEqual([NOTES, ...FILLERS])
    expect(order.indexOf(NOTES)).toBeGreaterThan(0)
    expect(await rowOrder(page)).toEqual(['alpha-general', ...order])

    await page.reload()
    await expect(page.locator(READY).first()).toBeVisible()
    expect(await rowOrder(page)).toEqual(['alpha-general', ...order])
  })

  test('an expanded group moves as one and Escape cancels with no request', async ({ page }) => {
    let posted = 0
    page.on('request', (request) => {
      if (request.method() === 'POST' && request.url().endsWith('/api/projects/reorder')) posted += 1
    })
    const group = page.locator(`[data-project-id="${NOTES}"]`)
    await expect(group.locator('.chat-item')).toHaveCount(1)
    const groupHeight = (await group.boundingBox())!.height
    await group.locator('.project-header').hover()
    const box = (await group.locator('.drag-grip').boundingBox())!
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
    await page.mouse.down()
    await page.mouse.move(box.x + 40, box.y + 120, { steps: 8 })

    const overlay = page.locator('.drag-overlay')
    await expect(overlay.locator('.chat-item')).toHaveCount(1)
    const placeholder = page.locator('.drag-placeholder')
    await expect(placeholder).toHaveCount(1)
    expect((await placeholder.boundingBox())!.height).toBeCloseTo(groupHeight, 0)
    expect(await rowOrder(page).then((rows) => rows[0])).toBe('alpha-general')

    await page.keyboard.press('Escape')
    await expect(overlay).toHaveCount(0)
    await page.mouse.up()
    expect(posted).toBe(0)
    expect((await rowOrder(page)).slice(0, 2)).toEqual(['alpha-general', NOTES])
  })

  test('a chat can still be dropped onto another project header', async ({ page }) => {
    let patched: Record<string, unknown> | null = null
    await page.route('**/api/chats/alpha-chat-2', async (route) => {
      if (route.request().method() !== 'PATCH') return route.fallback()
      patched = route.request().postDataJSON()
      const chats = await (await page.request.get('/api/chats')).json()
      const chat = chats.find((c: { chat_id: string }) => c.chat_id === 'alpha-chat-2')
      return route.fulfill({ json: { ...chat, project_id: 'filler-01' } })
    })
    const chat = page.locator(`[data-project-id="${NOTES}"] .chat-item`)
    const target = page.locator('[data-project-id="filler-01"] .project-header')
    await chat.dragTo(target)
    await expect.poll(() => patched).toEqual({ project_id: 'filler-01' })
  })

  test('the grip moves a project with the keyboard and keeps focus', async ({ page }) => {
    const grip = page.locator(`[data-project-id="${NOTES}"] .drag-grip`)
    await grip.focus()
    await page.keyboard.press('Space')
    await page.keyboard.press('ArrowDown')
    expect((await rowOrder(page)).slice(0, 3)).toEqual(['alpha-general', 'filler-01', NOTES])
    await page.keyboard.press('Escape')
    expect((await rowOrder(page)).slice(0, 2)).toEqual(['alpha-general', NOTES])
    await expect(grip).toBeFocused()
  })
})

test.describe('sidebar project reordering on a phone', () => {
  test('a touch drag on the 44px grip reorders without scrolling the sidebar', async ({ browser }, testInfo) => {
    const context = await browser.newContext({
      viewport: { width: 390, height: 844 },
      isMobile: true,
      hasTouch: true,
    })
    const page = await context.newPage()
    await isolate(page, `sidebar-project-reorder-touch-${testInfo.workerIndex}`)
    await seed(page)
    await boot(page, '/chat/alpha-chat-1', 'textarea.chat-input')
    await page.getByRole('button', { name: 'Open sidebar' }).click()

    const group = page.locator(`[data-project-id="${NOTES}"]`)
    const grip = group.locator('.drag-grip')
    await expect(grip).toBeVisible()
    // The drawer slides in: measure once it has stopped.
    await quiesce(page)
    const box = (await grip.boundingBox())!
    const cx = box.x + box.width / 2
    const cy = box.y + box.height / 2
    // The grip's hit area is the 44px square, not the 20x24 button: a point
    // outside the button's box but inside the square still reaches it.
    const hit = await page.evaluate(
      ([x, y]) => document.elementFromPoint(x, y)?.closest('[data-drag-handle]')?.getAttribute('data-drag-handle'),
      [cx - 16, cy + 16],
    )
    expect(hit).toBe(NOTES)

    // Pinned General has no grip; its chevron still lines up with the rows that do.
    const chevronLeft = (id: string) =>
      page.locator(`[data-project-id="${id}"] .project-icon`).evaluate((el) => el.getBoundingClientRect().left)
    expect(Math.abs((await chevronLeft('alpha-general')) - (await chevronLeft(NOTES)))).toBeLessThanOrEqual(1)

    const requested = page.waitForRequest(
      (request) => request.method() === 'POST' && request.url().endsWith('/api/projects/reorder'),
    )
    const cdp = await context.newCDPSession(page)
    const touch = (type: 'touchStart' | 'touchMove' | 'touchEnd', x: number, y: number) =>
      cdp.send('Input.dispatchTouchEvent', {
        type,
        touchPoints: type === 'touchEnd' ? [] : [{ x, y, id: 1 }],
      })
    await touch('touchStart', cx, cy)
    for (let i = 1; i <= 10; i += 1) await touch('touchMove', cx, cy + i * 12)
    await expect(page.locator('.drag-overlay')).toBeVisible()
    await touch('touchEnd', cx, cy + 120)
    const request = await requested
    expect((request.postDataJSON().order as string[]).indexOf(NOTES)).toBeGreaterThan(0)
    await context.close()
  })
})
