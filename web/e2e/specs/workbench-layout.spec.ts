import { expect, test } from '@playwright/test'
import { boot, COMPOSER, isolate } from '../support/app'

/**
 * Prototype A at a desktop width: layout facts jsdom cannot see. The review
 * rail must sit beside the request column rather than under it, and the
 * sidebar's stacked top (workspace, New chat, destinations) must not collapse
 * back into one crowded row - which is what it did at the default 340px
 * sidebar before the rail was restacked. The Home section gaps are here for the
 * same reason: one owner for the gap between sections is a claim about real
 * boxes, and only a real layout engine can check it.
 */
test.use({ viewport: { width: 1440, height: 900 } })

test.describe('workbench layout', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    // A slice per test, not per worker: opting in to update tasks writes a
    // session flag the fixture keeps, so a test that mutates must not share a
    // slice with one that asserts the mutation is absent.
    await isolate(page, `workbench-${testInfo.workerIndex}-${testInfo.title}`)
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

  test('Home separates its sections by one gap, whatever is open', async ({ page }) => {
    // Every section root on Home used to carry its own top margin, so the space
    // between the composer and whatever came next depended on which notices
    // happened to be open: 12px next to a notice, 42px down to the recent list,
    // and the two summing wherever both applied. `.home-main` owns the gap now,
    // and this measures it in a real layout engine rather than trusting the
    // stylesheet — jsdom gives every rect 0x0.
    await page.request.post('/__fixture__/update-tasks')
    await boot(page, '/', '.update-tasks')

    const report = await page.evaluate(() => {
      const main = document.querySelector('.home-main')
      if (!main) throw new Error('missing .home-main')
      const sections = Array.from(main.children).filter(
        el => el.getBoundingClientRect().height > 0,
      )
      return {
        rowGap: Number.parseFloat(getComputedStyle(main).rowGap),
        names: sections.map(el => String(el.className).split(' ')[0]),
        gaps: sections.slice(1).map((el, i) => {
          const above = sections[i].getBoundingClientRect().bottom
          return Math.round((el.getBoundingClientRect().top - above) * 100) / 100
        }),
      }
    })

    // The update group is on screen, which is what makes the pair the issue named
    // measurable: the composer, then the group, then whatever is below it.
    expect(report.names).toContain('home-intake')
    expect(report.names).toContain('update-tasks')
    expect(report.gaps).toHaveLength(report.names.length - 1)
    // Every boundary is the one token — not just the two ends being equal, which
    // a leftover section margin in the middle would still pass.
    for (const [i, gap] of report.gaps.entries()) {
      expect(gap, `${report.names[i]} → ${report.names[i + 1]}`).toBeCloseTo(report.rowGap, 0)
    }
  })

  test('a Home section root keeps the column it is laid out in', async ({ page }) => {
    // The shared gap reset used to read `margin: 0 auto`, and its inline half is
    // not free: `.home-main` is a column flex container, so an auto inline margin
    // on a child absorbs the free space on the cross axis. The chip (the one root
    // with `align-self: flex-start`) floated to the middle of the column and the
    // setup card — a root with no width of its own — collapsed to fit its text.
    // Neither is a gap, so the gap assertions above stayed green throughout; only
    // the edges say otherwise, and only a real layout engine has them.
    await page.request.post('/__fixture__/update-tasks')
    await boot(page, '/', '.update-tasks')

    // Closing a notice is how the recovery chip comes to be at all.
    await page.locator('.update-tasks .home-notice-close').first().click()
    await expect(page.locator('.home-notice-reopen')).toBeVisible()
    await expect(page.locator('.home-setup')).toBeVisible()

    const report = await page.evaluate(() => {
      const main = document.querySelector('.home-main')!.getBoundingClientRect()
      const read = (selector: string) => {
        const r = document.querySelector(selector)!.getBoundingClientRect()
        return { offset: Math.round(r.left - main.left), width: Math.round(r.width) }
      }
      return {
        column: Math.round(main.width),
        chip: read('.home-notice-reopen'),
        setup: read('.home-setup'),
      }
    })
    expect(report.chip.offset, 'the chip sits on the column edge, not in its middle').toBe(0)
    expect(report.setup, 'the setup card is as wide as the column').toEqual({ offset: 0, width: report.column })
  })

  test('a Home with no update work has no phantom gap for it', async ({ page }) => {
    // The group renders nothing at all when it has no open rows, so the gap the
    // layout reserves must not survive it: a doubled gap above the recent list
    // is the exact artefact a margin-only reset leaves behind.
    await boot(page, '/', '.home-intake')
    await expect(page.locator('.update-tasks')).toHaveCount(0)

    const report = await page.evaluate(() => {
      const main = document.querySelector('.home-main')!
      const sections = Array.from(main.children).filter(
        el => el.getBoundingClientRect().height > 0,
      )
      return {
        rowGap: Number.parseFloat(getComputedStyle(main).rowGap),
        names: sections.map(el => String(el.className).split(' ')[0]),
        gaps: sections.slice(1).map((el, i) =>
          Math.round((el.getBoundingClientRect().top - sections[i].getBoundingClientRect().bottom) * 100) / 100),
      }
    })
    expect(report.names.length).toBeGreaterThan(1)
    for (const [i, gap] of report.gaps.entries()) {
      expect(gap, `${report.names[i]} → ${report.names[i + 1]}`).toBeCloseTo(report.rowGap, 0)
    }
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
 * Home's section gaps at phone width. The same three boxes, the same one token,
 * on the layout that stacks the rail under the column — a `margin-top` on the
 * recent list that the shared gap also applies would show up here as a double.
 */
test.describe('home section gaps on a phone', () => {
  test.use({ viewport: { width: 390, height: 844 } })

  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `homegaps-${testInfo.workerIndex}`)
  })

  test('one gap between every section, at both widths', async ({ page }) => {
    await page.request.post('/__fixture__/update-tasks')
    await boot(page, '/', '.update-tasks')

    const report = await page.evaluate(() => {
      const main = document.querySelector('.home-main')!
      const sections = Array.from(main.children).filter(
        el => el.getBoundingClientRect().height > 0,
      )
      return {
        rowGap: Number.parseFloat(getComputedStyle(main).rowGap),
        names: sections.map(el => String(el.className).split(' ')[0]),
        gaps: sections.slice(1).map((el, i) =>
          Math.round((el.getBoundingClientRect().top - sections[i].getBoundingClientRect().bottom) * 100) / 100),
      }
    })
    expect(report.names).toContain('update-tasks')
    expect(report.gaps).toHaveLength(report.names.length - 1)
    for (const [i, gap] of report.gaps.entries()) {
      expect(gap, `${report.names[i]} → ${report.names[i + 1]}`).toBeCloseTo(report.rowGap, 0)
    }
    expect(report.rowGap).toBeGreaterThan(0)
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
