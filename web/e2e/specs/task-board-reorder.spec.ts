import { devices, expect, test, type Locator, type Page } from '@playwright/test'
import { boot, isolate, releasePointerInSameTask } from '../support/app'

/**
 * Placing cards on the board: the grip's pointer drag within and across lanes,
 * the order surviving a reload, a narrow scrolled board, the keyboard flow and a
 * touch drag. Geometry, hit testing and pointer capture only exist in a real
 * browser, so the gestures are driven here; the writes are held by a stateful
 * stand-in for the engine's `/api/tasks` routes.
 */
const NOW = new Date()
const BASE = {
  project_id: '', due: '', assignee: 'user', chat_id: '', attempt_id: '',
  created_at: '2026-01-01T09:00:00+00:00', updated_at: NOW.toISOString(),
  revision: 'a'.repeat(64), attempt_state: '', attempt_outcome: '', attempt_summary: '',
  attempt_detail: '', live_attempt_id: '',
}
type Status = 'backlog' | 'in_progress' | 'in_review' | 'done'

const makeTask = (n: number, title: string, status: Status) => ({
  ...BASE, id: String(n).repeat(32), title, status, relative_path: `Workspace/Tasks/${n}.md`,
})
const TASKS = [
  makeTask(1, 'Plan the offsite', 'backlog'),
  makeTask(2, 'Book the venue', 'backlog'),
  makeTask(3, 'Send the invites', 'backlog'),
  makeTask(4, 'Draft the agenda', 'in_progress'),
  makeTask(5, 'Review the budget', 'in_review'),
]

interface Sent { path: string; body: Record<string, unknown> }

/**
 * The engine's task routes, kept in memory so a reload reads what a write left:
 * a list with the order, `POST /order`, and `PATCH` with a placement.
 *
 * `holdOrder` lets a test keep an order write in flight while it moves focus,
 * then release it. It is per call, so no gate survives into another test.
 */
async function serve(page: Page, holdOrder?: Promise<void>): Promise<Sent[]> {
  const rows = TASKS.map(t => ({ ...t }))
  const sequences: Record<Status, string[]> = {
    backlog: rows.filter(t => t.status === 'backlog').map(t => t.id),
    in_progress: rows.filter(t => t.status === 'in_progress').map(t => t.id),
    in_review: rows.filter(t => t.status === 'in_review').map(t => t.id),
    done: [],
  }
  let version = 1
  const sent: Sent[] = []
  const order = () => ({ version, sequences })
  const place = (id: string, status: Status, body: Record<string, unknown>) => {
    for (const key of Object.keys(sequences) as Status[]) sequences[key] = sequences[key].filter(x => x !== id)
    const into = sequences[status]
    const anchor = (body.before_id ?? body.after_id) as string | null | undefined
    const at = anchor ? into.indexOf(anchor) + (body.before_id ? 0 : 1) : into.length
    into.splice(at < 0 ? into.length : at, 0, id)
    version += 1
  }
  await page.route(/\/api\/tasks(?:\?|$)/, route => route.fulfill({
    json: { workspace: 'alpha', tasks: rows, order: order() },
  }))
  await page.route(/\/api\/tasks\/(\d{32})\/order$/, async route => {
    await holdOrder
    const body = route.request().postDataJSON() as Record<string, unknown>
    const id = /tasks\/(\d{32})/.exec(route.request().url())![1]!
    sent.push({ path: `/api/tasks/${id}/order`, body })
    const row = rows.find(t => t.id === id)!
    place(id, row.status as Status, body)
    await route.fulfill({ json: { workspace: 'alpha', task: row, order: order() } })
  })
  await page.route(/\/api\/tasks\/(\d{32})$/, async route => {
    if (route.request().method() !== 'PATCH') return route.fallback()
    const body = route.request().postDataJSON() as Record<string, unknown>
    const id = /tasks\/(\d{32})/.exec(route.request().url())![1]!
    sent.push({ path: `/api/tasks/${id}`, body })
    const row = rows.find(t => t.id === id)!
    row.status = body.status as Status
    row.revision = 'b'.repeat(64)
    place(id, row.status as Status, (body.placement ?? {}) as Record<string, unknown>)
    await route.fulfill({ json: { workspace: 'alpha', task: row, order: order() } })
  })
  return sent
}

async function open(page: Page, id: string, holdOrder?: Promise<void>): Promise<Sent[]> {
  await isolate(page, id)
  const sent = await serve(page, holdOrder)
  await boot(page, '/tasks', '.task-card')
  return sent
}

const lane = (page: Page, name: string) => page.locator('.task-lanes').getByRole('region', { name, exact: true })
const titles = (page: Page, name: string) => lane(page, name).locator('.task-card .task-open').allTextContents()

/** Hold the grip, move in steps so the engine activates, and release `dy` below the target's middle. */
async function dragGrip(page: Page, title: string, target: Locator, dy = 0) {
  const card = page.locator('.task-card', { hasText: title })
  await card.hover()
  const grip = (await card.locator('.drag-grip').boundingBox())!
  const from = { x: grip.x + grip.width / 2, y: grip.y + grip.height / 2 }
  await page.mouse.move(from.x, from.y)
  await page.mouse.down()
  await page.mouse.move(from.x, from.y + 12, { steps: 3 })
  const to = (await target.boundingBox())!
  await page.mouse.move(to.x + to.width / 2, to.y + to.height / 2 + dy, { steps: 12 })
  await page.mouse.up()
}

/**
 * Press the card's own text (not the grip), move in steps so the engine
 * activates, and release at `point`.
 */
async function dragSurfaceTo(page: Page, title: string, point: { x: number; y: number }) {
  const text = page.locator('.task-card', { hasText: title }).locator('.task-title')
  const box = (await text.boundingBox())!
  // Near the line's left edge, which is plain text for these fixtures; a link
  // would own its own press.
  const from = { x: box.x + 4, y: box.y + box.height / 2 }
  await page.mouse.move(from.x, from.y)
  await page.mouse.down()
  await page.mouse.move(from.x, from.y + 12, { steps: 3 })
  await page.mouse.move(point.x, point.y, { steps: 12 })
  await page.mouse.up()
}

/** The middle of a card, useful as the target of a drag. */
async function cardCentre(target: Locator) {
  const box = (await target.boundingBox())!
  return { x: box.x + box.width / 2, y: box.y + box.height / 2 }
}

test.describe('placing cards with the mouse', () => {
  test.use({ viewport: { width: 1440, height: 900 } })

  test('reorders within a lane, persists once and keeps the order across a reload', async ({ page }) => {
    const sent = await open(page, 'task-reorder-within')
    expect(await titles(page, 'To do')).toEqual(['Plan the offsite', 'Book the venue', 'Send the invites'])
    // Drop the last card on the first one's upper half: it lands first.
    await dragGrip(page, 'Send the invites', page.locator('.task-card', { hasText: 'Plan the offsite' }), -15)
    await expect.poll(() => titles(page, 'To do')).toEqual(['Send the invites', 'Plan the offsite', 'Book the venue'])
    expect(sent).toHaveLength(1)
    expect(sent[0]!.path).toBe(`/api/tasks/${'3'.repeat(32)}/order`)
    expect(sent[0]!.body).toMatchObject({ before_id: '1'.repeat(32), expected_order_version: 1 })
    // No native drag ghost and no leftover overlay.
    await expect(page.locator('.drag-overlay')).toHaveCount(0)

    await page.reload()
    await expect(page.locator('.task-card').first()).toBeVisible()
    expect(await titles(page, 'To do')).toEqual(['Send the invites', 'Plan the offsite', 'Book the venue'])
  })

  test('moves a card across lanes in one PATCH with its placement', async ({ page }) => {
    const sent = await open(page, 'task-reorder-across')
    await dragGrip(page, 'Plan the offsite', page.locator('.task-card', { hasText: 'Draft the agenda' }), -15)
    await expect.poll(() => titles(page, 'In progress')).toEqual(['Plan the offsite', 'Draft the agenda'])
    expect(sent).toHaveLength(1)
    expect(sent[0]!.body).toMatchObject({
      status: 'in_progress',
      placement: { before_id: '4'.repeat(32), after_id: null, expected_order_version: 1 },
    })
    await expect.poll(() => titles(page, 'To do')).toEqual(['Book the venue', 'Send the invites'])
  })

  test('a fast release above a card lands before it even when its lane has not rendered', async ({ page }) => {
    const sent = await open(page, 'task-reorder-fast-seam')
    const source = page.locator('.task-card', { hasText: 'Plan the offsite' })
    const draft = page.locator('.task-card', { hasText: 'Draft the agenda' })
    await source.hover()
    const grip = (await source.locator('.drag-grip').boundingBox())!
    await page.mouse.move(grip.x + grip.width / 2, grip.y + grip.height / 2)
    await page.mouse.down()
    // Activate inside To do and stay there: In progress has drawn no placeholder.
    await page.mouse.move(grip.x + grip.width / 2, grip.y + grip.height / 2 + 12, { steps: 3 })
    await expect(page.locator('.drag-overlay')).toHaveCount(1)
    await expect(lane(page, 'In progress').locator('.drag-placeholder')).toHaveCount(0)

    // Destination move and release share one task. The release is clearly above
    // Draft's midpoint, so it must land before Draft with no preview to read.
    const target = (await draft.boundingBox())!
    await releasePointerInSameTask(page.locator('.task-lanes'), {
      x: target.x + target.width / 2,
      y: target.y + 8,
    })
    await page.mouse.up()

    await expect.poll(() => sent.length).toBe(1)
    expect(sent[0]!.body).toMatchObject({
      status: 'in_progress',
      placement: { before_id: '4'.repeat(32), after_id: null, expected_order_version: 1 },
    })
    await expect.poll(() => titles(page, 'In progress')).toEqual(['Plan the offsite', 'Draft the agenda'])
  })

  test('Escape cancels a drag without a request', async ({ page }) => {
    const sent = await open(page, 'task-reorder-escape')
    const card = page.locator('.task-card', { hasText: 'Plan the offsite' })
    await card.hover()
    const grip = (await card.locator('.drag-grip').boundingBox())!
    await page.mouse.move(grip.x + 8, grip.y + 8)
    await page.mouse.down()
    await page.mouse.move(grip.x + 8, grip.y + 60, { steps: 4 })
    await expect(page.locator('.drag-overlay')).toHaveCount(1)
    await page.keyboard.press('Escape')
    await page.mouse.up()
    await expect(page.locator('.drag-overlay')).toHaveCount(0)
    expect(sent).toHaveLength(0)
    expect(await titles(page, 'To do')).toEqual(['Plan the offsite', 'Book the venue', 'Send the invites'])
  })

  test('drags from the card text, and a plain press on the card still opens the editor', async ({ page }) => {
    const sent = await open(page, 'task-reorder-surface')
    const before = await cardCentre(page.locator('.task-card', { hasText: 'Plan the offsite' }))
    await dragSurfaceTo(page, 'Send the invites', { x: before.x, y: before.y - 15 })
    await expect.poll(() => titles(page, 'To do')).toEqual(['Send the invites', 'Plan the offsite', 'Book the venue'])
    expect(sent).toHaveLength(1)
    expect(sent[0]!.path).toBe(`/api/tasks/${'3'.repeat(32)}/order`)
    await expect(page.locator('.drag-overlay')).toHaveCount(0)

    // A press that does not travel is the card's own click, not a drag.
    await page.locator('.task-card', { hasText: 'Book the venue' }).locator('.task-title').click()
    await expect(page.locator('#task-detail-name')).toBeVisible()
    expect(sent).toHaveLength(1)
  })

  test('a single move into another lane picks the card up there', async ({ page }) => {
    const sent = await open(page, 'task-reorder-single-move-lane')
    const text = page.locator('.task-card', { hasText: 'Plan the offsite' }).locator('.task-title')
    const box = (await text.boundingBox())!
    const target = await cardCentre(page.locator('.task-card', { hasText: 'Review the budget' }))
    await page.mouse.move(box.x + 4, box.y + box.height / 2)
    await page.mouse.down()
    // One move, no steps: the first pointermove is already over another lane.
    await page.mouse.move(target.x, target.y - 20)
    await expect(page.locator('.drag-overlay')).toHaveCount(1)
    expect(await page.evaluate(() => window.getSelection()?.toString() ?? '')).toBe('')
    await page.mouse.up()

    await expect.poll(() => titles(page, 'In review')).toEqual(['Plan the offsite', 'Review the budget'])
    expect(sent).toHaveLength(1)
    expect(sent[0]!.body).toMatchObject({
      status: 'in_review',
      placement: { before_id: '5'.repeat(32), after_id: null, expected_order_version: 1 },
    })
  })

  test('a single move outside the board still picks the card up, so the drag can continue', async ({ page }) => {
    const sent = await open(page, 'task-reorder-single-move-outside')
    const text = page.locator('.task-card', { hasText: 'Plan the offsite' }).locator('.task-title')
    const box = (await text.boundingBox())!
    const target = await cardCentre(page.locator('.task-card', { hasText: 'Draft the agenda' }))
    await page.mouse.move(box.x + 4, box.y + box.height / 2)
    await page.mouse.down()
    // A single move straight out of the board leaves no root pointermove to
    // read, because the surface press captures only after the threshold.
    await page.mouse.move(60, target.y)
    await expect(page.locator('.drag-overlay')).toHaveCount(1)
    expect(await page.evaluate(() => window.getSelection()?.toString() ?? '')).toBe('')
    // Captured by now, so the rest of the drag tracks and lands normally. The
    // release is clearly above Draft's midpoint: exactly on it is a tie that
    // subpixel pointer rounding can decide either way.
    await page.mouse.move(target.x, target.y - 10, { steps: 8 })
    await page.mouse.up()

    await expect.poll(() => titles(page, 'In progress')).toEqual(['Plan the offsite', 'Draft the agenda'])
    expect(sent).toHaveLength(1)
    expect(sent[0]!.body).toMatchObject({
      status: 'in_progress',
      placement: { before_id: '4'.repeat(32), after_id: null, expected_order_version: 1 },
    })
  })

  test('a pen-style capture transfer from the card to the board does not cancel the drag', async ({ page }) => {
    const sent = await open(page, 'task-reorder-capture-transfer')
    await page.evaluate(() => {
      const card = [...document.querySelectorAll<HTMLElement>('.task-card')]
        .find((el) => el.textContent?.includes('Send the invites'))!
      // A pen implicitly captures the card it pressed. Transferring that capture
      // to the board as the drag activates reports the card's release here.
      card.addEventListener('pointerdown', (e) => card.setPointerCapture((e as PointerEvent).pointerId))
      const lost: string[] = []
      ;(window as unknown as { __lost: string[] }).__lost = lost
      document.addEventListener('lostpointercapture', (e) => {
        lost.push((e.target as HTMLElement).className)
      }, true)
    })
    const before = await cardCentre(page.locator('.task-card', { hasText: 'Plan the offsite' }))
    await dragSurfaceTo(page, 'Send the invites', { x: before.x, y: before.y - 15 })

    await expect.poll(() => titles(page, 'To do')).toEqual(['Send the invites', 'Plan the offsite', 'Book the venue'])
    expect(sent).toHaveLength(1)
    // The transfer really did fire on the card; the drag survived it.
    const lost = await page.evaluate(() => (window as unknown as { __lost: string[] }).__lost)
    expect(lost.some((cls) => cls.includes('task-card'))).toBe(true)
  })

  test('a drop through an overlay does not move the card', async ({ page }) => {
    const sent = await open(page, 'task-reorder-overlay')
    const text = page.locator('.task-card', { hasText: 'Send the invites' }).locator('.task-title')
    const box = (await text.boundingBox())!
    await page.mouse.move(box.x + 4, box.y + box.height / 2)
    await page.mouse.down()
    await page.mouse.move(box.x + 4, box.y + box.height / 2 + 12, { steps: 3 })
    await expect(page.locator('.drag-overlay')).toHaveCount(1)

    // Something opens over the board while the card is being moved.
    await page.evaluate(() => {
      const cover = document.createElement('div')
      cover.className = 'e2e-cover'
      Object.assign(cover.style, {
        position: 'fixed', inset: '0', zIndex: '2000', background: 'rgb(0 0 0 / 20%)',
      })
      document.body.append(cover)
    })
    const target = await cardCentre(page.locator('.task-card', { hasText: 'Draft the agenda' }))
    await page.mouse.move(target.x, target.y, { steps: 8 })
    await page.mouse.up()

    await expect(page.locator('.drag-overlay')).toHaveCount(0)
    expect(sent).toHaveLength(0)
    expect(await titles(page, 'To do')).toEqual(['Plan the offsite', 'Book the venue', 'Send the invites'])
    expect(await titles(page, 'In progress')).toEqual(['Draft the agenda'])
    await page.evaluate(() => document.querySelector('.e2e-cover')?.remove())
  })
})

test.describe('lane drop areas', () => {
  test.use({ viewport: { width: 1440, height: 900 } })

  test('a short lane reaches the board foot and accepts a drop under its last card', async ({ page }) => {
    const sent = await open(page, 'task-reorder-lane-height')
    const board = (await page.locator('.task-lanes').boundingBox())!
    const short = lane(page, 'In review')
    const shortBox = (await short.boundingBox())!
    const last = (await short.locator('.task-card').last().boundingBox())!
    // Every lane is as tall as the board, not just as its own last card.
    expect(Math.abs(shortBox.y + shortBox.height - (board.y + board.height))).toBeLessThanOrEqual(32)
    expect(shortBox.y + shortBox.height - (last.y + last.height)).toBeGreaterThan(100)

    await dragSurfaceTo(page, 'Plan the offsite', {
      x: shortBox.x + shortBox.width / 2,
      y: shortBox.y + shortBox.height - 16,
    })
    await expect.poll(() => titles(page, 'In review')).toEqual(['Review the budget', 'Plan the offsite'])
    expect(sent).toHaveLength(1)
    expect(sent[0]!.body).toMatchObject({
      status: 'in_review',
      placement: { before_id: null, after_id: '5'.repeat(32), expected_order_version: 1 },
    })
  })

  test('a release outside the board cancels with no write', async ({ page }) => {
    const sent = await open(page, 'task-reorder-outside')
    const board = (await page.locator('.task-lanes').boundingBox())!
    const column = (await lane(page, 'To do').boundingBox())!
    await dragSurfaceTo(page, 'Plan the offsite', { x: column.x + 100, y: board.y - 20 })
    await expect(page.locator('.drag-overlay')).toHaveCount(0)
    expect(sent).toHaveLength(0)
    expect(await titles(page, 'To do')).toEqual(['Plan the offsite', 'Book the venue', 'Send the invites'])
    await expect(page.getByRole('status').filter({ hasText: 'Move cancelled' })).toHaveCount(1)
  })
})

test.describe('the grip on a fine pointer', () => {
  test.use({ viewport: { width: 1440, height: 900 } })

  test('is hidden at rest, shown on hover and focus, and the hovered card is not dimmed', async ({ page }) => {
    await open(page, 'task-reorder-hover')
    const card = page.locator('.task-card', { hasText: 'Plan the offsite' })
    const opacity = (loc: Locator) => loc.evaluate(el => getComputedStyle(el).opacity)
    await page.mouse.move(0, 0)
    await expect.poll(() => opacity(card.locator('.drag-grip'))).toBe('0')

    await card.hover()
    await expect.poll(() => opacity(card.locator('.drag-grip'))).toBe('1')
    // The card and every ancestor up to the board draw at full strength.
    const dimmed = await card.evaluate(el => {
      const out: string[] = []
      for (let e: Element | null = el; e && !e.classList.contains('task-lanes'); e = e.parentElement) {
        if (getComputedStyle(e).opacity !== '1') out.push(`${e.className}: ${getComputedStyle(e).opacity}`)
      }
      return out
    })
    expect(dimmed).toEqual([])

    await page.mouse.move(0, 0)
    await expect.poll(() => opacity(card.locator('.drag-grip'))).toBe('0')
    await card.locator('.task-open').focus()
    await expect.poll(() => opacity(card.locator('.drag-grip'))).toBe('1')
  })
})

test.describe('placing cards by keyboard', () => {
  test.use({ viewport: { width: 1440, height: 900 } })

  test('Space picks a card up, the arrows move it, Enter drops it, and Shift+Arrow still changes lane', async ({ page }) => {
    let releaseOrder!: () => void
    const orderHeld = new Promise<void>((resolve) => { releaseOrder = resolve })
    const sent = await open(page, 'task-reorder-keyboard', orderHeld)
    const grip = page.locator('.task-card', { hasText: 'Plan the offsite' }).locator('.drag-grip')
    await grip.focus()
    await page.keyboard.press('Space')
    await expect(page.getByRole('status').filter({ hasText: 'Picked up Plan the offsite' })).toHaveCount(1)
    await page.keyboard.press('ArrowDown')
    await expect.poll(() => titles(page, 'To do')).toEqual(['Book the venue', 'Plan the offsite', 'Send the invites'])
    await page.keyboard.press('Enter')

    // Move to another card while the order write is still in flight. The drop's
    // deferred focus restoration must not pull focus back to the first grip.
    const second = page.locator('.task-card', { hasText: 'Send the invites' }).locator('.task-open')
    try {
      await second.focus()
      await expect(second).toBeFocused()
    } finally {
      releaseOrder()
    }
    // Wait on the board actually adopting the saved order, not on a request
    // count or on the old grip already holding focus: the preview disappears
    // only once the write has settled.
    await expect(page.locator('.drag-lifted')).toHaveCount(0)
    await expect.poll(() => titles(page, 'To do')).toEqual(['Book the venue', 'Plan the offsite', 'Send the invites'])
    expect(sent).toHaveLength(1)
    await expect(second).toBeFocused()

    // The old one-lane move, with no placement.
    await page.keyboard.press('Shift+ArrowRight')
    await expect.poll(() => titles(page, 'In progress')).toContain('Send the invites')
    expect(sent).toHaveLength(2)
    expect(sent[1]!.path).toBe(`/api/tasks/${'3'.repeat(32)}`)
    expect(sent[1]!.body).toMatchObject({ status: 'in_progress' })
    expect(sent[1]!.body).not.toHaveProperty('placement')
  })

  test('Move up and Move down in the card menu reorder it', async ({ page }) => {
    const sent = await open(page, 'task-reorder-menu')
    await page.locator('.task-card', { hasText: 'Book the venue' }).getByRole('button', { name: /^Actions for/ }).click()
    await page.getByRole('menuitem', { name: 'Move up' }).click()
    await expect.poll(() => titles(page, 'To do')).toEqual(['Book the venue', 'Plan the offsite', 'Send the invites'])
    expect(sent).toHaveLength(1)
    await page.locator('.task-card', { hasText: 'Book the venue' }).getByRole('button', { name: /^Actions for/ }).click()
    await expect(page.getByRole('menuitem', { name: 'Move up' })).toHaveAttribute('aria-disabled', 'true')
  })
})

test.describe('placing cards on a narrow, scrolled board', () => {
  test.use({ viewport: { width: 700, height: 800 } })

  test('drops into a lane that was scrolled into view, with the grip target large enough', async ({ page }) => {
    const sent = await open(page, 'task-reorder-narrow')
    await page.locator('.task-lanes').evaluate(el => { el.scrollLeft = 320 })
    await dragGrip(page, 'Draft the agenda', page.locator('.task-card', { hasText: 'Review the budget' }), -15)
    await expect.poll(() => titles(page, 'In review')).toEqual(['Draft the agenda', 'Review the budget'])
    expect(sent).toHaveLength(1)
    // The page itself never scrolls sideways.
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
  })

  test('a lane clipped by the board edge is not a drop target', async ({ page }) => {
    const sent = await open(page, 'task-reorder-clipped-lane')
    await page.locator('.task-lanes').evaluate(el => { el.scrollLeft = 0 })
    const setup = await page.evaluate(() => {
      const board = document.querySelector('.task-lanes')!.getBoundingClientRect()
      const lane = [...document.querySelectorAll<HTMLElement>('.task-lane')].find((el) => {
        const rect = el.getBoundingClientRect()
        return rect.left < board.right - 1 && rect.right > board.right + 40
      })
      if (!lane) return null
      const rect = lane.getBoundingClientRect()
      return {
        board: { left: board.left, right: board.right },
        lane: { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom, status: lane.dataset.lane },
      }
    })
    expect(setup, 'a lane must sit partly past the board’s right edge').not.toBeNull()
    // The point is inside the lane's own rect but past the board's visible edge,
    // so only the un-clipped bounding box would call it a target.
    const x = setup!.board.right + 12
    const y = Math.min(setup!.lane.top + 320, setup!.lane.bottom - 20)
    expect(x).toBeLessThan(700)
    expect(x).toBeGreaterThan(setup!.lane.left)
    expect(x).toBeLessThan(setup!.lane.right)

    await dragSurfaceTo(page, 'Plan the offsite', { x, y })

    await expect(page.locator('.drag-overlay')).toHaveCount(0)
    expect(sent).toHaveLength(0)
    expect(await titles(page, 'To do')).toEqual(['Plan the offsite', 'Book the venue', 'Send the invites'])
    await expect(page.getByRole('status').filter({ hasText: 'Move cancelled' })).toHaveCount(1)
  })
})

test.describe('placing cards by touch', () => {
  const { defaultBrowserType: _ignored, ...iphone } = devices['iPhone 13']
  test.use(iphone)

  test('the grip is a 44px target and a touch drag reorders', async ({ page, context }) => {
    const sent = await open(page, 'task-reorder-touch')
    const card = page.locator('.task-card', { hasText: 'Send the invites' })
    const grip = card.locator('.drag-grip')
    const hit = (await grip.boundingBox())!
    expect(hit.width).toBeGreaterThanOrEqual(44)
    expect(hit.height).toBeGreaterThanOrEqual(44)

    const cdp = await context.newCDPSession(page)
    const box = (await grip.boundingBox())!
    const target = (await page.locator('.task-card', { hasText: 'Plan the offsite' }).boundingBox())!
    const start = { x: box.x + box.width / 2, y: box.y + box.height / 2 }
    const touch = (type: 'touchStart' | 'touchMove' | 'touchEnd', x: number, y: number) =>
      cdp.send('Input.dispatchTouchEvent', { type, touchPoints: type === 'touchEnd' ? [] : [{ x, y, id: 1 }] })
    await touch('touchStart', start.x, start.y)
    for (let step = 1; step <= 10; step++) {
      await touch('touchMove', start.x, start.y + (target.y + 4 - start.y) * (step / 10))
    }
    await touch('touchEnd', 0, 0)
    await expect.poll(() => sent.length).toBe(1)
    await expect.poll(() => titles(page, 'To do')).toEqual(['Send the invites', 'Plan the offsite', 'Book the venue'])
  })

  test('a swipe on the card text scrolls the board instead of dragging', async ({ page, context }) => {
    const sent = await open(page, 'task-reorder-touch-scroll')
    const board = page.locator('.task-lanes')
    const boardBox = (await board.boundingBox())!
    const text = page.locator('.task-card', { hasText: 'Plan the offsite' }).locator('.task-title')
    const box = (await text.boundingBox())!
    const before = await board.evaluate(el => el.scrollLeft)
    const y = box.y + box.height / 2
    const startX = Math.min(box.x + box.width / 2, boardBox.x + boardBox.width - 40)

    const cdp = await context.newCDPSession(page)
    const touch = (type: 'touchStart' | 'touchMove' | 'touchEnd', x: number) =>
      cdp.send('Input.dispatchTouchEvent', { type, touchPoints: type === 'touchEnd' ? [] : [{ x, y, id: 1 }] })
    await touch('touchStart', startX)
    for (let step = 1; step <= 12; step++) await touch('touchMove', startX - step * 12)
    await touch('touchEnd', 0)

    // The horizontal pan belongs to the board, not to the card's drag surface.
    await expect.poll(() => board.evaluate(el => el.scrollLeft)).toBeGreaterThan(before)
    await expect(page.locator('.drag-overlay')).toHaveCount(0)
    expect(sent).toHaveLength(0)
    await expect(page.locator('#task-detail-name')).toHaveCount(0)
  })
})
