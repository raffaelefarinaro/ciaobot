import { expect, test, type Page } from '@playwright/test'
import { boot, isolate, quiesce, releasePointerInSameTask } from '../support/app'

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
    const name = group.locator('.project-name')
    await name.hover()
    const box = (await name.boundingBox())!
    const edge = (await scroller(page))!
    await page.mouse.move(box.x + 4, box.y + box.height / 2)
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

  test('a fast release above a project lands before it when the drawn placeholder is stale', async ({ page }) => {
    const requests: Array<Record<string, unknown>> = []
    page.on('request', (request) => {
      if (request.method() === 'POST' && request.url().endsWith('/api/projects/reorder')) {
        requests.push(request.postDataJSON())
      }
    })
    const group = page.locator('[data-project-id="filler-02"]')
    const name = group.locator('.project-name')
    await name.hover()
    const from = (await name.boundingBox())!
    await page.mouse.move(from.x + 4, from.y + from.height / 2)
    await page.mouse.down()
    // Activate at the source slot, so the preview has drawn its placeholder there.
    await page.mouse.move(from.x + 4, from.y + from.height / 2 + 12, { steps: 3 })
    await expect(page.locator('.drag-overlay')).toBeVisible()
    await expect(page.locator('.project-list > .drag-placeholder')).toHaveCount(1)

    // Move above Notes and release in one task: the drawn placeholder is still
    // at the old slot while targetAt recomputes for the new one.
    const notes = (await page.locator(`[data-project-id="${NOTES}"]`).boundingBox())!
    await releasePointerInSameTask(page.locator('.project-list'), {
      x: notes.x + notes.width / 2,
      y: notes.y + 8,
    })
    await page.mouse.up()

    await expect.poll(() => requests.length).toBe(1)
    const expected = ['filler-02', NOTES, 'filler-01', ...FILLERS.slice(2)]
    expect(requests[0]!.order).toEqual(expected)
    await expect.poll(() => rowOrder(page)).toEqual(['alpha-general', ...expected])
  })

  test('a project reorders from its header name, with no handle on the row', async ({ page }) => {
    const requests: Array<Record<string, unknown>> = []
    page.on('request', (request) => {
      if (request.method() === 'POST' && request.url().endsWith('/api/projects/reorder')) {
        requests.push(request.postDataJSON())
      }
    })
    // There is no drag handle at all: pointer drags the header, and the row menu
    // (with 44px items on touch) is the keyboard and touch path.
    await expect(page.locator('.project-list .drag-grip')).toHaveCount(0)

    // Drag a one-row project up by its name, above Notes: above the target is
    // stable whether or not the placeholder has flushed, so the assertion is
    // about the surface gesture, not about the fast-release seam (covered above).
    const name = page.locator('[data-project-id="filler-02"] .project-name')
    await name.hover()
    const from = (await name.boundingBox())!
    await page.mouse.move(from.x + 4, from.y + from.height / 2)
    await page.mouse.down()
    await page.mouse.move(from.x + 4, from.y + from.height / 2 - 12, { steps: 3 })
    // Measure after the pick-up has collapsed the source row, in the layout the
    // drag is actually in.
    const above = (await page.locator(`[data-project-id="${NOTES}"] .project-header`).boundingBox())!
    await page.mouse.move(above.x + above.width / 2, above.y + 4, { steps: 12 })
    await expect.poll(() => page.evaluate((notes) => {
      const children = [...document.querySelector('.project-list')!.children] as HTMLElement[]
      const placeholder = children.findIndex((el) => el.classList.contains('drag-placeholder'))
      const target = children.findIndex((el) => el.dataset.projectId === notes)
      return placeholder >= 0 && placeholder === target - 1
    }, NOTES)).toBe(true)
    await page.mouse.up()

    await expect.poll(() => requests.length).toBe(1)
    const expected = ['filler-02', NOTES, 'filler-01', ...FILLERS.slice(2)]
    expect(requests[0]!.order).toEqual(expected)
    await expect.poll(() => rowOrder(page)).toEqual(['alpha-general', ...expected])
    // The press was a drag, not the name's click.
    expect(new URL(page.url()).pathname).toBe('/')
  })

  test('a plain press on a project name still opens the project page', async ({ page }) => {
    let posted = 0
    page.on('request', (request) => {
      if (request.method() === 'POST' && request.url().endsWith('/api/projects/reorder')) posted += 1
    })
    await page.locator(`[data-project-id="${NOTES}"] .project-name`).click()
    await expect(page).toHaveURL(new RegExp(`/project/${NOTES}$`))
    expect(posted).toBe(0)
  })

  test('an expanded group moves as one and Escape cancels with no request', async ({ page }) => {
    let posted = 0
    page.on('request', (request) => {
      if (request.method() === 'POST' && request.url().endsWith('/api/projects/reorder')) posted += 1
    })
    const group = page.locator(`[data-project-id="${NOTES}"]`)
    await expect(group.locator('.chat-item')).toHaveCount(1)
    const groupHeight = (await group.boundingBox())!.height
    const name = group.locator('.project-name')
    await name.hover()
    const box = (await name.boundingBox())!
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
    await expect.poll(() => rowOrder(page).then((rows) => rows.slice(0, 2))).toEqual(['alpha-general', NOTES])
  })

  test('a cover over the list blocks a drop through it', async ({ page }) => {
    let posted = 0
    page.on('request', (request) => {
      if (request.method() === 'POST' && request.url().endsWith('/api/projects/reorder')) posted += 1
    })
    const group = page.locator(`[data-project-id="${NOTES}"]`)
    const name = group.locator('.project-name')
    await name.hover()
    const box = (await name.boundingBox())!
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
    await page.mouse.down()
    await page.mouse.move(box.x + 40, box.y + 120, { steps: 8 })
    await expect(page.locator('.drag-overlay')).toBeVisible()

    // Something opens over the sidebar while the row is picked up.
    await page.evaluate(() => {
      const cover = document.createElement('div')
      cover.className = 'e2e-cover'
      Object.assign(cover.style, {
        position: 'fixed', top: '0', left: '0', width: '320px', height: '100vh',
        zIndex: '2000', background: 'rgb(0 0 0 / 20%)',
      })
      document.body.append(cover)
    })
    const target = page.locator('[data-project-id="filler-01"] .project-header')
    const at = (await target.boundingBox())!
    await page.mouse.move(at.x + 20, at.y + 5, { steps: 8 })
    await page.mouse.up()

    await expect(page.locator('.drag-overlay')).toHaveCount(0)
    expect(posted).toBe(0)
    await expect.poll(() => rowOrder(page).then((rows) => rows.slice(0, 2))).toEqual(['alpha-general', NOTES])
    await page.evaluate(() => document.querySelector('.e2e-cover')?.remove())
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

  test('the project menu reorders with the keyboard and returns focus to its trigger', async ({ page }) => {
    const trigger = page.locator('[data-project-id="filler-02"] .project-actions-btn')
    await trigger.focus()
    await page.keyboard.press('Enter')
    const moveUp = page.getByRole('menuitem', { name: 'Move up' })
    await expect(moveUp).toBeVisible()
    // Rename is first; walk to Move up when it is not the first item focused.
    for (let step = 0; step < 4; step += 1) {
      if (await moveUp.evaluate((el) => el === document.activeElement)) break
      await page.keyboard.press('ArrowDown')
    }
    await expect(moveUp).toBeFocused()
    await page.keyboard.press('Enter')
    await expect.poll(() => rowOrder(page)).toEqual([
      'alpha-general', NOTES, 'filler-02', 'filler-01', ...FILLERS.slice(2),
    ])
    await expect(trigger).toBeFocused()
  })
})

test.describe('sidebar project reordering on a phone', () => {
  test('the project menu reorders by touch, with 44px targets', async ({ browser }, testInfo) => {
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

    // The drawer slides in: measure once it has stopped.
    await quiesce(page)
    // Pinned and reorderable rows now share one leading chevron: no handle slot.
    const chevronLeft = (id: string) =>
      page.locator(`[data-project-id="${id}"] .project-icon`).evaluate((el) => el.getBoundingClientRect().left)
    expect(Math.abs((await chevronLeft('alpha-general')) - (await chevronLeft(NOTES)))).toBeLessThanOrEqual(1)

    const trigger = page.locator(`[data-project-id="${NOTES}"] .project-actions-btn`)
    const hit = (await trigger.boundingBox())!
    expect(hit.width).toBeGreaterThanOrEqual(44)
    expect(hit.height).toBeGreaterThanOrEqual(44)

    const requested = page.waitForRequest(
      (request) => request.method() === 'POST' && request.url().endsWith('/api/projects/reorder'),
    )
    await trigger.tap()
    const moveDown = page.getByRole('menuitem', { name: 'Move down' })
    await expect(moveDown).toBeVisible()
    const item = (await moveDown.boundingBox())!
    expect(item.height).toBeGreaterThanOrEqual(44)
    await moveDown.tap()

    const request = await requested
    expect((request.postDataJSON().order as string[]).indexOf(NOTES)).toBeGreaterThan(0)
    await context.close()
  })
})
