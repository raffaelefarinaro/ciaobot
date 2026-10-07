import { expect, test } from '@playwright/test'
import { boot, COMPOSER, isolate } from '../support/app'

for (const mobile of [false, true]) {
  test.describe(mobile ? 'Work details drawer' : 'Work details window', () => {
    test.use({ viewport: mobile ? { width: 390, height: 844 } : { width: 1440, height: 900 }, hasTouch: mobile })

    test('shows readable context and supports closing and reopening', async ({ page }, testInfo) => {
      await isolate(page, `work-details-${mobile}`)
      await page.route('**/api/projects', async route => {
        const response = await route.fetch()
        const projects = await response.json()
        await route.fulfill({ response, json: projects.map((project: Record<string, unknown>) => ({
          ...project,
          context: 'Synthetic project planning. Hub doc: planning.md',
          vault_doc_path: 'alpha/memory-vault/projects/planning.md',
        })) })
      })
      await boot(page, '/chat/alpha-chat-2', COMPOSER)
      const surface = page.locator(mobile ? '#chat-work-inspector' : '#chat-work-rail')
      if (mobile) await page.locator('.work-inspector-trigger').click()
      await expect(surface).toBeVisible()
      await expect(surface.locator('.agent-context-description')).toContainText('Synthetic')
      await expect(surface.locator('pre')).toHaveCount(0)
      await expect(surface).not.toContainText('project_context=')
      const documentLink = surface.locator('.agent-context-description a.file-link')
      await expect(documentLink).toHaveText('planning.md')
      await expect(documentLink).toHaveAttribute('data-file-path', 'alpha/memory-vault/projects/planning.md')
      await expect(surface.locator('.agent-context-doc')).toHaveCount(0)
      await documentLink.focus()
      await expect(documentLink).toBeFocused()
      const close = surface.getByRole('button', { name: mobile ? 'Close work details' : 'Hide work details' })
      await expect(close.locator('svg path')).toHaveAttribute('d', 'm6 6 12 12M18 6 6 18')
      const size = await close.boundingBox()
      if (mobile) {
        expect(size!.width).toBeGreaterThanOrEqual(44)
        expect(size!.height).toBeGreaterThanOrEqual(44)
      }
      await page.locator('html').evaluate(el => el.classList.remove('theme-light'))
      await page.screenshot({ path: testInfo.outputPath('dark.png'), animations: 'disabled' })
      await page.locator('html').evaluate(el => el.classList.add('theme-light'))
      await page.screenshot({ path: testInfo.outputPath('light.png'), animations: 'disabled' })
      await close.click()
      await expect(surface).toHaveCount(0)
      const opener = page.locator('.work-inspector-trigger')
      await expect(opener).toBeFocused()
      await opener.press('Enter')
      await expect(surface).toBeVisible()
      await expect(close).toBeFocused()
      const overflow = await surface.evaluate(el => el.scrollWidth - el.clientWidth)
      expect(overflow).toBe(0)
      await page.locator('html').evaluate(el => el.style.setProperty('--font-scale', '1.5'))
      await expect(close).toBeVisible()
      expect(await surface.evaluate(el => el.scrollWidth - el.clientWidth)).toBe(0)
    })
  })
}
