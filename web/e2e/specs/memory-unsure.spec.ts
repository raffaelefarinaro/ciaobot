import { expect, test, type Page, type Route } from '@playwright/test'
import { boot, horizontalOverflow, isolate, TOUCH_FLOOR } from '../support/app'
import { UNSURE_FIXTURE_ITEMS } from '../../src/stories/unsureFixture'
import type { UnsureItem } from '../../src/lib/unsureList'

/**
 * Memory → Unsure as a review queue over Workspace/Unsure.md, acted on through
 * /api/memory/unsure. The route is answered here, statefully, from the
 * thirteen-line synthetic list the stories use, so what a real layout does
 * (the two panes, the keys, the phone's swipe, the sidebar count moving, touch
 * targets, no sideways scroll) is checked against the same data.
 */
type State = { items: UnsureItem[]; conflictNext?: boolean; posts: Array<Record<string, string>> }

function payload(state: State) {
  const ticked = state.items.filter(i => i.checked).length
  return {
    workspace: 'alpha', path: 'memory-vault/alpha/Workspace/Unsure.md', found: state.items.length > 0,
    items: state.items, counts: { waiting: state.items.length - ticked, ticked },
  }
}

async function answerList(page: Page, items: UnsureItem[] = UNSURE_FIXTURE_ITEMS): Promise<State> {
  const state: State = { items: items.map(i => ({ ...i })), posts: [] }
  await page.route(/\/api\/memory\/unsure(\?.*)?$/, async (route: Route) => {
    const request = route.request()
    if (request.method() === 'GET') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(payload(state)) })
      return
    }
    const body = request.postDataJSON() as Record<string, string>
    state.posts.push(body)
    if (body.op === 'restore') {
      const removed = items.find(i => i.raw === body.line)!
      const at = state.items.findIndex(i => i.id === body.before)
      state.items.splice(at < 0 ? state.items.length : at, 0, { ...removed })
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(payload(state)) })
      return
    }
    const index = state.items.findIndex(i => i.id === body.id)
    if (state.conflictNext || index < 0) {
      state.conflictNext = false
      await route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ error: 'the list changed; reload it', code: 'line_changed' }) })
      return
    }
    const current = state.items[index]
    if (body.op === 'remove') state.items.splice(index, 1)
    else {
      const checked = body.op === 'tick'
      state.items[index] = {
        ...current, checked, id: `${body.op[0]}${current.id.slice(1)}`,
        raw: current.raw.replace(/^- \[[ x]\]/, checked ? '- [x]' : '- [ ]'),
      }
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(payload(state)) })
  })
  return state
}

const [FIRST, SECOND, THIRD] = UNSURE_FIXTURE_ITEMS
const detailTitle = (page: Page) => page.locator('#mu-detail-title')

test.describe('Memory › Unsure', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `memory-unsure-${testInfo.workerIndex}`)
  })

  test('shows the queue beside the open item, with what approving saves and where', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 960 })
    await answerList(page)
    await boot(page, '/memory/unsure', '[data-unsure-row]')

    await expect(page.locator('.page-tag')).toContainText('Unsure')
    await expect(page.locator('[data-unsure-row]')).toHaveCount(13)
    await expect(page.locator('[data-unsure-count]')).toHaveText('1 of 12')
    await expect(detailTitle(page)).toHaveText(FIRST.title)
    await expect(page.locator('.mud-effect-line')).toHaveText(FIRST.proposal)
    await expect(page.locator('.mud-effect-where')).toContainText(`Saved to ${FIRST.destination}`)
    await expect(page.getByRole('link', { name: 'Open the chat chat-7e11530b' })).toHaveAttribute('href', '/chat/chat-7e11530b')
    // Two panes side by side, the rail's two windows beside them.
    const list = await page.locator('.mu-list-pane').boundingBox()
    const detail = await page.locator('.mu-detail').boundingBox()
    expect(detail!.x).toBeGreaterThan(list!.x + list!.width - 2)
    await expect(page.locator('.mm-review-rail .docked-window')).toHaveCount(2)
    await expect(page.locator('a[href="/memory/unsure"] .memory-nav-count')).toHaveText('12')
    await page.screenshot({ path: 'test-results/memory-unsure-desktop.png', fullPage: true })
  })

  test('A approves and moves on, D dismisses, Undo puts it back, and the arrows move', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    const state = await answerList(page)
    await boot(page, '/memory/unsure', '[data-unsure-row]')

    await page.keyboard.press('a')
    await expect(detailTitle(page)).toHaveText(SECOND.title)
    expect(state.posts[0]).toEqual({ workspace: 'alpha', op: 'tick', id: FIRST.id })
    await expect(page.locator('a[href="/memory/unsure"] .memory-nav-count')).toHaveText('11')
    await expect(page.locator('[data-unsure-live]')).toContainText(`Approved “${FIRST.title}”.`)

    await page.keyboard.press('d')
    await expect(detailTitle(page)).toHaveText(THIRD.title)
    expect(state.posts[1]).toEqual({ workspace: 'alpha', op: 'remove', id: SECOND.id })
    await page.getByRole('button', { name: 'Undo' }).click()
    await expect(detailTitle(page)).toHaveText(SECOND.title)
    expect(state.posts[2]).toEqual({ workspace: 'alpha', op: 'restore', line: SECOND.raw, before: THIRD.id, after: FIRST.id.replace(/^./, 't'), section: 'Unsure' })

    // Arrows move; focus on a row moves with them.
    await page.locator('[data-unsure-row][aria-selected="true"]').focus()
    await page.keyboard.press('ArrowDown')
    await expect(detailTitle(page)).toHaveText(THIRD.title)
    await expect(page.locator('[data-unsure-row][aria-selected="true"]')).toBeFocused()
    await page.keyboard.press('s')
    await expect(detailTitle(page)).toHaveText(UNSURE_FIXTURE_ITEMS[3].title)
    expect(state.posts).toHaveLength(3)
  })

  test('a stale page is told and reads the list again', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    const state = await answerList(page)
    await boot(page, '/memory/unsure', '[data-unsure-row]')
    state.conflictNext = true
    await page.getByRole('button', { name: `Dismiss: ${FIRST.title}` }).click()
    await expect(page.locator('[data-unsure-notice]')).toContainText('The list changed since this page read it')
    await expect(page.locator('[data-unsure-row]')).toHaveCount(13)
  })

  test('Approve on a repository item opens a new chat in the General project', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    await answerList(page)
    const projects = await (await page.request.get('/api/projects')).json()
    const marked = projects.map((p: { name: string }) => (p.name === 'General' ? { ...p, is_auto: true } : p))
    expect((await page.request.post('/__fixture__/projects', { data: { projects: marked } })).ok()).toBe(true)
    await boot(page, '/memory/unsure', '[data-unsure-row]')

    const repo = UNSURE_FIXTURE_ITEMS.find(i => i.kind === 'repo')!
    await page.locator('[data-unsure-row]', { hasText: repo.title }).click()
    await expect(page.locator('.mud-effect-where')).toContainText(`${repo.qualifier}/AGENTS.md`)
    const created = page.waitForRequest(request => request.method() === 'POST' && request.url().endsWith('/api/projects/alpha-general/chats'))
    await page.getByRole('button', { name: `Approve in a chat: ${repo.title}` }).click()
    const body = (await created).postDataJSON() as { title: string }
    expect(body.title).toMatch(/^Settle: /)
  })

  test('says so when nothing is waiting', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    await answerList(page, [])
    await boot(page, '/memory/unsure', '.mu-state')

    await expect(page.locator('.mu')).toContainText('Nothing is waiting. Care adds a line here only when it is not sure enough to write something down.')
    await expect(page.locator('a[href="/memory/unsure"] .memory-nav-count')).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Open Unsure.md' })).toBeDisabled()
  })
})

test.describe('Memory › Unsure on a phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `memory-unsure-phone-${testInfo.workerIndex}`)
  })

  test('one card at a time, no sideways scroll, every control 44px', async ({ page }) => {
    await answerList(page)
    await boot(page, '/memory/unsure', '[data-unsure-card]')

    await expect(page.locator('[data-unsure-count]')).toHaveText('1 of 12')
    await expect(page.locator('[data-unsure-card]')).toContainText(FIRST.title)
    const { overflow, culprits } = await horizontalOverflow(page)
    expect(overflow, `scrolls ${overflow}px sideways; widest: ${JSON.stringify(culprits)}`).toBeLessThanOrEqual(0)
    for (const name of [`Approve: ${FIRST.title}`, `Dismiss: ${FIRST.title}`, `Skip: ${FIRST.title}`, 'Open Unsure.md']) {
      const box = await page.getByRole('button', { name }).boundingBox()
      expect(box, name).not.toBeNull()
      expect(box!.height, name).toBeGreaterThanOrEqual(TOUCH_FLOOR)
      expect(box!.width, name).toBeGreaterThanOrEqual(TOUCH_FLOOR)
    }
    await page.screenshot({ path: 'test-results/memory-unsure-phone.png' })
  })

  test('swiping the card right approves and left dismisses; a short drag springs back', async ({ page }) => {
    const state = await answerList(page)
    await boot(page, '/memory/unsure', '[data-unsure-card]')
    const card = page.locator('[data-unsure-card]')

    async function swipe(dx: number, hold = false): Promise<void> {
      const box = (await card.boundingBox())!
      const x = box.x + box.width / 2
      const y = box.y + 120
      await page.mouse.move(x, y)
      await page.mouse.down()
      for (let step = 1; step <= 8; step++) await page.mouse.move(x + (dx * step) / 8, y + step)
      if (hold) return
      await page.mouse.up()
    }

    await swipe(60)
    await expect(card).toContainText(FIRST.title)
    expect(state.posts).toHaveLength(0)

    await swipe(180, true)
    await expect(page.locator('.mudk-stamp--approve')).toHaveCSS('opacity', '1')
    await page.screenshot({ path: 'test-results/memory-unsure-phone-drag.png' })
    await page.mouse.up()
    await expect(card).toContainText(SECOND.title)
    expect(state.posts[0]).toEqual({ workspace: 'alpha', op: 'tick', id: FIRST.id })

    await swipe(-180)
    await expect(card).toContainText(THIRD.title)
    expect(state.posts[1]).toEqual({ workspace: 'alpha', op: 'remove', id: SECOND.id })
    await expect(page.locator('[data-unsure-count]')).toHaveText('1 of 10')
  })
})
