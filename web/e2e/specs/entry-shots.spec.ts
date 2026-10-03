import { test } from '@playwright/test'
import { boot, isolate } from '../support/app'

/**
 * The screenshots `plans/issue-785/browser-check.md` refers to.
 *
 * Not assertions — the spec next door is where those live. This exists because a
 * layout claim ("the block reflows", "the map counts the two separately") is a
 * claim about what a person sees, and a person has to be able to look at it.
 * Run with `npx playwright test e2e/specs/entry-shots.spec.ts`.
 */

const REVIEW = '/memory/review?show=revisit'
const MAP = '/memory/map'

async function withVerification(page: import('@playwright/test').Page) {
  await page.evaluate(() => fetch('/__fixture__/verification', { method: 'POST' }))
  await page.reload()
}

function nadiaRow(page: import('@playwright/test').Page) {
  return page.locator('.vr-row').filter({ has: page.locator('.vr-title', { hasText: /Nadia/ }) })
}

test.describe.configure({ mode: 'serial' })

test('the mixed-entry review row, at 1280px', async ({ page }) => {
  await isolate(page, 'shots-row')
  await page.setViewportSize({ width: 1280, height: 1000 })
  await boot(page, REVIEW, '.vault-review')
  await withVerification(page)
  await nadiaRow(page).locator('.vr-entries').scrollIntoViewIfNeeded()
  await nadiaRow(page).screenshot({ path: 'test-results/shot-review-row.png' })
})

test('the mixed-entry review row at 390px', async ({ page }) => {
  await isolate(page, 'shots-phone')
  await page.setViewportSize({ width: 390, height: 1400 })
  await boot(page, REVIEW, '.vault-review')
  await withVerification(page)
  await nadiaRow(page).locator('.vr-entries').scrollIntoViewIfNeeded()
  await nadiaRow(page).screenshot({ path: 'test-results/shot-review-row-390.png' })
})

test('the map node carrying entry coverage', async ({ page }) => {
  await isolate(page, 'shots-map')
  await page.setViewportSize({ width: 1280, height: 1000 })
  await boot(page, MAP, '.mm-toolbar')
  await withVerification(page)
  await page.getByRole('button', { name: 'List', exact: true }).click()
  await page.screenshot({ path: 'test-results/shot-map-toolbar.png' })
  await page.getByRole('button', { name: 'Open Nadia' }).click()
  await page.getByRole('dialog', { name: 'Details for Nadia' })
    .screenshot({ path: 'test-results/shot-map-tile.png' })
})

test('the entry scope in History', async ({ page }) => {
  await isolate(page, 'shots-history')
  await page.setViewportSize({ width: 1280, height: 1000 })
  await boot(page, '/memory/history', '.ph-list')
  await withVerification(page)
  const row = page.locator('.ph-row').filter({ hasText: 'People/Nadia.md' }).first()
  await row.locator('.ph-verify-summary').click()
  await page.screenshot({ path: 'test-results/shot-history.png' })
})
