import { expect, test } from '@playwright/test'
import { boot, isolate } from '../support/app'

test('a project decision shows its permission and saves in that project', async ({ page }, info) => {
  await isolate(page, `project-decisions-${info.workerIndex}`)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.route('**/api/workspaces/alpha/share-evidence', route => route.fulfill({ json: {
    grants: [{ grant_id: 'g1', project_id: 'alpha-notes', type: 'standing', status: 'active', brain: { name: 'Team' }, audience: 'team', field_mask: ['decision'], producers: ['memory_extraction'] }],
  } }))
  let saved: unknown
  await page.route('**/api/workspaces/alpha/share-evidence/decisions', route => {
    saved = route.request().postDataJSON()
    return route.fulfill({ json: {} })
  })
  await boot(page, '/project/alpha-notes', '.project-view')
  const section = page.locator('.decision-entry')
  await section.getByRole('button', { name: 'Record decision' }).click()
  await expect(section).toContainText('Automatic sharing is not active yet')
  await expect(section).toContainText('Team (team): decision for memory_extraction')
  await section.getByLabel('Decision', { exact: true }).fill('Use the blue design')
  await section.getByLabel('Effective date').fill('2026-11-25')
  await section.getByRole('button', { name: 'Save decision' }).click()
  await expect(section.getByRole('status')).toHaveText('Decision recorded.')
  expect(saved).toEqual({ project_id: 'alpha-notes', decision: 'Use the blue design', reason: '', effective_date: '2026-11-25' })
  expect(await page.evaluate(() => document.scrollingElement!.scrollWidth <= window.innerWidth)).toBe(true)
  await section.screenshot({ path: info.outputPath('project-decision.png') })
})
