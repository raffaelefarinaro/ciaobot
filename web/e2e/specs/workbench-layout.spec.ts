import { expect, test } from '@playwright/test'
import { boot, COMPOSER, isolate } from '../support/app'

/**
 * Prototype A at a desktop width: layout facts jsdom cannot see. The review
 * rail must sit beside the request column rather than under it, and the
 * sidebar's stacked top (workspace, New chat, destinations) must not collapse
 * back into one crowded row - which is what it did at the default 340px
 * sidebar before the rail was restacked.
 */
test.use({ viewport: { width: 1440, height: 900 } })

test.describe('workbench layout', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `workbench-${testInfo.workerIndex}`)
  })

  test('the review rail sits beside the command surface', async ({ page }) => {
    await boot(page)
    const surface = await page.locator('.home-intake-form').boundingBox()
    const rail = await page.locator('.home-rail').boundingBox()
    expect(surface).not.toBeNull()
    expect(rail).not.toBeNull()
    expect(rail!.x).toBeGreaterThan(surface!.x + surface!.width)

    // Rows, not a card grid: each row spans the rail's width. Up-to-date
    // queues drop out, so the fixture shows proposals and automations only.
    const rows = page.locator('.home-review-item')
    await expect(rows).toHaveCount(2)
    for (const row of await rows.all()) {
      const box = await row.boundingBox()
      expect(box!.width).toBeGreaterThan(rail!.width - 2)
    }
  })

  test('the sidebar stacks workspace, New chat and navigation without overlap', async ({ page }) => {
    await boot(page)
    const order = ['.workspace-scope-trigger', '.sidebar-new-chat', '.nav-links']
    let previousBottom = -Infinity
    for (const selector of order) {
      const box = await page.locator(selector).first().boundingBox()
      expect(box, selector).not.toBeNull()
      expect(box!.y, `${selector} starts below the control above it`).toBeGreaterThanOrEqual(previousBottom - 0.5)
      previousBottom = box!.y + box!.height
    }
    // Every destination shows its label at the default sidebar width.
    for (const label of ['Home', 'Automations', 'Memory', 'Settings']) {
      await expect(page.locator('.nav-links .nav-item-label', { hasText: label })).toBeVisible()
    }
  })

  test('the Work details rail reopens without scrolling sideways', async ({ page }) => {
    // The hide button overhung the rail's right edge, so the rail was wider
    // than itself; focusing the button on reopen scrolled it sideways and cut
    // off the rail's left edge.
    await boot(page, '/chat/alpha-chat-2', COMPOSER)
    const rail = page.locator('.chat-rail')
    await page.locator('.chat-rail-hide').click()
    await expect(rail).toHaveCount(0)
    await page.locator('[aria-controls="chat-work-rail"]').first().click()
    await expect(rail).toBeVisible()
    const box = await rail.evaluate(r => ({ left: r.scrollLeft, over: r.scrollWidth - r.clientWidth }))
    expect(box).toEqual({ left: 0, over: 0 })
  })

  test('the selected chat row is marked by its fill, not by an accent bar', async ({ page }) => {
    await boot(page, '/chat/alpha-chat-1', COMPOSER)

    // The rail's selection is the filled row plus the brighter label. It also
    // carried a 2px accent bar down its left edge, which said the same thing a
    // second time and was the only place the workspace accent bled into a list
    // it did not belong to.
    const [selected, other] = await page.evaluate(() => {
      const rows = Array.from(document.querySelectorAll('.chat-item'))
      const read = (el: Element) => {
        const cs = getComputedStyle(el)
        return { background: cs.backgroundColor, color: cs.color, shadow: cs.boxShadow }
      }
      return [read(rows.find(r => r.classList.contains('active'))!), read(rows.find(r => !r.classList.contains('active'))!)]
    })
    expect(selected.shadow, 'the selected row still paints an inset bar').toBe('none')
    expect(selected.background).not.toBe(other.background)
    expect(selected.color).not.toBe(other.color)
  })
})

/**
 * Between 600 and 768px of viewport the phone header and the desktop header both
 * matched at once: the phone grid puts the hamburger in column 1, and the
 * desktop rule promoted the page tag to a visible left-hand title in column 1 -
 * where `chat-pane` is the whole viewport, because the sidebar is a fixed
 * drawer. The two printed on top of each other, at exactly the widths where the
 * phone layout had just been given room to breathe. jsdom cannot see it (every
 * box is 0x0), so it is asserted here rather than in a component test.
 */
test.describe('pane header at a narrow desktop window', () => {
  test.use({ viewport: { width: 700, height: 900 }, isMobile: false, hasTouch: false })

  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `hdr-${testInfo.workerIndex}`)
  })

  test('no two header regions share a grid cell', async ({ page }) => {
    await boot(page, '/', '.home-intake-form')

    const regions = await page.locator('.pane-header').evaluate((head) =>
      Array.from(head.children)
        .filter((el) => getComputedStyle(el).display !== 'none')
        .map((el) => {
          const r = el.getBoundingClientRect()
          return { name: String(el.className).split(' ')[0], x: r.x, y: r.y, right: r.right, bottom: r.bottom }
        })
        .filter((r) => r.right - r.x > 1 && r.bottom - r.y > 1),
    )
    // Without this the loop below would pass on a header that rendered nothing.
    expect(regions.map((r) => r.name)).toEqual(expect.arrayContaining(['header-lead', 'header-center']))

    for (let i = 0; i < regions.length; i++) {
      for (let j = i + 1; j < regions.length; j++) {
        const a = regions[i]
        const b = regions[j]
        const ox = Math.min(a.right, b.right) - Math.max(a.x, b.x)
        const oy = Math.min(a.bottom, b.bottom) - Math.max(a.y, b.y)
        expect(
          ox > 2 && oy > 2,
          `${a.name} and ${b.name} overlap by ${Math.round(ox)}x${Math.round(oy)}px`,
        ).toBe(false)
      }
    }
  })
})
