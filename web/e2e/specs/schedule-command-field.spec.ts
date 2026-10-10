import { expect, test, type Page } from '@playwright/test'
import { boot, horizontalOverflow, isolate } from '../support/app'

const SHOTS = '/private/tmp/claude-501/-Users-raffaelefarinaro-repos-ciaobot/d6b99252-f939-4dd7-b1fb-0de2ae7b0ad8/scratchpad/i1258shots'
const HINT = 'Runs first. If it prints nothing and exits 0, no chat is opened.'
const PHONE = { width: 390, height: 844 }
// 200% page zoom is indistinguishable from halving the CSS viewport; see browser-zoom.spec.ts.
const ZOOMED = { width: 640, height: 450 }

/** Open the new-automation form; on a phone the sidebar is a drawer. */
async function openNewForm(page: Page, mobile = false) {
  await boot(page, '/schedules', 'text=Next up')
  if (mobile) await page.getByRole('button', { name: 'Open sidebar' }).click()
  await page.getByRole('button', { name: 'New automation' }).first().click()
  // The phone drawer does not close itself when New is chosen; close it so the
  // form is what the screenshot and the measurements see.
  if (mobile) await page.mouse.click(360, 600) // the scrim to the right of the drawer
  const field = page.getByLabel('Command (optional)')
  await field.scrollIntoViewIfNeeded()
  await expect(field).toBeVisible()
  return field
}

async function openEditCard(page: Page, patches: Array<Record<string, unknown>>) {
  await page.route('**/api/schedules/alpha-daily-brief', async (route) => {
    if (route.request().method() === 'PATCH') {
      patches.push(route.request().postDataJSON())
      await route.fulfill({ json: { schedule_id: 'alpha-daily-brief', title: 'Daily project briefing', prompt: 'p', daily_time_utc: '08:00', timezone_name: 'UTC', frequency: 'daily', enabled: true, workspace: 'alpha', web_project_id: 'alpha-general', command: 'x' } })
      return
    }
    await route.fallback()
  })
  await boot(page, '/schedules/alpha-daily-brief', 'text=Daily project briefing')
  await page.getByRole('button', { name: 'Edit', exact: true }).first().click()
  const field = page.getByLabel('Command (optional)')
  await field.scrollIntoViewIfNeeded()
  await expect(field).toBeVisible()
  return field
}

async function expectFocusRing(field: ReturnType<Page['getByLabel']>) {
  const ring = await field.evaluate((el) => {
    const s = getComputedStyle(el)
    return { matches: el.matches(':focus-visible'), outlineStyle: s.outlineStyle, outlineWidth: s.outlineWidth, boxShadow: s.boxShadow, border: s.borderColor }
  })
  console.log(`RING: ${JSON.stringify(ring)}`)
  expect(ring.matches).toBe(true)
  expect(ring.outlineStyle !== 'none' || ring.boxShadow !== 'none', JSON.stringify(ring)).toBe(true)
}

async function tabTo(page: Page, field: ReturnType<Page['getByLabel']>, from: string) {
  await page.locator(from).focus()
  let reached = false
  for (let i = 0; i < 4 && !reached; i++) {
    await page.keyboard.press('Tab')
    reached = await field.evaluate((el) => el === document.activeElement)
  }
  expect(reached, 'command field not reached by Tab').toBe(true)
}

test.describe('schedule command field', () => {
  test('new form: labelled, Tab reachable, focus ring, create body', async ({ page }, testInfo) => {
    await isolate(page, `schedcmd-n-${testInfo.workerIndex}`)
    await page.setViewportSize({ width: 1280, height: 900 })
    const posts: Array<Record<string, unknown>> = []
    await page.route('**/api/schedules', async (route) => {
      if (route.request().method() === 'POST') {
        posts.push(route.request().postDataJSON())
        await route.fulfill({ json: { schedule_id: 'new-one', title: 'n', prompt: 'p', daily_time_utc: '', timezone_name: 'UTC', frequency: 'manual', enabled: true, workspace: 'alpha' } })
        return
      }
      await route.fallback()
    })
    const field = await openNewForm(page)
    await expect(page.getByText(HINT)).toBeVisible()
    await expect(field).toHaveAttribute('aria-describedby', 'nsf-command-hint')

    await tabTo(page, field, '#nsf-prompt')
    await expectFocusRing(field)
    await page.screenshot({ path: `${SHOTS}/new-desktop-focused.png` })

    await page.locator('#nsf-prompt').fill('Check the docs')
    await field.fill('git -C docs pull')
    await page.locator('#nsf-frequency').selectOption('manual')
    await page.getByRole('button', { name: 'Create automation' }).click()
    await expect.poll(() => posts.length).toBeGreaterThan(0)
    expect(posts[0]).toHaveProperty('command', 'git -C docs pull')
  })

  test('edit card: labelled, Tab reachable, focus ring, patch body', async ({ page }, testInfo) => {
    await isolate(page, `schedcmd-e-${testInfo.workerIndex}`)
    await page.setViewportSize({ width: 1280, height: 900 })
    const patches: Array<Record<string, unknown>> = []
    const field = await openEditCard(page, patches)
    await expect(page.getByText(HINT)).toBeVisible()
    await expect(field).toHaveAttribute('aria-describedby', 'sp-command-hint')

    await tabTo(page, field, '.content-form textarea >> nth=1')
    await expectFocusRing(field)
    await page.screenshot({ path: `${SHOTS}/edit-desktop-focused.png` })

    await field.fill('./scripts/check.sh')
    await page.locator('.content-form .btn-primary').click()
    await expect.poll(() => patches.length).toBeGreaterThan(0)
    expect(patches[0]).toHaveProperty('command', './scripts/check.sh')
  })

  for (const surface of ['new', 'edit'] as const) {
    test(`${surface}: 390x844 touch target and no horizontal overflow`, async ({ browser }, testInfo) => {
      const context = await browser.newContext({ viewport: PHONE, hasTouch: true, isMobile: true })
      const page = await context.newPage()
      await isolate(page, `schedcmd-m-${surface}-${testInfo.workerIndex}`)
      const field = surface === 'new' ? await openNewForm(page, true) : await openEditCard(page, [])
      await field.fill('git -C docs pull && ./scripts/some-really-long-sync-script-name-without-breaks.sh --flag')
      const box = await field.boundingBox()
      const { overflow, culprits } = await horizontalOverflow(page)
      console.log(`MOBILE ${surface}: field ${box!.width}x${box!.height}, overflow ${overflow}`)
      await page.screenshot({ path: `${SHOTS}/${surface}-mobile.png` })
      expect(box!.height).toBeGreaterThanOrEqual(44)
      expect(overflow, `scrolls ${overflow}px; ${JSON.stringify(culprits)}`).toBeLessThanOrEqual(0)
      await context.close()
    })

    test(`${surface}: 200% zoom has no horizontal overflow`, async ({ page }, testInfo) => {
      await isolate(page, `schedcmd-z-${surface}-${testInfo.workerIndex}`)
      await page.setViewportSize({ width: 1280, height: 900 })
      const field = surface === 'new' ? await openNewForm(page) : await openEditCard(page, [])
      await page.setViewportSize(ZOOMED)
      await field.scrollIntoViewIfNeeded()
      await expect(field).toBeVisible()
      await field.fill('git -C docs pull && ./scripts/some-really-long-sync-script-name-without-breaks.sh --flag')
      const box = await field.boundingBox()
      const { overflow, culprits } = await horizontalOverflow(page)
      console.log(`ZOOM ${surface}: field ${box!.width}x${box!.height}, overflow ${overflow}`)
      await page.screenshot({ path: `${SHOTS}/${surface}-zoom200.png` })
      expect(overflow, `scrolls ${overflow}px; ${JSON.stringify(culprits)}`).toBeLessThanOrEqual(0)
    })
  }
})
