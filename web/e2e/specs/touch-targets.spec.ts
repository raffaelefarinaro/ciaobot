import { devices, expect, test, type Page } from '@playwright/test'
import { BOOTED, COMPOSER, boot, isolate, quiesce } from '../support/app'

/**
 * DESIGN.md requires every interactive target to be at least 44x44 CSS px on
 * touch layouts. jsdom applies no CSS, so only a real engine under touch
 * emulation can measure that.
 */
const TOUCH_MIN = 44
// The workspace settings page is behind `workspacesLoaded`; measure the loaded
// cards, not the skeleton that satisfies the settings shell (#2274). The list
// renders in the same branch as the Overview cards and is the stronger wait.
const WORKSPACE_READY = '.workspace-row'
const TABS: Array<[string, string]> = [
  ['/settings', '.settings-main'],
  ['/settings/appearance', '.settings-main'],
  ['/settings/access', '.settings-main'],
  ['/settings/advanced', '.settings-main'],
  ['/settings/workspace', WORKSPACE_READY],
  ['/settings/providers', '.settings-main'],
  ['/settings/skills', '.settings-main'],
  ['/settings/subagents', '.settings-main'],
  ['/settings/commands', '.settings-main'],
  ['/settings/mcp', '.settings-main'],
]

// Content selectors, not shell ones: a surface must have rendered its data
// before it is measured. Automations and Import have no fixture rows, so they
// wait on their heading.
const MAIN_SURFACES: Array<[string, string]> = [
  ['/', '.home-setup'],
  ['/tasks', '.task-card'],
  ['/automations', '.schedule-panel .page-tag'],
  ['/memory/review', '.pr-select-toggle'],
  ['/memory/map', '.mm-toolbar-stale'],
  ['/memory/categories', '.cat-h'],
  ['/memory/import', '.import-title'],
  ['/memory/retired', '.guide-budget-link'],
  ['/memory/history', '.ph-seg'],
  ['/project/alpha-general', '.text-action'],
  ['/schedules/alpha-daily-brief', '.text-link'],
  ['/tasks/0123456789abcdef0123456789abcdef', '.task-status-opt'],
]

// Surfaces whose menus, popovers and dialogs are opened and measured.
// The settings surfaces wait for their own loaded controls, not the settings
// shell: the workspace card and the workspace overview are behind
// `workspacesLoaded`, so `.settings-main` is satisfied by a SkeletonLoader that
// has none of the triggers below (#2274).
const POPUP_SURFACES: Array<[string, string]> = [
  ...MAIN_SURFACES,
  ['/chat/alpha-chat-1', COMPOSER],
  ['/settings/workspace', WORKSPACE_READY],
  ['/settings/providers', '.settings-main'],
  ['/settings/advanced', '.settings-main'],
]

/**
 * Open every <details>, then list the interactive controls under `scope`
 * that are smaller than TOUCH_MIN in either dimension.
 */
async function measure(page: Page, scope = 'body') {
  // Never measure a layout that is still being styled: sizes read before a
  // lazy stylesheet applies are the fine-pointer ones.
  await quiesce(page)
  return page.evaluate(({ min, scope }) => {
    if (!matchMedia('(pointer: coarse)').matches) throw new Error('page is not a coarse pointer; sizes would be fine-pointer ones')
    document.querySelectorAll('details').forEach((d) => { d.open = true })
    const bad: string[] = []
    let total = 0
    const sel = 'button, a[href], [role=button], [role=switch], [role=tab], [role=checkbox], '
      + '[role=radio], [role=menuitem], [role=option], input:not([type=hidden]), select, summary'
    const seen = new Set<Element>()
    for (const raw of Array.from(document.querySelectorAll(`${scope} :is(${sel})`))) {
      // A native checkbox/radio is tapped through its label.
      const el = raw instanceof HTMLInputElement && (raw.type === 'checkbox' || raw.type === 'radio')
        ? (raw.closest('label') ?? raw)
        : raw
      if (seen.has(el)) continue
      seen.add(el)
      const parent = el.parentElement
      // Inline prose link (WCAG 2.5.8 exception): sits among sentence text.
      const inline = el.tagName === 'A' && parent && ['P', 'LI'].includes(parent.tagName)
        && !el.classList.contains('settings-help-link')
        && Array.from(parent.childNodes).some((n) => n.nodeType === Node.TEXT_NODE && (n.textContent ?? '').trim() !== '')
      if (inline) continue
      const cs = getComputedStyle(el)
      // Visually hidden keyboard controls (e.g. the task card's `.task-open`):
      // the whole card is the pointer target, so the control is not tapped.
      if (cs.clip === 'rect(0px, 0px, 0px, 0px)' || cs.clipPath === 'inset(50%)') continue
      if (cs.visibility === 'hidden') continue
      const r = el.getBoundingClientRect()
      if (r.width === 0 || r.height === 0) continue
      total += 1
      if (r.height < min - 0.5 || r.width < min - 0.5) {
        const label = (el.getAttribute('aria-label') || el.textContent || el.getAttribute('title') || '')
          .trim().replace(/\s+/g, ' ').slice(0, 60)
        bad.push(`"${label}" <${el.tagName.toLowerCase()} class="${el.className}"> ${Math.round(r.height)}×${Math.round(r.width)} (h×w)`)
      }
    }
    return { total, bad }
  }, { min: TOUCH_MIN, scope })
}

/** Visible popup triggers: menus, popovers, disclosure buttons and Settings' New form. */
const TRIGGER = '[aria-haspopup], [aria-expanded="false"]'
const TRIGGER_CAP = 80
// Opening these proves each surface reached the kind of popup it is meant to
// cover, not just some DOM change. Keyed by the surface they live on.
const KNOWN_TRIGGERS: Record<string, string[]> = {
  '/': ['Model for the new chat'],
  '/chat/alpha-chat-1': ['Show chat details'],
  '/settings/workspace': ['New workspace', 'Actions for workspace'],
}
// Triggers a surface may legitimately fail to click because another element
// covers them. This was one whole-run cap of 3; each surface is now its own
// test, so the cap is per surface and the caps sum to 2, never more than 3.
// The task editor opens as a modal over the board, so the board's "Show done
// tasks" toggle and the card's actions menu behind it are covered by design.
// Every other surface must click every trigger it tags.
const MAX_SKIPPED: Record<string, number> = { '/tasks/0123456789abcdef0123456789abcdef': 2 }

/**
 * Measure every open `.field-info-panel`: report those whose box leaves the
 * viewport or sits more than 12px from the visible glyph's bottom.
 */
async function panelProblems(page: Page) {
  return page.evaluate(() => {
    const out: string[] = []
    const w = window.innerWidth
    for (const el of Array.from(document.querySelectorAll('.field-info-panel'))) {
      const r = el.getBoundingClientRect()
      if (r.width === 0) continue
      const summary = el.parentElement?.querySelector('summary')
      if (r.left < -0.5 || r.right > w + 0.5) out.push(`panel overflows viewport: left ${Math.round(r.left)} right ${Math.round(r.right)} of ${w}`)
      const s = summary?.getBoundingClientRect()
      // The 20px glyph sits centred in the 44px hit area, so its bottom is the
      // summary's bottom minus the 12px inset on touch.
      const glyphBottom = s ? s.bottom - 12 : 0
      if (s && Math.abs(r.top - glyphBottom) > 12.5) out.push(`panel ${Math.round(r.top - glyphBottom)}px from the glyph`)
    }
    return { out }
  })
}

/** Tag every visible popup trigger with its index, so it can be found again after the DOM churns. */
async function tagTriggers(page: Page): Promise<number> {
  return page.evaluate((sel) => {
    document.querySelectorAll('[data-touch-trigger]').forEach((e) => e.removeAttribute('data-touch-trigger'))
    let n = 0
    for (const el of Array.from(document.querySelectorAll(sel))) {
      const r = el.getBoundingClientRect()
      if (r.width === 0 || r.height === 0 || getComputedStyle(el).visibility === 'hidden') continue
      // Covered by an open modal's backdrop (the task detail over the page
      // header): not something the user can open from here.
      const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)
      if (hit && !el.contains(hit)) continue
      el.setAttribute('data-touch-trigger', String(n))
      n += 1
    }
    return n
  }, TRIGGER)
}

/**
 * True once the DOM has changed from `before` elements and then held still
 * for a few frames; false if it never changed (nothing opened). Counted in
 * animation frames, not milliseconds: a wall-clock cutoff reads a slow runner
 * as "the click did nothing".
 */
async function settled(page: Page, before: number): Promise<boolean> {
  // Reset the counters first: values left by the previous trigger could
  // otherwise satisfy the check on the first frame, before anything opened.
  await page.evaluate(() => {
    const w = window as unknown as { __touchLast?: number; __touchStable?: number; __touchIdle?: number }
    w.__touchLast = undefined
    w.__touchStable = 0
    w.__touchIdle = 0
  })
  return page.waitForFunction(({ was, quiet, idle }) => {
    const w = window as unknown as { __touchLast?: number; __touchStable?: number; __touchIdle?: number }
    const now = document.getElementsByTagName('*').length
    if (now === was) {
      // Unchanged since the click: give a popup `idle` frames to start opening.
      w.__touchIdle = (w.__touchIdle ?? 0) + 1
      return w.__touchIdle >= idle ? 'nothing' : false
    }
    w.__touchStable = w.__touchLast === now ? (w.__touchStable ?? 0) + 1 : 0
    w.__touchLast = now
    return w.__touchStable >= quiet ? 'opened' : false
  }, { was: before, quiet: 5, idle: 60 }, { polling: 'raf', timeout: 0 }).then(async (h) => (await h.jsonValue()) === 'opened')
}

interface PopupWalkOptions {
  /** Awaited before each navigation, so a test can hold this boot's fetch. */
  beforeNavigate?: (boot: number) => Promise<void>
  /**
   * Awaited inside the walk after navigation and before the readiness wait,
   * once per boot. The seam a test uses to inspect (or hold) a surface before
   * its readiness condition can pass and before anything is tagged.
   */
  beforeReady?: (boot: number) => Promise<void>
  /** Bound on every readiness wait, so a test can prove that the walk blocks. */
  readyTimeout?: number
  /** Called in the turn each boot's readiness wait starts. */
  onReadyWait?: (boot: number) => void
  /** Called with the tag count after every enumeration. */
  onEnumerate?: (count: number) => void
}

/**
 * Walk every tagged popup trigger on one surface: click it, wait for it to
 * open, measure the controls it revealed, and check that the surface's known
 * triggers did open. Shared with the #2274 regression below, whose gated
 * workspace fetch holds every boot at the readiness seam.
 */
async function checkPopupSurface(
  page: Page,
  route: string,
  ready: string,
  options: PopupWalkOptions = {},
) {
  const small: string[] = []
  const panels: string[] = []
  const opened: string[] = []
  const skipped: string[] = []
  let boots = 0
  const bootSurface = () => {
    boots += 1
    return boot(page, route, ready, {
      beforeNavigate: options.beforeNavigate ? () => options.beforeNavigate!(boots) : undefined,
      beforeReady: options.beforeReady ? () => options.beforeReady!(boots) : undefined,
      onReadyWait: options.onReadyWait ? () => options.onReadyWait!(boots) : undefined,
      readyTimeout: options.readyTimeout,
    })
  }
  await bootSurface()
  await quiesce(page)
  let count = await tagTriggers(page)
  options.onEnumerate?.(count)
  expect(count, `${route}: trigger cap of ${TRIGGER_CAP} would hide triggers`).toBeLessThan(TRIGGER_CAP)
  const baseline = new Set((await measure(page)).bad)
  for (let i = 0; i < count; i += 1) {
    let trigger = page.locator(`[data-touch-trigger="${i}"]`)
    if ((await trigger.count()) !== 1) {
      // A re-render replaced it, or measure() opened a <details> that hid it: start clean.
      await bootSurface()
      count = await tagTriggers(page)
      options.onEnumerate?.(count)
      trigger = page.locator(`[data-touch-trigger="${i}"]`)
    }
    const label = (await trigger.first().evaluate((el) => (el.getAttribute('aria-label') || el.textContent || '')
      .trim().replace(/\s+/g, ' ').slice(0, 40)).catch(() => '')) || `#${i}`
    const name = `${route} › ${label}`
    await quiesce(page)
    const before = await page.evaluate(() => document.getElementsByTagName('*').length)
    try {
      await trigger.click({ timeout: 2000 })
    } catch (err) {
      skipped.push(`${name}: ${String(err)}`)
      await bootSurface()
      count = await tagTriggers(page)
      options.onEnumerate?.(count)
      continue
    }
    if (!(await settled(page, before))) continue // nothing opened
    opened.push(name)
    const found = await measure(page)
    for (const f of found.bad) if (!baseline.has(f)) small.push(`${name}: ${f}`)
    const p = await panelProblems(page)
    for (const m of p.out) panels.push(`${name}: ${m}`)
    await page.keyboard.press('Escape')
    // Escape leaves forms open on Settings, and a trigger may navigate: start those clean.
    if (route.startsWith('/settings') || new URL(page.url()).pathname !== route) {
      await bootSurface()
      count = await tagTriggers(page)
      options.onEnumerate?.(count)
    }
  }
  for (const known of KNOWN_TRIGGERS[route] ?? []) {
    expect(opened.some((o) => o.includes(known)), `trigger "${known}" never opened on ${route}; opened:\n${opened.join('\n')}\nskipped:\n${skipped.join('\n')}`).toBe(true)
  }
  expect(skipped.length, `triggers that could not be clicked:\n${skipped.join('\n')}`).toBeLessThanOrEqual(MAX_SKIPPED[route] ?? 0)
  return { small, panels, opened, skipped }
}

test.use({ ...devices['iPhone 13'], defaultBrowserType: undefined })

test.describe('touch targets', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `touch-${testInfo.workerIndex}`)
    // The mocked update below would otherwise raise an "Update available" toast
    // once per browser context, at whatever moment the status fetch lands. It
    // is fixed to the bottom of the viewport and covers whichever trigger is
    // there, so a click on it timed out depending on timing. Mark it seen: no
    // spec here is about the toast.
    await page.addInitScript(() => {
      try { localStorage.setItem('ciao-update-toast-seen-version', '1.2.0') } catch { /* private mode */ }
    })
    await page.route('**/api/package/status', (route) => route.fulfill({ json: {
      current_version: '1.1.0', latest_version: '1.2.0', update_available: true, mode: 'installer',
    } }))
    await page.route('**/api/update/status', (route) => route.fulfill({ json: {
      install_mode: 'installer', can_update: true, operation: null, running: false,
    } }))
  })

  test('touch emulation is a coarse pointer', async ({ page }) => {
    await boot(page)
    expect(await page.evaluate(() => matchMedia('(pointer: coarse)').matches)).toBe(true)
  })

  test('every Settings control is at least 44×44 on touch', async ({ page }) => {
    const small: string[] = []
    for (const [tab, ready] of TABS) {
      await boot(page, tab, ready)
      const found = await measure(page, '.settings-main')
      expect(found.total, `no controls measured on ${tab}`).toBeGreaterThan(0)
      for (const f of found.bad) small.push(`${tab}: ${f}`)
    }
    expect(small, `controls under ${TOUCH_MIN}px:\n${small.join('\n')}`).toEqual([])
  })

  test("every control on the app's main surfaces is at least 44×44 on touch", async ({ page }) => {
    const small: string[] = []
    for (const [route, ready] of MAIN_SURFACES) {
      await boot(page, route, ready)
      const found = await measure(page)
      expect(found.total, `no controls measured on ${route}`).toBeGreaterThan(0)
      for (const f of found.bad) small.push(`${route}: ${f}`)
    }
    expect(small, `controls under ${TOUCH_MIN}px:\n${small.join('\n')}`).toEqual([])
  })

  test('every control in the empty chat view is at least 44×44 on touch', async ({ page }) => {
    await boot(page)
    await page.click(BOOTED)
    await expect(page.locator('.chat-empty-knowledge')).toBeVisible()
    const found = await measure(page)
    expect(found.total, 'no controls measured in chat').toBeGreaterThan(0)
    expect(found.bad, `controls under ${TOUCH_MIN}px:\n${found.bad.join('\n')}`).toEqual([])
  })

  test('the Repository section is at least 44×44 on touch', async ({ page }) => {
    await boot(page, '/chat/alpha-chat-1', COMPOSER)
    // The code badge is hidden on phones; Chat details opens from the ⓘ button.
    await page.getByRole('button', { name: 'Show chat details' }).click()
    await expect(page.locator('#chat-work-rail .chat-repo-section')).toBeVisible()
    const found = await measure(page, '#chat-work-rail .chat-repo-section')
    expect(found.total, 'no controls measured in the Repository section').toBeGreaterThan(0)
    expect(found.bad, `controls under ${TOUCH_MIN}px:\n${found.bad.join('\n')}`).toEqual([])
  })

  for (const [route, ready] of POPUP_SURFACES) {
    test(`controls inside menus and dialogs on ${route} are at least 44×44 on touch`, async ({ page }) => {
      const { small, panels } = await checkPopupSurface(page, route, ready)
      expect.soft(panels, `popup panels out of place:\n${panels.join('\n')}`).toEqual([])
      expect(small, `controls under ${TOUCH_MIN}px:\n${small.join('\n')}`).toEqual([])
    })
  }

  /**
   * #2274's regression: on /settings/workspace the New workspace button and
   * every row's actions menu live in a card behind `workspacesLoaded`. Until
   * GET /api/workspaces answers, the page shows a SkeletonLoader that
   * satisfies `.settings-main` and animation quiescence - quiesce only waits
   * for the element count and document height to stop changing, so a stable
   * skeleton passes while the fetch is in flight. Tagging then found the
   * notifications bell and nothing else, and the known-trigger check failed
   * with `trigger "New workspace" never opened` - CI's failure.
   *
   * The response is gated, not delayed, and every gate is released from a
   * finally: the first walk is bounded at its readiness wait and has to reject
   * there without enumerating; the second walk is the real one, and a later
   * reload is gated separately to show it cannot re-tag until its own release.
   * Only then does the walk reach New workspace, open it, and pass every
   * existing per-trigger assertion.
   */
  test('#2274: New workspace is reached when /api/workspaces is still pending at boot', async ({ page }) => {
    const route = '/settings/workspace'
    // One gate per boot, created before that boot navigates. A request captures
    // the gate current when it starts, so a later boot can neither release an
    // earlier document's fetch nor be released by it.
    const gateReleases = new Map<number, () => void>()
    let gate: Promise<void> = Promise.resolve()
    const holdNextFetch = (boot: number) => {
      let release!: () => void
      gate = new Promise<void>((resolve) => { release = resolve })
      gateReleases.set(boot, release)
    }
    const releaseGate = (boot: number) => {
      const release = gateReleases.get(boot)
      if (!release) throw new Error(`no held /api/workspaces request for boot ${boot}`)
      gateReleases.delete(boot)
      release()
    }
    const releaseEveryGate = () => {
      gateReleases.forEach((release) => release())
      gateReleases.clear()
    }
    let intercepted = 0
    let interceptedBeforeBoot = 0
    await page.route('**/api/workspaces', async (r) => {
      intercepted += 1
      const thisBoot = gate
      await thisBoot
      await r.continue()
    })

    let enumerated = 0
    const countEnumeration: PopupWalkOptions['onEnumerate'] = (count) => {
      expect(count, 'an enumeration always tags at least one trigger').toBeGreaterThan(0)
      enumerated += 1
    }
    const expectLoadingCard = async (where: string) => {
      await expect(page.getByRole('status', { name: 'Loading workspace' }), `${where}: the skeleton is on screen`)
        .toBeVisible()
      await expect(page.locator(WORKSPACE_READY), `${where}: no workspace row before the response`).toHaveCount(0)
      await expect(page.locator('[data-touch-trigger]'), `${where}: nothing is tagged yet`).toHaveCount(0)
      await expect(page.getByRole('button', { name: 'New workspace' }), `${where}: the card is not rendered yet`)
        .toHaveCount(0)
    }
    // The seam runs before the readiness wait; its boot's request must already
    // be intercepted, or "still loading" would be an accident of ordering.
    const expectBootFetchHeld = async (where: string) => {
      await expect.poll(() => intercepted, `${where}: the boot's fetch is intercepted`)
        .toBeGreaterThan(interceptedBeforeBoot)
    }

    // First walk: the readiness wait itself is the block. The seam runs once
    // the document is up and before the ready selector can pass; the walk is
    // then bounded so the test observes it cannot get past that point.
    const blocked = checkPopupSurface(page, route, WORKSPACE_READY, {
      readyTimeout: 1000,
      onEnumerate: countEnumeration,
      beforeNavigate: async (boot) => {
        holdNextFetch(boot)
        interceptedBeforeBoot = intercepted
      },
      beforeReady: async (boot) => {
        if (boot !== 1) return
        await expectBootFetchHeld('blocked boot')
        await expectLoadingCard('blocked boot')
      },
    })
    let rejection: Error | null = null
    try {
      rejection = await blocked.then(() => null, (err: Error) => err)
      expect(rejection, 'the walk must not get past readiness while the response is gated').not.toBeNull()
      expect(rejection!.message, 'the walk blocked on the workspace readiness selector').toContain(WORKSPACE_READY)
      expect(enumerated, 'the blocked walk never reached enumeration').toBe(0)
    } finally {
      releaseEveryGate()
    }
    // The released response renders the card, so the second walk starts from a
    // page whose held request has actually been answered.
    await expect(page.locator(WORKSPACE_READY).first(), 'the released response renders the card').toBeVisible()

    // Second walk: every boot is gated again. Boot 1 shows the delayed
    // readiness and is released at its seam. Boot 2 is a reload after the first
    // popup; its response stays held while the boot's own readiness wait is
    // running, which is observed from outside before it is released.
    enumerated = 0
    let reloadIsWaiting!: () => void
    const reloadWait = new Promise<void>((resolve) => { reloadIsWaiting = resolve })
    const walk = checkPopupSurface(page, route, WORKSPACE_READY, {
      onEnumerate: countEnumeration,
      onReadyWait: (boot) => { if (boot === 2) reloadIsWaiting() },
      beforeNavigate: async (boot) => {
        holdNextFetch(boot)
        interceptedBeforeBoot = intercepted
      },
      beforeReady: async (boot) => {
        await expectBootFetchHeld(`boot ${boot}`)
        if (boot === 1) {
          await expectLoadingCard('first boot of the walk')
          releaseGate(boot)
        } else if (boot === 2) {
          // A fresh document: the previous document's tags are gone. The
          // response stays gated here; the release happens below, only after
          // the boot's readiness wait has been observed to make no progress.
          await expectLoadingCard('reload after the first popup')
        } else {
          releaseGate(boot)
        }
      },
    })
    try {
      // The walk is now inside boot 2's readiness wait with its response held.
      const stage = await Promise.race([
        reloadWait.then(() => 'reload-waiting'),
        walk.then(() => 'finished', () => 'failed'),
      ])
      expect(stage, 'the walk reached the reload readiness wait').toBe('reload-waiting')

      // Explicit observation: while that wait is active the readiness selector
      // cannot pass, so the walk cannot enumerate.
      const stillLoading = await expect(page.locator(WORKSPACE_READY).first())
        .toBeVisible({ timeout: 1000 })
        .then(() => null, (err: Error) => err)
      expect(stillLoading, 'the readiness selector cannot pass while the reload is held').not.toBeNull()
      await expect(page.locator('[data-touch-trigger]'), 'the reload has not re-tagged').toHaveCount(0)
      expect(enumerated, 'no enumeration while the reload is held').toBe(1)

      // Release the held reload and the walk resumes at that same wait.
      releaseGate(2)
      const { small, panels, opened } = await walk
      expect(opened.some((o) => o.includes('New workspace')), 'New workspace actually opened').toBe(true)
      expect(enumerated, 'the walk enumerated again after the gated reload').toBeGreaterThanOrEqual(2)
      expect.soft(panels, `popup panels out of place:\n${panels.join('\n')}`).toEqual([])
      expect(small, `controls under ${TOUCH_MIN}px:\n${small.join('\n')}`).toEqual([])
    } finally {
      releaseEveryGate()
    }
  })

  test('the workspace reorder handle and Move up / Move down are at least 44×44 on touch', async ({ page }) => {
    await boot(page, '/settings/workspace', WORKSPACE_READY)
    const handle = page.locator('[data-workspace-handle="beta"]')
    await expect(handle).toBeVisible()
    const box = await handle.boundingBox()
    expect(box).not.toBeNull()
    expect(box!.width).toBeGreaterThanOrEqual(TOUCH_MIN - 0.5)
    expect(box!.height).toBeGreaterThanOrEqual(TOUCH_MIN - 0.5)

    await page.getByRole('button', { name: 'Actions for workspace beta' }).click()
    for (const name of ['Move workspace beta up', 'Move workspace beta down']) {
      const item = page.getByRole('menuitem', { name })
      await expect(item).toBeVisible()
      const itemBox = await item.boundingBox()
      expect(itemBox, name).not.toBeNull()
      expect(itemBox!.height, name).toBeGreaterThanOrEqual(TOUCH_MIN - 0.5)
      expect(itemBox!.width, name).toBeGreaterThanOrEqual(TOUCH_MIN - 0.5)
    }
  })
})
