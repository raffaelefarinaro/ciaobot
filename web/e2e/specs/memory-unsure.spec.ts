import { expect, test, type Page, type Route } from '@playwright/test'
import { boot, horizontalOverflow, isolate } from '../support/app'
import { UNSURE_FIXTURE_ITEMS } from '../../src/stories/unsureFixture'
import type { UnsureItem } from '../../src/lib/unsureList'

/**
 * Memory → Unsure: one row per line of Workspace/Unsure.md, acted on through
 * /api/memory/unsure. The route is answered here, statefully, from the
 * thirteen-line synthetic list the stories use, so what a real layout says
 * (rows, rail windows, the sidebar count moving, touch targets, no sideways
 * scroll) is checked against the same data.
 */
type State = { items: UnsureItem[]; conflictNext?: boolean; posts: unknown[] }

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
    const body = request.postDataJSON() as { op: string; id: string }
    state.posts.push(body)
    const index = state.items.findIndex(i => i.id === body.id)
    if (state.conflictNext || index < 0) {
      state.conflictNext = false
      await route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ error: 'the list changed; reload it', code: 'line_changed' }) })
      return
    }
    if (body.op === 'remove') state.items.splice(index, 1)
    else state.items[index] = { ...state.items[index], checked: body.op === 'tick', id: `${body.op[0]}${state.items[index].id.slice(1)}` }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(payload(state)) })
  })
  return state
}

const FIRST = UNSURE_FIXTURE_ITEMS[0].title

test.describe('Memory › Unsure', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `memory-unsure-${testInfo.workerIndex}`)
  })

  test('lists one row per line, with the rail windows and the sidebar count', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    await answerList(page)
    await boot(page, '/memory/unsure', '.mu-row')

    await expect(page.locator('.page-tag')).toContainText('Unsure')
    await expect(page.locator('.mu-row')).toHaveCount(13)
    await expect(page.locator('.mu-group-title').first()).toHaveText('Waiting for you 12')
    await expect(page.locator('.mu-row').first().locator('.mu-row-title')).toHaveText(FIRST)
    await expect(page.locator('.mm-review-rail .docked-window')).toHaveCount(2)
    await expect(page.getByRole('heading', { name: 'How this works' })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Always loaded' })).toBeVisible()
    await expect(page.locator('a[href="/memory/unsure"] .memory-nav-count')).toHaveText('12')
    await expect(page.locator('a[href="/memory"]')).toHaveAttribute('data-count', '12')
    await page.screenshot({ path: 'test-results/memory-unsure-desktop.png', fullPage: true })
  })

  test('Approve ticks the line, the count drops, and Undo puts it back', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    const state = await answerList(page)
    await boot(page, '/memory/unsure', '.mu-row')

    await page.getByRole('button', { name: `Approve: ${FIRST}` }).click()
    await expect(page.getByText('Approved: applied by the next care run.').first()).toBeVisible()
    expect(state.posts[0]).toEqual({ workspace: 'alpha', op: 'tick', id: UNSURE_FIXTURE_ITEMS[0].id })
    await expect(page.locator('a[href="/memory/unsure"] .memory-nav-count')).toHaveText('11')
    await page.getByRole('button', { name: `Undo approval: ${FIRST}` }).click()
    await expect(page.locator('a[href="/memory/unsure"] .memory-nav-count')).toHaveText('12')
    expect((state.posts[1] as { op: string }).op).toBe('untick')
  })

  test('Reject removes the line; a stale page is told and reads the list again', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    const state = await answerList(page)
    await boot(page, '/memory/unsure', '.mu-row')

    await page.getByRole('button', { name: `Reject: ${FIRST}` }).click()
    await expect(page.locator('.mu-row')).toHaveCount(12)
    state.conflictNext = true
    await page.getByRole('button', { name: `Reject: ${UNSURE_FIXTURE_ITEMS[1].title}` }).click()
    await expect(page.locator('.mu-notice')).toContainText('The list changed since this page read it')
    await expect(page.locator('.mu-row')).toHaveCount(12)
  })

  test('Discuss, and Approve on a repository item, open a new chat in the General project', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    await answerList(page)
    const projects = await (await page.request.get('/api/projects')).json()
    const marked = projects.map((p: { name: string }) => (p.name === 'General' ? { ...p, is_auto: true } : p))
    expect((await page.request.post('/__fixture__/projects', { data: { projects: marked } })).ok()).toBe(true)
    await boot(page, '/memory/unsure', '.mu-row')

    const repo = UNSURE_FIXTURE_ITEMS.find(i => i.kind === 'repo')!
    const created = page.waitForRequest(request => request.method() === 'POST' && request.url().endsWith('/api/projects/alpha-general/chats'))
    await page.getByRole('button', { name: `Approve in a chat: ${repo.title}` }).click()
    const body = (await created).postDataJSON() as { title: string; draft?: string }
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

  test('does not scroll sideways, puts the rail under the list and keeps every action 44px', async ({ page }) => {
    await answerList(page)
    await boot(page, '/memory/unsure', '.mu-row')

    const { overflow, culprits } = await horizontalOverflow(page)
    expect(overflow, `scrolls ${overflow}px sideways; widest: ${JSON.stringify(culprits)}`).toBeLessThanOrEqual(0)
    const row = page.locator('.mu-row').first()
    for (const name of [`Approve: ${FIRST}`, `Reject: ${FIRST}`, `Discuss: ${FIRST}`]) {
      const box = await row.getByRole('button', { name }).boundingBox()
      expect(box, name).not.toBeNull()
      expect(box!.height, name).toBeGreaterThanOrEqual(44)
      expect(box!.width, name).toBeGreaterThanOrEqual(44)
    }
    const list = await page.locator('.mu').boundingBox()
    const rail = await page.locator('.mm-review-rail').boundingBox()
    expect(rail!.y).toBeGreaterThan(list!.y + list!.height - 1)
    await page.screenshot({ path: 'test-results/memory-unsure-phone.png', fullPage: true })
  })
})
