import { expect, test } from '@playwright/test'
import { activeWorkspaceButton, boot, horizontalOverflow, isolate, settled } from '../support/app'

test.describe.configure({ mode: 'serial' })

const task = {
  id: 'learnings-cleanup', revision: 1, scope: 'workspace',
  title: 'Review old learnings', why: 'Some old learnings need review.',
  since_version: '2.1.0', status: 'offered', applicability: 'applicable',
  applicability_checked_at: '2026-11-12T10:00:00Z', offered: true, suppressed: false,
  chat_id: '', chat_live: false, prompt_digest: '', attempted_fingerprint: '', updated_at: '',
}
const alphaSummary = '2 active, 1 removals, 1 kept, 0 conflicts. Review required.'
const betaSummary = '4 active, 2 removals, 2 kept, 0 conflicts. Review required.'

for (const scenario of ['desktop', 'phone', 'zoom'] as const) {
  test(`home prepares maintenance without a model or write: ${scenario}`, async ({ browser }, testInfo) => {
    const phone = scenario === 'phone'
    const context = await browser.newContext({
      viewport: phone ? { width: 390, height: 844 } : { width: 1280, height: 900 },
      hasTouch: phone, isMobile: phone,
    })
    const page = await context.newPage()
    await isolate(page, `update-preview-${scenario}-${testInfo.workerIndex}`)
    const writes: string[] = []
    const previewRequests: string[] = []
    page.on('request', request => {
      const url = new URL(request.url())
      if (url.pathname.startsWith('/api/') && request.method() !== 'GET') writes.push(`${request.method()} ${url.pathname}`)
    })
    let releaseFirst!: () => void
    const firstPending = new Promise<void>(resolve => { releaseFirst = resolve })
    let releaseStale!: () => void
    const stalePending = new Promise<void>(resolve => { releaseStale = resolve })
    let staleDelivered!: () => void
    const staleDone = new Promise<void>(resolve => { staleDelivered = resolve })
    let released = false
    await page.route('**/api/settings/appearance', route => route.fulfill({ json: {
      text_contrast: 'normal', body_font: 'system', theme: 'dark', font_scale: 1.15, custom_colors: {}, revision: 'a0',
    } }))
    await page.route('**/api/update-tasks?*', route => route.fulfill({ json: { tasks: [task] } }))
    await page.route('**/api/update-tasks/learnings-cleanup/preview?*', async route => {
      const url = new URL(route.request().url())
      previewRequests.push(url.search)
      const holdStale = released && scenario === 'desktop' && url.searchParams.get('workspace') === 'alpha' && url.searchParams.get('fresh') === '1'
      if (!released) await firstPending
      const workspace = url.searchParams.get('workspace') || ''
      if (holdStale) await stalePending
      await route.fulfill({ json: {
        task_id: task.id, revision: task.revision, workspace, state: 'review',
        summary: workspace === 'alpha' ? alphaSummary : betaSummary,
        checked_at: '2026-11-12T10:00:00Z', plan: { note: 'PRIVATE_PLAN_SENTINEL' },
      } })
      if (holdStale) staleDelivered()
    })

    await boot(page, '/', '.home-intake-form textarea')
    const row = page.locator('.needs-you-row').filter({ hasText: task.title })
    await expect(row.locator('.needs-you-line')).toHaveText('Preparing a preview…')
    const composer = page.locator('.home-intake-form textarea')
    await composer.fill('A draft while preparation is pending')
    await expect(composer).toHaveValue('A draft while preparation is pending')
    expect(writes).toEqual([])
    released = true
    releaseFirst()
    await expect(row.locator('.needs-you-line')).toHaveText(`Preview: ${alphaSummary}`)
    await expect(row).not.toContainText('PRIVATE_PLAN_SENTINEL')
    await expect(row.getByRole('button', { name: 'Start in chat' })).toBeEnabled()
    if (scenario === 'zoom') await page.evaluate(() => { document.documentElement.style.zoom = '2' })
    const more = row.getByRole('button', { name: `More actions for ${task.title}` })
    await more.focus()
    const beforeRefresh = previewRequests.length
    await page.keyboard.press('Enter')
    const refresh = row.getByRole('menuitem', { name: /^Refresh preview \(checked / })
    await expect(refresh).toBeFocused()
    await settled(row)
    await page.screenshot({ path: testInfo.outputPath(`${scenario}-menu.png`) })
    if (phone) {
      for (const control of [more, row.getByRole('button', { name: 'Start in chat' }), refresh]) {
        const box = await control.boundingBox()
        expect(box).not.toBeNull()
        expect(box!.width).toBeGreaterThanOrEqual(44)
        expect(box!.height).toBeGreaterThanOrEqual(44)
      }
    }
    expect((await horizontalOverflow(page)).overflow).toBeLessThanOrEqual(0)
    await page.keyboard.press('Enter')
    await expect(more).toBeFocused()
    await expect.poll(() => previewRequests.length).toBe(beforeRefresh + 1)
    expect(new URLSearchParams(previewRequests.at(-1)).get('fresh')).toBe('1')
    expect(writes).toEqual([])

    if (scenario === 'desktop') {
      await expect(row.locator('.needs-you-line')).toHaveText('Preparing a preview…')
      await activeWorkspaceButton(page).click()
      await page.locator('#workspace-scope-menu [role="menuitem"]').filter({ hasText: 'beta' }).click()
      await expect(row.locator('.needs-you-line')).toHaveText(`Preview: ${betaSummary}`)
      releaseStale()
      await staleDone
      await expect(row.locator('.needs-you-line')).toHaveText(`Preview: ${betaSummary}`)
      await expect(page.locator('body')).not.toContainText(alphaSummary)
      expect(writes).toEqual([])
    } else {
      await expect(row.locator('.needs-you-line')).toHaveText(`Preview: ${alphaSummary}`)
    }
    await context.close()
  })
}
