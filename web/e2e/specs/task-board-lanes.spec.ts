import { devices, expect, test, type Locator, type Page } from '@playwright/test'
import { boot, isolate } from '../support/app'

/**
 * The board's lanes are one fixed width at every pane width (it scrolls
 * sideways instead), a card's state and action share one line, and Done sits
 * behind a tab at the end of the row. Widths, line boxes and the touch targets
 * only exist in a real layout engine, so they are measured here.
 */
const NOW = new Date()

const BASE = {
  project_id: '',
  due: '',
  assignee: 'user',
  chat_id: '',
  attempt_id: '',
  created_at: '2026-01-01T09:00:00+00:00',
  updated_at: NOW.toISOString(),
  revision: 'a'.repeat(64),
  attempt_state: '',
  attempt_outcome: '',
  attempt_summary: '',
  attempt_detail: '',
  live_attempt_id: '',
}

const TASKS = [
  { ...BASE, id: '1'.repeat(32), title: 'Plan the offsite', status: 'backlog', relative_path: 'Workspace/Tasks/a.md' },
  {
    ...BASE, id: '2'.repeat(32), title: 'Draft the quarterly summary', status: 'in_progress', assignee: 'agent',
    attempt_id: 'att-needs', chat_id: 'chat-needs', attempt_state: 'needs_you', live_attempt_id: 'att-needs',
    attempt_summary: 'I need a decision on scope.', relative_path: 'Workspace/Tasks/b.md',
  },
  {
    ...BASE, id: '3'.repeat(32), title: 'Write the migration note', status: 'in_review', assignee: 'agent',
    attempt_id: 'att-review', chat_id: 'chat-review', attempt_state: 'ready_for_review', live_attempt_id: 'att-review',
    relative_path: 'Workspace/Tasks/c.md',
  },
  {
    ...BASE, id: '4'.repeat(32), title: 'Send the invoice', status: 'done',
    completed_at: NOW.toISOString(), relative_path: 'Workspace/Tasks/d.md',
  },
]

async function open(page: Page, id: string) {
  await isolate(page, id)
  await page.route(/\/api\/tasks(?:\?|$)/, route => route.fulfill({
    json: { workspace: 'alpha', tasks: TASKS },
  }))
  await boot(page, '/tasks', '.task-card')
}

/** Pick a card up by its own text and drop it on the middle of `target`, with real pointer moves. */
async function dragBySurface(page: Page, title: string, target: Locator, dy = 0) {
  const card = page.locator('.task-card', { hasText: title })
  const from = (await card.locator('.task-title').boundingBox())!
  const to = (await target.boundingBox())!
  await page.mouse.move(from.x + 4, from.y + from.height / 2)
  await page.mouse.down()
  await page.mouse.move(from.x + 4, from.y + from.height / 2 + 12, { steps: 3 })
  await page.mouse.move(to.x + to.width / 2, to.y + to.height / 2 + dy, { steps: 12 })
  await page.mouse.up()
}

const lane = (page: Page, name: string) => page.locator('.task-lanes').getByRole('region', { name, exact: true })
const toggle = (page: Page) => page.locator('.task-lanes').getByRole('button', { name: /show done tasks/i })

async function laneWidths(page: Page) {
  return page.evaluate(() => Array.from(document.querySelectorAll('.task-lanes--columns > .task-lane'))
    .map(el => el.getBoundingClientRect().width))
}

/**
 * The state word and its action share one row: the action box is taller (a
 * 32px minimum, 44px on touch), so the boxes are centred on each other, and a
 * wrapped band would put the action a whole row below the word.
 */
function expectOneLine(
  status: { y: number; height: number },
  action: { y: number; height: number },
) {
  const centres = Math.abs(status.y + status.height / 2 - (action.y + action.height / 2))
  expect(centres).toBeLessThanOrEqual(2)
  expect(status.height).toBeLessThan(30)
}

async function expectFixedLanes(page: Page) {
  const widths = await laneWidths(page)
  expect(widths.length).toBeGreaterThanOrEqual(3)
  for (const width of widths) expect(Math.abs(width - 310)).toBeLessThanOrEqual(1)
}

for (const width of [1440, 1100, 900]) {
  test.describe(`lanes at ${width}px`, () => {
    test.use({ viewport: { width, height: 900 } })

    test('every lane is 310px wide', async ({ page }) => {
      await open(page, `task-lanes-width-${width}`)
      await expectFixedLanes(page)
      // Expanded Done is the same width too.
      await toggle(page).click()
      await expectFixedLanes(page)
      expect((await laneWidths(page)).length).toBe(4)
      // The computed sizing is the one rule, not a stretched or shrunk lane.
      const sizing = await page.evaluate(() => {
        const style = getComputedStyle(document.querySelector('.task-lanes--columns > .task-lane')!)
        return { grow: style.flexGrow, shrink: style.flexShrink, minWidth: style.minWidth }
      })
      expect(sizing).toEqual({ grow: '0', shrink: '0', minWidth: '0px' })
    })
  })
}

test.describe('lanes at 1440px', () => {
  test.use({ viewport: { width: 1440, height: 900 } })

  test('three lanes and the Done tab fit without scrolling, and the band is one line', async ({ page }) => {
    await open(page, 'task-lanes-fit-1440')
    const fit = await page.evaluate(() => {
      const board = document.querySelector('.task-lanes')!
      const tab = document.querySelector('.task-done-toggle')!.getBoundingClientRect()
      return {
        scrollWidth: board.scrollWidth, clientWidth: board.clientWidth,
        tabLeft: tab.left, tabRight: tab.right, viewport: window.innerWidth,
        tabTop: tab.top, tabBottom: tab.bottom, viewportHeight: window.innerHeight,
      }
    })
    expect(fit.scrollWidth).toBeLessThanOrEqual(fit.clientWidth + 1)
    expect(fit.tabLeft).toBeGreaterThanOrEqual(0)
    expect(fit.tabRight).toBeLessThanOrEqual(fit.viewport)
    expect(fit.tabBottom).toBeLessThanOrEqual(fit.viewportHeight)
    const band = page.locator('.task-card', { hasText: 'Write the migration note' }).locator('.task-agent-band')
    expectOneLine((await band.locator('.task-agent-status').boundingBox())!, (await band.locator('.task-agent-action').boundingBox())!)
  })
})

test.describe('lanes at 900px', () => {
  test.use({ viewport: { width: 900, height: 900 } })

  test('the review band stays on one line', async ({ page }) => {
    await open(page, 'task-lanes-band')
    const band = page.locator('.task-card', { hasText: 'Write the migration note' }).locator('.task-agent-band')
    const status = await band.locator('.task-agent-status').boundingBox()
    const action = await band.locator('.task-agent-action').boundingBox()
    expectOneLine(status!, action!)
  })
})

test.describe('lanes on a phone', () => {
  const { defaultBrowserType: _ignored, ...iphone } = devices['iPhone 13']
  test.use(iphone)

  test('every lane is 310px wide and the band is one line', async ({ page }) => {
    await open(page, 'task-lanes-iphone')
    await expectFixedLanes(page)
    const band = page.locator('.task-card', { hasText: 'Write the migration note' }).locator('.task-agent-band')
    const status = await band.locator('.task-agent-status').boundingBox()
    const action = await band.locator('.task-agent-action').boundingBox()
    expectOneLine(status!, action!)
    // The coarse-pointer rules win over the base ones: both new controls are 44px.
    const tab = await toggle(page).boundingBox()
    expect(tab!.width).toBeGreaterThanOrEqual(44)
    await toggle(page).click()
    const hide = await lane(page, 'Done').getByRole('button', { name: 'Hide done tasks' }).boundingBox()
    expect(hide!.height).toBeGreaterThanOrEqual(44)
    expect(hide!.width).toBeGreaterThanOrEqual(44)
  })
})

test.describe('Done behind a tab', () => {
  test.use({ viewport: { width: 1440, height: 900 } })

  test('collapsed by default, expands, persists, hides again', async ({ page }) => {
    await open(page, 'task-lanes-done-toggle')
    await expect(lane(page, 'Done')).toHaveCount(0)
    await expect(toggle(page)).toBeVisible()
    await expect(toggle(page)).toHaveAttribute('aria-expanded', 'false')
    // The tab sits after the last lane, 44px wide, at least as tall as a lane head.
    const boxes = await page.evaluate(() => {
      const lanes = Array.from(document.querySelectorAll('.task-lanes--columns > .task-lane'))
      const tab = document.querySelector('.task-done-toggle')!.getBoundingClientRect()
      const last = lanes[lanes.length - 1]!.getBoundingClientRect()
      const head = document.querySelector('.task-lane-head')!.getBoundingClientRect()
      return { tabLeft: tab.left, lastRight: last.right, width: tab.width, height: tab.height, head: head.height }
    })
    expect(boxes.tabLeft).toBeGreaterThanOrEqual(boxes.lastRight)
    expect(Math.abs(boxes.width - 44)).toBeLessThanOrEqual(1)
    expect(boxes.height).toBeGreaterThanOrEqual(boxes.head)

    await toggle(page).click()
    await expect(lane(page, 'Done')).toBeVisible()
    await expect(page.locator('.task-lanes .task-done-toggle')).toHaveCount(0)
    // The click scrolls the lane into view and hands focus to Hide (the tab unmounted).
    await expect(lane(page, 'Done').getByRole('button', { name: 'Hide done tasks' })).toBeFocused()
    await expect.poll(() => page.evaluate(() => {
      const done = document.querySelector('[data-lane="done"]')!.getBoundingClientRect()
      const board = document.querySelector('.task-lanes')!.getBoundingClientRect()
      return done.left >= board.left - 1 && done.right <= board.right + 1
    })).toBe(true)

    await page.reload()
    await expect(page.locator('.task-card').first()).toBeVisible()
    await expect(lane(page, 'Done')).toBeVisible()

    await lane(page, 'Done').getByRole('button', { name: 'Hide done tasks' }).click()
    await expect(lane(page, 'Done')).toHaveCount(0)
    await expect(toggle(page)).toHaveAttribute('aria-expanded', 'false')
  })

  test('the Done status filter still lists the done task at full width', async ({ page }) => {
    await open(page, 'task-lanes-done-filter')
    await page.getByRole('group', { name: 'Filter by status' }).getByRole('button', { name: /^Done/ }).click()
    await expect(page.locator('.task-card', { hasText: 'Send the invoice' })).toBeVisible()
    const widths = await page.evaluate(() => {
      const list = document.querySelector('.task-lanes--list')!.getBoundingClientRect()
      const one = document.querySelector('.task-lanes--list > .task-lane')!.getBoundingClientRect()
      return { list: list.width, lane: one.width }
    })
    expect(widths.lane).toBeGreaterThan(widths.list - 20)
  })

  test('dropping a card on the collapsed tab opens the completion sheet', async ({ page }) => {
    await open(page, 'task-lanes-done-drop')
    // The card's own surface drag; HTML5 drag and drop is gone from the board.
    await dragBySurface(page, 'Plan the offsite', toggle(page))
    await expect(page.getByRole('dialog')).toBeVisible()
    const actions = page.locator('.task-resolution-sheet .form-actions button')
    await expect(actions).toHaveCount(2)
    await expect(actions).toHaveText(['Mark done', 'Cancel'])
    await expect(page.getByRole('button', { name: 'Mark done' })).toBeVisible()
  })

  test('completing from the editor with Done collapsed leaves focus on the Done tab', async ({ page }) => {
    await open(page, 'task-lanes-editor-focus')
    const detail = { ...TASKS[0], body: '', resolution: '', completions: [], linked_chats: [] }
    await page.route(/\/api\/tasks\/1{32}(?:\?.*)?$/, route => route.fulfill({ json: { workspace: 'alpha', task: detail } }))
    await page.route(/\/api\/tasks\/1{32}\/complete/, route => route.fulfill({ json: { workspace: 'alpha', task: {
      ...detail, status: 'done', revision: 'b'.repeat(64), completed_at: new Date().toISOString(),
    } } }))
    // The To do lane holds one card, so no neighbour is left to take focus.
    await page.locator('.task-card', { hasText: 'Plan the offsite' }).locator('.task-open').focus()
    await page.keyboard.press('Enter')
    await page.getByRole('radio', { name: 'Done', exact: true }).click()
    await expect(page.locator('.task-resolution-sheet .form-actions button')).toHaveText(['Mark done', 'Cancel'])
    await page.getByRole('button', { name: 'Mark done' }).click()
    await expect(page.locator('#task-detail-name')).toHaveCount(0)
    await expect(toggle(page)).toBeFocused()
  })
})
