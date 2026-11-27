import { expect, test, type Page } from '@playwright/test'
import { boot, horizontalOverflow, isolate } from '../support/app'
import { CARE_PROPOSAL_PATH, careReports } from '../../src/lib/careReportFixture'
import type { CareReport } from '../../src/lib/careReport'

/**
 * Reviewing a proposal the care run wrote: the Review proposal button opens it
 * in place, Reject writes `status: rejected` through the file viewer's save
 * path and nothing else, Approve opens a new chat with a draft and writes
 * nothing. The care read and the file are answered here.
 */
const VIEWER_PATH = `alpha/Workspace/Proposals/guide-trim-duplicates.md`

const PROPOSAL = [
  '---',
  'type: guide-edit',
  'target: AGENTS.md',
  'title: Trim the guide',
  'summary: Removes an outdated status section and a repeated folder path from the guide.',
  'rationale: |',
  '  The status section names a synthetic tool nobody uses now.',
  '  The chat-logs folder is written twice, once with an old name.',
  'evidence:',
  '  - Workspace/Care-Report.md',
  '  - https://example.com/synthetic/issue/1',
  'proposed_change: Delete the "Tool status" section and the second chat-logs line.',
  'created: 2026-11-26',
  'status: draft',
  '---',
  '',
  'Both lines are loaded into every chat.',
  '',
].join('\n')

function withViewerPaths(report: CareReport): CareReport {
  return {
    ...report,
    workspace: 'alpha',
    report_path: 'alpha/Workspace/Care-Report.md',
    unsure_path: 'alpha/Workspace/Unsure.md',
    needs_you: report.needs_you.map(item => ({ ...item, open_path: item.open_path ? `alpha/${item.path}` : '' })),
  }
}

async function answer(page: Page, status = 'draft') {
  let current = status
  const saves: { path: string; content: string }[] = []
  await page.route(/\/api\/care-report\?/, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(withViewerPaths({
      ...careReports.needsYou,
      needs_you: careReports.needsYou.needs_you.map(item => (item.path === CARE_PROPOSAL_PATH ? { ...item, proposal_status: current } : item)),
    })),
  }))
  await page.route(/\/api\/workspace-file(\?|$)/, async route => {
    const request = route.request()
    if (request.method() === 'POST') {
      const body = request.postDataJSON() as { path: string; content: string }
      saves.push(body)
      current = /^status: (\w+)$/m.exec(body.content)?.[1] ?? current
      await route.fulfill({ status: 200, contentType: 'application/json', body: '{"ok":true,"snapshot":null}' })
      return
    }
    const url = new URL(request.url())
    expect(url.searchParams.get('path')).toBe(VIEWER_PATH)
    await route.fulfill({ status: 200, contentType: 'text/markdown', body: PROPOSAL.replace('status: draft', `status: ${current}`) })
  })
  return saves
}

test.describe('Proposal review', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `proposal-${testInfo.workerIndex}-${testInfo.title}`)
    await page.setViewportSize({ width: 1280, height: 900 })
  })

  test('Review proposal opens the file in place, and Reject writes only its status', async ({ page }) => {
    const saves = await answer(page)
    await boot(page, '/memory/care', '.mc-need')
    await page.getByRole('button', { name: /^Review proposal: / }).click()
    await expect(page).toHaveURL(/\/memory\/care\?proposal=Workspace(%2F|\/)Proposals(%2F|\/)guide-trim-duplicates\.md$/)
    await expect(page.getByRole('heading', { name: 'Trim the guide' })).toBeVisible()
    await expect(page.getByText('The change is made in a chat, with you.')).toBeVisible()
    await expect(page.getByRole('heading', { name: 'What it changes' })).toBeVisible()
    await page.screenshot({ path: 'test-results/care-look/proposal-review-1280.png', fullPage: true })

    const reject = page.getByRole('button', { name: 'Reject' })
    await reject.focus()
    await page.keyboard.press('Enter')
    await expect(page.getByText('This proposal was rejected. Nothing else was changed.')).toBeVisible()
    expect(saves).toHaveLength(1)
    expect(saves[0].path).toBe(VIEWER_PATH)
    expect(saves[0].content).toBe(PROPOSAL.replace('status: draft', 'status: rejected'))

    // Back on the report, the item is gone: its file now says rejected.
    await page.getByRole('button', { name: 'Workspace care' }).click()
    await expect(page.locator('.mc-need')).toHaveCount(3)
    await expect(page.getByRole('button', { name: /^Review proposal: / })).toHaveCount(0)
  })

  test('Approve starts a new chat in General and writes nothing itself', async ({ page }) => {
    const saves = await answer(page)
    // The fixture's General project, marked as the workspace's own.
    await page.route(/\/api\/projects$/, async route => {
      const response = await route.fetch()
      const projects = (await response.json()) as { name: string; workspace: string }[]
      await route.fulfill({ response, json: projects.map(p => (p.name === 'General' ? { ...p, is_auto: true } : p)) })
    })
    // The chat create is where the flow leaves this page; the draft it carries
    // is pinned by the component test. Refusing it here keeps the fixture still.
    const creates: unknown[] = []
    await page.route(/\/api\/projects\/alpha-general\/chats$/, async route => {
      creates.push(route.request().postDataJSON())
      await route.fulfill({ status: 503, contentType: 'application/json', body: '{"error":"fixture"}' })
    })
    await boot(page, `/memory/care?proposal=${CARE_PROPOSAL_PATH}`, 'text=Trim the guide')
    await page.getByRole('button', { name: 'Approve' }).click()
    await expect.poll(() => creates).toEqual([{ title: 'Approve: Trim the guide' }])
    expect(saves).toEqual([])
  })

  test('an applied proposal is no longer an item, and its review says so', async ({ page }) => {
    await answer(page, 'applied')
    await boot(page, '/memory/care', '.mc-need')
    await expect(page.locator('.mc-need')).toHaveCount(3)
    await page.goto(`/memory/care?proposal=${CARE_PROPOSAL_PATH}`)
    await expect(page.getByText('This proposal was approved and applied.')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Approve' })).toHaveCount(0)
  })
})

test.describe('Proposal review on a phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

  for (const colorScheme of ['light', 'dark'] as const) {
    test(`reads without sideways scroll and keeps 44px controls (${colorScheme})`, async ({ page }, testInfo) => {
      await isolate(page, `proposal-phone-${testInfo.workerIndex}-${colorScheme}`)
      await page.emulateMedia({ colorScheme })
      await answer(page)
      await boot(page, `/memory/care?proposal=${CARE_PROPOSAL_PATH}`, 'text=Trim the guide')
      const { overflow, culprits } = await horizontalOverflow(page)
      expect(overflow, JSON.stringify(culprits)).toBeLessThanOrEqual(0)
      for (const name of ['Approve', 'Reject']) {
        expect((await page.getByRole('button', { name }).boundingBox())!.height).toBeGreaterThanOrEqual(44)
      }
      await page.screenshot({ path: `test-results/care-look/proposal-review-${colorScheme}-390.png`, fullPage: true })
    })
  }
})
