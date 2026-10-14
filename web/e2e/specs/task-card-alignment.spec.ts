import { devices, expect, test, type Page } from '@playwright/test'
import { boot, isolate } from '../support/app'

/**
 * Everything under a task card's title starts at the title's text column, and
 * the agent band's dot sits under the Done ring. jsdom has no layout, so the
 * boxes are measured in a real engine, on a fine and on a coarse pointer (the
 * checkbox is 28px there and 44px here; both must net the same 20px).
 */
const NOW = new Date()
const TODAY = `${NOW.getFullYear()}-${String(NOW.getMonth() + 1).padStart(2, '0')}-${String(NOW.getDate()).padStart(2, '0')}`

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

const ALIGNMENT_TASKS = [
  // (a) a due date: the meta line.
  { ...BASE, id: '1'.repeat(32), title: 'Plan the offsite', status: 'backlog', due: TODAY, relative_path: 'Workspace/Tasks/a.md' },
  // (b) the agent needs the user: note and band.
  {
    ...BASE, id: '2'.repeat(32), title: 'Draft the quarterly summary', status: 'in_progress', assignee: 'agent',
    attempt_id: 'att-needs', chat_id: 'chat-needs', attempt_state: 'needs_you', live_attempt_id: 'att-needs',
    attempt_summary: 'I got as far as the second section and need a decision on scope.',
    relative_path: 'Workspace/Tasks/b.md',
  },
  // (c) ready for review: dashed ring and band.
  {
    ...BASE, id: '3'.repeat(32), title: 'Write the migration note', status: 'in_review', assignee: 'agent',
    attempt_id: 'att-review', chat_id: 'chat-review', attempt_state: 'ready_for_review', live_attempt_id: 'att-review',
    relative_path: 'Workspace/Tasks/c.md',
  },
  // (d) done today by the agent: "Done by agent" meta.
  {
    ...BASE, id: '4'.repeat(32), title: 'Send the invoice', status: 'done', assignee: 'agent',
    attempt_id: 'att-done', chat_id: 'chat-done', attempt_state: 'ready_for_review', attempt_outcome: 'done',
    completed_at: NOW.toISOString(), relative_path: 'Workspace/Tasks/d.md',
  },
  // (e) a failed attempt whose chat was archived: the caption.
  {
    ...BASE, id: '5'.repeat(32), title: 'Reconcile the ledger', status: 'in_progress', assignee: 'agent',
    attempt_id: 'att-failed', chat_id: 'chat-gone', chat_archived: true, attempt_state: 'failed',
    relative_path: 'Workspace/Tasks/e.md',
  },
]

async function mockTasks(page: Page) {
  // Done is collapsed by default; the done card is one of the shapes measured.
  await page.addInitScript(() => localStorage.setItem('ciao.tasks.doneShown', '1'))
  await page.route(/\/api\/tasks(?:\?|$)/, route => route.fulfill({
    json: { workspace: 'alpha', tasks: ALIGNMENT_TASKS },
  }))
}

interface Measured {
  title: string
  titleLeft: number
  meta: number | null
  note: number | null
  changed: number | null
  status: number | null
  dotCenter: number | null
  ringCenter: number
  checkBox: { width: number; height: number }
  checkCoarse: boolean
}

function measureCards(page: Page): Promise<Measured[]> {
  return page.evaluate(() => {
    const firstText = (root: Element): Text | null => {
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
      for (let n = walker.nextNode(); n; n = walker.nextNode()) {
        if ((n.textContent ?? '').trim()) return n as Text
      }
      return null
    }
    const textLeft = (root: Element | null): number | null => {
      const text = root && firstText(root)
      if (!text) return null
      const range = document.createRange()
      range.selectNodeContents(text)
      return range.getClientRects()[0]!.left
    }
    return Array.from(document.querySelectorAll('.task-card')).map(card => {
      const check = card.querySelector('.task-check')!
      const checkRect = check.getBoundingClientRect()
      const ring = check.querySelector('svg')!.getBoundingClientRect()
      const meta = card.querySelector('.task-meta')
      const dot = card.querySelector('.task-agent-dot')
      const dotRect = dot?.getBoundingClientRect()
      return {
        title: card.querySelector('.task-title')!.textContent!.trim(),
        titleLeft: textLeft(card.querySelector('.task-title'))!,
        meta: meta ? meta.firstElementChild!.getBoundingClientRect().left : null,
        note: card.querySelector('.task-agent-note')?.getBoundingClientRect().left ?? null,
        changed: textLeft(card.querySelector('.task-changed')) === null
          ? null
          : card.querySelector('.task-changed')!.getBoundingClientRect().left,
        status: textLeft(card.querySelector('.task-agent-status')),
        dotCenter: dotRect ? dotRect.left + dotRect.width / 2 : null,
        ringCenter: ring.left + ring.width / 2,
        checkBox: { width: checkRect.width, height: checkRect.height },
        checkCoarse: matchMedia('(pointer: coarse)').matches,
      }
    })
  })
}

async function expectAligned(page: Page, checkSize: number) {
  await mockTasks(page)
  await boot(page, '/tasks', '.task-card')
  await expect(page.locator('.task-card')).toHaveCount(ALIGNMENT_TASKS.length)
  const cards = await measureCards(page)
  const seen = { meta: 0, note: 0, changed: 0, status: 0, dot: 0 }
  for (const card of cards) {
    // The shared 20px net width is an assumption about both pointer kinds: prove it.
    expect(card.checkBox.width, `${card.title} check width`).toBe(checkSize)
    expect(card.checkBox.height, `${card.title} check height`).toBe(checkSize)
    for (const kind of ['meta', 'note', 'changed', 'status'] as const) {
      const left = card[kind]
      if (left === null) continue
      seen[kind]++
      expect.soft(Math.abs(left - card.titleLeft), `${card.title}: ${kind} left edge vs title text`).toBeLessThanOrEqual(1)
    }
    if (card.dotCenter !== null) {
      seen.dot++
      expect.soft(Math.abs(card.dotCenter - card.ringCenter), `${card.title}: band dot vs ring centre`).toBeLessThanOrEqual(1)
    }
  }
  // Every kind of block was measured, so the test cannot pass vacuously.
  expect(seen.meta).toBeGreaterThanOrEqual(2)
  expect(seen.note).toBeGreaterThanOrEqual(1)
  expect(seen.changed).toBeGreaterThanOrEqual(1)
  expect(seen.status).toBeGreaterThanOrEqual(3)
  expect(seen.dot).toBeGreaterThanOrEqual(3)
  return cards
}

test.describe('task card alignment, fine pointer', () => {
  test.use({ viewport: { width: 1280, height: 900 } })

  test('the text under the title hangs from the title column', async ({ page }) => {
    await isolate(page, 'task-card-alignment-desktop')
    const cards = await expectAligned(page, 28)
    expect(cards.every(card => !card.checkCoarse)).toBe(true)
  })
})

test.describe('task card alignment, coarse pointer', () => {
  const { defaultBrowserType: _ignored, ...iphone } = devices['iPhone 13']
  test.use(iphone)

  test('the text under the title hangs from the title column', async ({ page }) => {
    await isolate(page, 'task-card-alignment-iphone')
    const cards = await expectAligned(page, 44)
    expect(cards.every(card => card.checkCoarse)).toBe(true)
  })
})
