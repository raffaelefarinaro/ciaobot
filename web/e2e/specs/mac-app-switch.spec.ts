import { expect, test, type Page, type TestInfo } from '@playwright/test'
import { boot, fixtureWrites, horizontalOverflow, isolate, seedInbox } from '../support/app'

/**
 * Why this is a browser test and not a vitest one.
 *
 * The switch row's claims are about a real engine payload rendered on a real
 * screen: the title and one line the strip draws, a lead link big enough to
 * tap (44px) that survives keyboard Tab order, a ⋯ menu whose *Not now* really
 * fires the dismiss POST, a phone where the controls drop under the text
 * without sideways scroll, and dark scheme and 200% page zoom. jsdom has no
 * layout, no focus ring and no container query, so it can say none of that.
 *
 * Eligibility is the engine's (see `_detect_mac_app_switch` in
 * `ciao/operator_actions.py`); the fixture here serves what an installer-mode
 * engine on an Apple-silicon Mac would answer.
 */

test.use({ viewport: { width: 1280, height: 800 } })

const DMG = 'https://github.com/raffaelefarinaro/ciaobot/releases/latest/download/Ciaobot.dmg'

/** What `GET /api/housekeeping` answers for an installer engine on arm64 macOS. */
const switchTile = () => ({
  id: 'mac-app-switch',
  kind: 'mac-app-switch',
  severity: 35,
  title: 'Switch to the new Ciaobot app',
  detail:
    'Your workspaces, chats and settings are kept. Install Ciaobot for Mac on '
    + 'the computer running Ciaobot and it takes over from this install.',
  glyph: '▲',
  workspace: '',
  run_label: '',
  chat_label: '',
  chat_prompt: '',
  view_label: '',
  view_route: '',
  link_label: 'Download for Mac',
  link_url: DMG,
  dismiss_label: 'Not now',
  primary: '',
  blocking: false,
  terminal_action: '',
  command: '',
})

const rowOf = (page: Page) => page.locator('.needs-you-row')

/** Seed the tile, then reload so the strip draws it. */
async function bootWithTile(page: Page, ready = '.needs-you-row') {
  await boot(page, '/')
  await seedInbox(page, { actions: [switchTile()] })
  await boot(page, '/', ready)
}

async function attachShot(ti: TestInfo, name: string, page: Page) {
  await ti.attach(name, { body: await page.screenshot({ fullPage: true }), contentType: 'image/png' })
}

test.describe('mac app switch row', () => {
  test('shows the switch row with a download link and Not now', async ({ page }, ti) => {
    await isolate(page, 'mac-switch-1')
    await bootWithTile(page)
    await attachShot(ti, 'mac-switch-rendered', page)

    const switchRow = rowOf(page)
    await expect(switchRow.locator('.needs-you-title-text')).toHaveText('Switch to the new Ciaobot app')
    // The row renders the detail's first line; the reassurance leads it.
    await expect(switchRow.locator('.needs-you-line')).toHaveText(
      /^Your workspaces, chats and settings are kept\./,
    )

    const link = switchRow.getByRole('link', { name: 'Download for Mac' })
    await expect(link).toHaveAttribute('href', DMG)
    await expect(link).toHaveAttribute('target', '_blank')
    const box = await link.boundingBox()
    expect(box, 'the lead link is a real tap target').not.toBeNull()
    expect(box!.height).toBeGreaterThanOrEqual(44)

    // Keyboard: Tab reaches the link and its focus ring is visible.
    let onLink = false
    for (let i = 0; i < 60; i += 1) {
      await page.keyboard.press('Tab')
      if (await link.evaluate((el) => el === document.activeElement)) { onLink = true; break }
    }
    expect(onLink, 'Tab reaches the Download for Mac link').toBe(true)
    await expect(link).toBeFocused()
    const outlineStyle = await link.evaluate((el) => getComputedStyle(el).outlineStyle)
    expect(outlineStyle, 'focused link shows a visible focus ring').not.toBe('none')

    // One more Tab is the row's ⋯ trigger; the menu opens with Enter, which
    // focuses its first item — Not now. The arrows rotate the items and come
    // back, then Enter activates.
    await page.keyboard.press('Tab')
    await expect(switchRow.locator('.needs-you-more')).toBeFocused()
    await page.keyboard.press('Enter')
    const notNow = page.getByRole('menuitem', { name: 'Not now' })
    await expect(notNow).toBeFocused()
    await page.keyboard.press('ArrowUp')
    await expect(page.getByRole('menuitem', { name: 'Hide for now' })).toBeFocused()
    await page.keyboard.press('ArrowDown')
    await expect(notNow).toBeFocused()
    await page.keyboard.press('Enter')

    // The dismissal is the row's own POST, and the strip redraws without it.
    await expect(switchRow).toBeHidden()
    const dismissal = (await fixtureWrites(page)).filter(
      (w) => w.method === 'POST' && w.path === '/api/housekeeping/mac-app-switch/dismiss',
    )
    expect(dismissal.length).toBeGreaterThan(0)
    await attachShot(ti, 'mac-switch-dismissed', page)
  })

  test.describe('phone', () => {
    test.use({ viewport: { width: 390, height: 844 }, hasTouch: true })

    test('fits a phone without sideways scroll', async ({ page }, ti) => {
      await isolate(page, 'mac-switch-phone')
      await bootWithTile(page)
      const { overflow, culprits } = await horizontalOverflow(page)
      expect(
        overflow,
        `the row scrolls ${overflow}px sideways; widest unclipped: ${JSON.stringify(culprits)}`,
      ).toBeLessThanOrEqual(0)

      // Under the 520px list width the controls drop under the text.
      const layout = await rowOf(page).evaluate((el) => {
        const line = el.querySelector('.needs-you-line')!.getBoundingClientRect()
        const primary = el.querySelector('.needs-you-primary')!.getBoundingClientRect()
        return { lineBottom: line.bottom, primaryTop: primary.top, primaryWidth: primary.width }
      })
      expect(
        layout.primaryTop,
        `controls must sit below the text (line bottom ${layout.lineBottom})`,
      ).toBeGreaterThanOrEqual(layout.lineBottom - 1)
      await attachShot(ti, 'mac-switch-phone', page)
    })
  })

  test('keeps the row readable in dark scheme and at 200% zoom', async ({ page }, ti) => {
    await page.emulateMedia({ colorScheme: 'dark' })
    await isolate(page, 'mac-switch-dark')
    await bootWithTile(page)
    await attachShot(ti, 'mac-switch-dark', page)

    // Chromium's page zoom is indistinguishable from halving the CSS viewport
    // (the pattern browser-zoom.spec.ts uses): at 200% a 1280px window is 640
    // CSS px, so the row must reflow, not merely have been born small.
    await page.setViewportSize({ width: 640, height: 400 })
    await expect(rowOf(page)).toBeVisible()
    const { overflow } = await horizontalOverflow(page)
    expect(overflow, 'the dark scheme row holds at 200% zoom').toBeLessThanOrEqual(0)
    await attachShot(ti, 'mac-switch-dark-zoomed', page)
  })
})
