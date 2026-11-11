import { expect, test } from '@playwright/test'
import { boot, isolate } from '../support/app'

test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

test('Home menu follows a safe-area inset that settles after startup', async ({ page }) => {
  await isolate(page, 'safe-area-header')
  const session = await page.context().newCDPSession(page)
  await session.send('Emulation.setSafeAreaInsetsOverride', { insets: { top: 0 } })
  await boot(page)

  // iOS may initially report zero, then expose the status-bar inset without
  // rotating the device. The header must follow CSS env(), not a cached pixel.
  await session.send('Emulation.setSafeAreaInsetsOverride', { insets: { top: 59 } })
  const menu = page.getByRole('button', { name: 'Open sidebar', exact: true })
  await expect.poll(async () => (await menu.boundingBox())?.y ?? -1).toBeGreaterThanOrEqual(59)
  await expect(page.locator('.header-trail .notif-bell')).toBeInViewport()
  await menu.click()
  await expect(page.locator('.chat-layout')).toHaveClass(/sidebar-open/)
})

for (const [theme, header] of [['dark', 'rgb(26, 26, 46)'], ['light', 'rgb(244, 244, 250)']] as const) {
  test(`installed phone app keeps a ${theme} header-coloured fixed strip on top for the iOS 26 status-bar tint`, async ({ page }) => {
    await isolate(page, `safe-area-status-tint-${theme}`)
    await page.addInitScript((t) => localStorage.setItem('ciao-theme', t), theme)
    const session = await page.context().newCDPSession(page)
    await session.send('Emulation.setSafeAreaInsetsOverride', { insets: { top: 59 } })
    await boot(page)

    // Chromium cannot emulate `display-mode: standalone`, so lift the app's own
    // standalone-phone rules out of their @media block and apply them unconditionally.
    const media = await page.evaluate(() => {
      for (const sheet of Array.from(document.styleSheets)) {
        for (const rule of Array.from(sheet.cssRules)) {
          if (rule instanceof CSSMediaRule && rule.cssText.includes('.status-bar-tint')) {
            const extra = new CSSStyleSheet()
            extra.replaceSync(Array.from(rule.cssRules).map((r) => r.cssText).join('\n'))
            document.adoptedStyleSheets = [...document.adoptedStyleSheets, extra]
            return rule.conditionText
          }
        }
      }
      return null
    })
    expect(media).toContain('display-mode: standalone')
    expect(media).toContain('max-width: 768px')
    // elementFromPoint honours pointer-events; WebKit's sampler ignores them.
    await page.addStyleTag({ content: '.status-bar-tint { pointer-events: auto !important; }' })

    // WebKit fills the bar solid only for an opaque fixed box >= 90% wide and
    // > 10px tall that is topmost at the top edge's midpoint (4px inset).
    const probe = () => page.evaluate(() => {
      const hit = document.elementFromPoint(window.innerWidth / 2, 4) as HTMLElement | null
      const strip = document.querySelector('.status-bar-tint') as HTMLElement
      const s = getComputedStyle(strip)
      const r = strip.getBoundingClientRect()
      return { hit: hit === strip, position: s.position, width: r.width / window.innerWidth, height: r.height, bg: s.backgroundColor }
    })

    const closed = await probe()
    expect(closed).toMatchObject({ hit: true, position: 'fixed', height: 12 + 59, bg: header })
    expect(closed.width).toBeGreaterThanOrEqual(0.9)

    await page.getByRole('button', { name: 'Open sidebar', exact: true }).click()
    await expect(page.locator('.chat-layout')).toHaveClass(/sidebar-open/)
    expect(await probe()).toMatchObject({ hit: true, bg: header })
    // Full-screen phone surfaces clear the strip (--chrome-top), so it hides no control.
    const firstControlTop = await page.locator('.sidebar button:visible').first()
      .evaluate((el) => el.getBoundingClientRect().top)
    expect(firstControlTop).toBeGreaterThanOrEqual(12 + 59)
  })
}

test('a browser tab does not render the status-bar strip', async ({ page }) => {
  await isolate(page, 'safe-area-status-tint-tab')
  await boot(page)
  await expect(page.locator('.status-bar-tint')).toBeHidden()
})
