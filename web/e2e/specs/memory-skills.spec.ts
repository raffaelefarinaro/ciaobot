import { expect, test, type Page, type Route } from '@playwright/test'
import { boot, horizontalOverflow, isolate } from '../support/app'
import { skillFixtureItems } from '../../src/stories/skillsFixture'
import type { SkillProposalItem } from '../../src/lib/skillProposals'

/**
 * Memory → Skills: the open skill-edit proposals as a review queue. The list
 * route and the proposal files (`/api/workspace-file`, the file viewer's own
 * read and save) are answered here, statefully, from the stories' three
 * synthetic proposals, so the two panes, the keys, Reject writing only the
 * status line, Undo, the sidebar count and the phone card are checked in a
 * real layout.
 */
type State = { items: SkillProposalItem[]; statuses: Record<string, string>; saves: Array<{ path: string; content: string; expected_content?: string }> }

const ITEMS = skillFixtureItems('alpha')
const [FIRST, SECOND] = ITEMS

function fileText(item: SkillProposalItem, status: string): string {
  return ['---', 'type: skill-edit', `target: ${item.target}`, `title: "${item.title}"`, 'summary: s', 'rationale: r',
    'evidence:', '  - chat-a290cb48', 'proposed_change: c', `created: ${item.created}`, `status: ${status}`, '---', '', 'Body.', ''].join('\n')
}

async function answer(page: Page, items: SkillProposalItem[] = ITEMS): Promise<State> {
  const state: State = { items, statuses: Object.fromEntries(items.map(i => [i.open_path, i.status])), saves: [] }
  const open = () => state.items.filter(i => state.statuses[i.open_path] === 'draft' || state.statuses[i.open_path] === 'ready')
  await page.route(/\/api\/memory\/skill-proposals(\?.*)?$/, async (route: Route) => {
    const listed = open()
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ workspace: 'alpha', found: true, items: listed, counts: { waiting: listed.length }, skipped: 0, truncated: false }) })
  })
  await page.route(/\/api\/workspace-file(\?.*)?$/, async (route: Route) => {
    const request = route.request()
    if (request.method() === 'POST') {
      const body = request.postDataJSON() as { path: string; content: string; expected_content?: string }
      const item = state.items.find(i => i.open_path === body.path)!
      // The engine's conditional save: only over the text the page read.
      if (body.expected_content !== fileText(item, state.statuses[body.path])) {
        await route.fulfill({ status: 409, contentType: 'application/json', body: '{"code":"file_changed"}' })
        return
      }
      state.saves.push(body)
      state.statuses[body.path] = /^status: (\w+)$/m.exec(body.content)![1]
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{"ok":true}' })
      return
    }
    const path = new URL(request.url()).searchParams.get('path') ?? ''
    const item = state.items.find(i => i.open_path === path)
    if (!item) { await route.fulfill({ status: 404, body: 'not found' }); return }
    await route.fulfill({ status: 200, contentType: 'text/plain', body: fileText(item, state.statuses[path]) })
  })
  return state
}

const detailTitle = (page: Page) => page.locator('#msk-detail-title')

test.describe('Memory › Skills', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `memory-skills-${testInfo.workerIndex}`)
  })

  test('shows the suggestions beside the open one, its change as a diff', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 960 })
    await answer(page)
    await boot(page, '/memory/skills', '[data-skills-row]')

    await expect(page.locator('.page-tag')).toContainText('Skills')
    await expect(page.locator('[data-skills-row]')).toHaveCount(3)
    await expect(page.locator('[data-skills-count]')).toHaveText('1 of 3')
    await expect(detailTitle(page)).toHaveText(FIRST.title)
    await expect(page.locator('.pd-change-count')).toHaveText('+2 −1')
    await expect(page.locator('.pd-diff-row--add')).toHaveCount(2)
    await expect(page.getByRole('link', { name: 'Open the chat chat-a290cb48' })).toHaveAttribute('href', '/chat/chat-a290cb48')
    const list = await page.locator('.rq-list-pane').boundingBox()
    const detail = await page.locator('.rq-detail').boundingBox()
    expect(detail!.x).toBeGreaterThan(list!.x + list!.width - 2)
    await expect(page.locator('a[href="/memory/skills"] .memory-nav-count')).toHaveText('3')
    await page.screenshot({ path: 'test-results/memory-skills-desktop.png', fullPage: true })
  })

  test('D rejects (only the status line), moves on, and Undo puts it back', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    const state = await answer(page)
    await boot(page, '/memory/skills', '[data-skills-row]')

    await page.keyboard.press('d')
    await expect(detailTitle(page)).toHaveText(SECOND.title)
    expect(state.saves).toHaveLength(1)
    expect(state.saves[0].path).toBe(FIRST.open_path)
    expect(state.saves[0].content).toBe(fileText(FIRST, 'rejected'))
    await expect(page.locator('a[href="/memory/skills"] .memory-nav-count')).toHaveText('2')
    await expect(page.locator('[data-skills-notice]')).toContainText(`Rejected “${FIRST.title}”.`)

    await page.getByRole('button', { name: 'Undo' }).click()
    await expect(detailTitle(page)).toHaveText(FIRST.title)
    expect(state.saves[1].content).toBe(fileText(FIRST, 'ready'))
    await expect(page.locator('a[href="/memory/skills"] .memory-nav-count')).toHaveText('3')

    await page.locator('[data-skills-row][aria-selected="true"]').focus()
    await page.keyboard.press('ArrowDown')
    await expect(detailTitle(page)).toHaveText(SECOND.title)
    await expect(page.locator('[data-skills-row][aria-selected="true"]')).toBeFocused()
  })

  test('Approve opens a new chat in the General project and writes no file', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    const state = await answer(page)
    const projects = await (await page.request.get('/api/projects')).json()
    const marked = projects.map((p: { name: string }) => (p.name === 'General' ? { ...p, is_auto: true } : p))
    expect((await page.request.post('/__fixture__/projects', { data: { projects: marked } })).ok()).toBe(true)
    await boot(page, '/memory/skills', '[data-skills-row]')

    const created = page.waitForRequest(request => request.method() === 'POST' && request.url().endsWith('/api/projects/alpha-general/chats'))
    await page.getByRole('button', { name: `Approve in a chat: ${FIRST.title}` }).click()
    const body = (await created).postDataJSON() as { title: string }
    expect(body.title).toBe(`Approve: ${FIRST.title}`)
    expect(state.saves).toHaveLength(0)
  })
})

test.describe('Memory › Skills on a phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `memory-skills-phone-${testInfo.workerIndex}`)
  })

  test('one card at a time, no sideways scroll, every control 44px, swipe left rejects', async ({ page }) => {
    const state = await answer(page)
    await boot(page, '/memory/skills', '[data-review-card]')

    await expect(page.locator('[data-skills-count]')).toHaveText('1 of 3')
    const { overflow, culprits } = await horizontalOverflow(page)
    expect(overflow, `scrolls ${overflow}px sideways; widest: ${JSON.stringify(culprits)}`).toBeLessThanOrEqual(0)
    for (const name of [`Approve in a chat: ${FIRST.title}`, `Reject: ${FIRST.title}`, `Skip: ${FIRST.title}`]) {
      const box = await page.getByRole('button', { name }).boundingBox()
      expect(box, name).not.toBeNull()
      expect(box!.height, name).toBeGreaterThanOrEqual(44)
      expect(box!.width, name).toBeGreaterThanOrEqual(44)
    }
    await page.screenshot({ path: 'test-results/memory-skills-phone.png' })

    const card = page.locator('[data-review-card]')
    const box = (await card.boundingBox())!
    const x = box.x + box.width / 2
    const y = box.y + 120
    await page.mouse.move(x, y)
    await page.mouse.down()
    for (let step = 1; step <= 8; step++) await page.mouse.move(x - (180 * step) / 8, y + step)
    await expect(page.locator('.rvd-stamp--dismiss')).toHaveText('REJECT')
    await page.mouse.up()
    await expect(card).toContainText(SECOND.title)
    expect(state.saves[0].content).toBe(fileText(FIRST, 'rejected'))
  })
})
