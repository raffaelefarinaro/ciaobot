import { expect, test } from '@playwright/test'
import { boot, isolate } from '../support/app'

test.use({ viewport: { width: 390, height: 844 }, hasTouch: true })

test('keyboard redraft opens the new private review on a narrow screen', async ({ page }) => {
  await isolate(page, 'operator-redraft')
  let id = 'synthetic-old'
  const writes: string[] = []
  await page.route('**/api/proposals**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    const row = { id, workspace: 'alpha', kind: 'share', text: 'Synthetic decision', source: '', path: 'alpha/Workspace/Memory-Proposals.md', line: 3 }
    if (request.method() === 'POST') {
      writes.push(path)
      expect(request.postDataJSON()).toEqual({ workspace: 'alpha', expected_revision: 'synthetic-old-token' })
      id = 'synthetic-new'
      return route.fulfill({ json: { replacement: { id, revision: id + '-token' } } })
    }
    if (path.endsWith('/preview')) {
      return route.fulfill({ json: { ok: true, preview: { ...row, revision: id + '-token', operation: 'create', before: null, after: id, path: 'decisions/note.md', brain: 'Team', blocked_reason: 'draft_unavailable', redraft_available: true } } })
    }
    return route.fulfill({ json: { rows: path === '/api/proposals' ? [row] : [], total: 1 } })
  })
  await boot(page, '/memory/review?show=suggested', '.pr-row-action')
  await page.locator('.pr-row-action').first().click()
  const button = page.getByRole('button', { name: 'Redraft for review' })
  await expect(button).toBeVisible()
  expect((await button.boundingBox())!.height).toBeGreaterThanOrEqual(44)
  await button.focus()
  await page.keyboard.press('Enter')
  await expect(page.locator('.pr-card')).toContainText('synthetic-new')
  await expect(page.getByRole('button', { name: 'Publish', exact: true })).toHaveCount(0)
  expect(writes).toEqual(['/api/proposals/synthetic-old/redraft'])
})
