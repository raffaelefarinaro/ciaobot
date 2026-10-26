import { expect, test, type Page, type TestInfo } from '@playwright/test'
import {
  BELL,
  RAIL_BELL,
  ROWS,
  activeWorkspaceButton,
  bellCount,
  boot,
  fixtureWrites,
  horizontalOverflow,
  isolate,
  quiesce,
  secondDevice,
  seedInbox,
} from '../support/app'

/**
 * Why this is a browser test and not a vitest one.
 *
 * The inbox is a projection over owner stores, and every claim worth making
 * about it is about the real app around it: the shell it sits in on a direct
 * load, the rail and the drawer that hold its bell, a second device whose
 * server-side `chat_read` arrives over a real socket, a request that is still
 * in flight when the workspace changes, and a link whose target vanished
 * between the projection and the click. jsdom has no shell, no socket and no
 * layout, so it can say none of that.
 *
 * Alpha is not empty by default (a proposals row, and alpha-chat-7's memory
 * pass is blocking), so alpha assertions are relative to a baseline or name a
 * row by its key. Gamma has no default rows and hosts the zero-state tests.
 */
test.use({ viewport: { width: 1440, height: 900 } })

const SUMMARY = '[data-testid="inbox-summary"]'
const row = (page: Page, key: string) => page.locator(`[data-testid="row-${key}"]`)

/**
 * Writes made on the user's behalf. Boot itself syncs the appearance settings
 * with a PATCH, which is not something the inbox does, so it is not evidence.
 */
async function writes(page: Page) {
  return (await fixtureWrites(page)).filter(
    (w) => !(w.method === 'PATCH' && w.path === '/api/settings/appearance'),
  )
}

const session = (ti: TestInfo) => `inbox-${ti.workerIndex}-${ti.title}`

const PERM = JSON.stringify({
  request_id: 'perm-1',
  session_id: '',
  tool_name: 'Bash',
  message: 'Run the build?',
  tool_input: 'npm run build',
})
const TASK_ID = '0123456789abcdef0123456789abcdef'
const REVIEW_TASK = {
  id: TASK_ID,
  title: 'Reply to Ivo in the Feedback Tracker',
  status: 'in_review',
  project_id: '',
  due: '',
  assignee: 'user',
  chat_id: '',
  attempt_id: '',
  created_at: '2026-01-01T09:00:00+00:00',
  updated_at: '2026-01-01T09:00:00+00:00',
  revision: 'a'.repeat(64),
  relative_path: `Workspace/Tasks/${TASK_ID}.md`,
  attempt_state: 'ready_for_review',
  attempt_outcome: '',
  attempt_summary: '',
  attempt_detail: '',
  live_attempt_id: 'attempt-1',
}
const TASK_ROW = `task:alpha:${TASK_ID}`
const FAILING_SCHEDULE = { last_status: 'error' }
const REPLY = {
  last_activity_at: '2026-01-02T10:00:00Z',
  last_read_at: '2026-01-01T09:05:00Z',
  last_snippet: 'Done',
}
const UNREAD_ROW = (id: string) => `chat:${id}`

test.beforeEach(async ({ page }, ti) => {
  await isolate(page, session(ti))
})

test.describe('notification inbox', () => {
  test('direct_load_keeps_the_shell', async ({ page }) => {
    await seedInbox(page, { chats: { 'alpha-chat-2': REPLY } })
    await boot(page, '/notifications', SUMMARY)

    await expect(page.locator('.nav-links')).toBeVisible()
    await expect(page.locator(BELL)).toHaveClass(/nav-item--active/)
    await expect(page).toHaveTitle(/notifications/i)
    await expect(
      page.locator('section', { has: page.getByRole('heading', { name: /New replies/ }) })
        .locator('[data-testid="row-chat:alpha-chat-2"]'),
    ).toBeVisible()
    await page.screenshot({ path: test.info().outputPath('inbox-expanded.png') })

    await page.reload()
    await expect(page.locator(SUMMARY)).toBeVisible()
    await expect(page.locator('.nav-links')).toBeVisible()
    await expect(page.locator(BELL)).toHaveClass(/nav-item--active/)
    await expect(row(page, UNREAD_ROW('alpha-chat-2'))).toBeVisible()
  })

  test('arrival_on_another_view_updates_the_badge', async ({ page }) => {
    await boot(page, '/tasks', '.task-card')
    const base = await bellCount(page)

    await seedInbox(page, {
      chats: { 'alpha-chat-2': REPLY },
      events: [{
        type: 'chat_result_ready',
        chat_id: 'alpha-chat-2',
        project_id: 'alpha-notes',
        title: 'alpha second conversation',
        snippet: 'Done',
      }],
    })
    await expect.poll(() => bellCount(page)).toBe(base + 1)

    await page.locator(BELL).click()
    await expect(row(page, UNREAD_ROW('alpha-chat-2'))).toBeVisible()
    await expect(
      page.locator('section', { has: page.getByRole('heading', { name: /New replies/ }) })
        .locator('[data-testid="row-chat:alpha-chat-2"]'),
    ).toBeVisible()
  })

  test('opening_the_inbox_reads_nothing_and_two_devices_converge', async ({ page, browser }, ti) => {
    const b = await secondDevice(browser, session(ti))
    try {
      await seedInbox(page, {
        chats: {
          'alpha-chat-1': REPLY,
          'alpha-chat-2': { ...REPLY, pending_permission: PERM },
        },
      })
      await boot(page, '/notifications', SUMMARY)
      await boot(b, '/notifications', SUMMARY)
      for (const p of [page, b]) {
        for (const id of ['alpha-chat-1', 'alpha-chat-2']) {
          await expect(row(p, UNREAD_ROW(id))).toBeVisible()
          await expect(row(p, UNREAD_ROW(id)).getByText('Unread')).toHaveCount(1)
        }
      }
      expect((await fixtureWrites(page)).filter((w) => w.path.endsWith('/read'))).toEqual([])

      // A opens chat-1. Opening is what reads it, and B hears about it.
      const bBefore = await bellCount(b)
      await row(page, UNREAD_ROW('alpha-chat-1')).locator(ROWS).click()
      await expect(page).toHaveURL(/\/chat\/alpha-chat-1$/)
      await expect.poll(async () =>
        (await fixtureWrites(page)).filter((w) => w.path === '/api/chats/alpha-chat-1/read'),
      ).toEqual([{ method: 'POST', path: '/api/chats/alpha-chat-1/read' }])
      await expect(row(b, UNREAD_ROW('alpha-chat-1'))).toHaveCount(0)
      await expect.poll(() => bellCount(b)).toBe(bBefore - 1)

      // A opens chat-2, which also needs a permission answer. Read is not resolved.
      const bAfter = await bellCount(b)
      await page.locator(BELL).click()
      await expect(page).toHaveURL(/\/notifications$/)
      await row(page, UNREAD_ROW('alpha-chat-2')).locator(ROWS).click()
      await expect(page).toHaveURL(/\/chat\/alpha-chat-2$/)
      await expect(row(b, UNREAD_ROW('alpha-chat-2')).getByText('Unread')).toHaveCount(0)
      await expect(
        b.locator('section', { has: b.getByRole('heading', { name: /Needs you/ }) })
          .locator('[data-testid="row-chat:alpha-chat-2"]'),
      ).toBeVisible()
      expect(await bellCount(b)).toBe(bAfter)

      // Nothing answered the permission, anywhere.
      const paths = (await writes(page)).map((w) => `${w.method} ${w.path}`)
      for (const p of paths) {
        expect(p).toMatch(/^POST \/api\/chats\/alpha-chat-[12]\/read$/)
      }
    } finally {
      await b.context().close()
    }
  })

  test('resolved_elsewhere_removes_task_and_schedule_rows', async ({ page }) => {
    await seedInbox(page, {
      tasks: { alpha: [REVIEW_TASK] },
      schedules: { 'alpha-daily-brief': FAILING_SCHEDULE },
    })
    await boot(page, '/notifications', SUMMARY)
    await expect(row(page, TASK_ROW)).toBeVisible()
    await expect(row(page, 'schedule:alpha-daily-brief')).toBeVisible()
    const base = await bellCount(page)

    await seedInbox(page, {
      tasks: { alpha: [{ ...REVIEW_TASK, status: 'done', attempt_state: '', live_attempt_id: '' }] },
      events: [{ type: 'tasks_changed', workspace: 'alpha' }],
    })
    await expect(row(page, TASK_ROW)).toHaveCount(0)
    await expect.poll(() => bellCount(page)).toBe(base - 1)

    await seedInbox(page, {
      schedules: { 'alpha-daily-brief': { last_status: 'ok' } },
      events: [{ type: 'schedules_changed' }],
    })
    await expect(row(page, 'schedule:alpha-daily-brief')).toHaveCount(0)
    await expect.poll(() => bellCount(page)).toBe(base - 2)
    expect(await writes(page)).toEqual([])
  })

  test('partial_failure_is_never_all_clear', async ({ page }) => {
    await page.route('**/api/schedules', (r) => r.fulfill({ status: 500, body: '{}' }))
    await boot(page, '/notifications', SUMMARY)
    await page.keyboard.press('3')
    await expect(activeWorkspaceButton(page)).toContainText('Gamma')

    await expect(page.locator('[data-testid="inbox-empty-unavailable"]')).toBeVisible()
    await expect(page.locator('[data-testid="inbox-empty-clear"]')).toHaveCount(0)
    await expect(page.locator('[data-testid="source-automations"]'))
      .toContainText('Automations: could not be checked.')
    await expect(page.locator(BELL)).toHaveAttribute('aria-label', /Some sources could not be fully checked$/)
    await expect(page.locator('.nav-item-alert--partial').first()).toBeVisible()

    await page.unroute('**/api/schedules')
    await page.getByRole('button', { name: 'Retry checking Automations' }).click()
    await expect(page.locator('[data-testid="inbox-empty-clear"]')).toHaveText("You're all caught up.")
    await expect(page.locator('[data-testid="source-automations"]')).toHaveCount(0)
    await expect(page.locator('.nav-item-alert--partial')).toHaveCount(0)
    await expect(page.locator(BELL)).toHaveAttribute('aria-label', 'Notifications, none')
  })

  test('stale_rows_are_retained_and_marked', async ({ page }) => {
    await seedInbox(page, { chats: { 'alpha-chat-2': REPLY } })
    await boot(page, '/notifications', SUMMARY)
    await expect(row(page, UNREAD_ROW('alpha-chat-2'))).toBeVisible()

    await page.route('**/api/chats*', (r) => r.fulfill({ status: 500, body: '{}' }))
    await page.evaluate(() => window.dispatchEvent(new Event('focus')))

    await expect(row(page, UNREAD_ROW('alpha-chat-2'))).toBeVisible()
    await expect(row(page, UNREAD_ROW('alpha-chat-2')).getByText('May be out of date')).toBeVisible()
    await expect(page.locator('[data-testid="source-chats"]'))
      .toContainText('Chats: showing the last successful check')

    await page.unroute('**/api/chats*')
    await page.getByRole('button', { name: 'Retry checking Chats' }).click()
    await expect(page.locator('[data-testid="source-chats"]')).toHaveCount(0)
    await expect(row(page, UNREAD_ROW('alpha-chat-2')).getByText('May be out of date')).toHaveCount(0)
    await expect(row(page, UNREAD_ROW('alpha-chat-2'))).toBeVisible()
  })

  test('offline_and_reconnect', async ({ page, request }, ti) => {
    await boot(page, '/notifications', SUMMARY)
    const headers = { cookie: `e2e_session=${session(ti)}` }
    const connections = async () =>
      (await (await request.get('/__fixture__/ws-count', { headers })).json()).connections as number
    const before = await connections()
    expect(before).toBeGreaterThan(0)
    await expect(row(page, TASK_ROW)).toHaveCount(0)

    await page.context().setOffline(true)
    await request.post('/__fixture__/drop-ws', { headers })
    await request.post('/__fixture__/inbox', { headers, data: { tasks: { alpha: [REVIEW_TASK] } } })
    await page.context().setOffline(false)

    await expect.poll(connections).toBeGreaterThan(before)
    // The new socket's snapshot reloads the task signals: no click, no reload.
    await expect(row(page, TASK_ROW)).toBeVisible()
  })

  test('workspace_round_trip_rejects_late_responses', async ({ page }) => {
    await seedInbox(page, {
      tasks: { alpha: [REVIEW_TASK] },
      chats: { 'alpha-chat-2': REPLY, 'beta-chat-2': REPLY },
    })
    await boot(page, '/notifications', SUMMARY)
    await expect(row(page, TASK_ROW)).toBeVisible()
    await expect(row(page, UNREAD_ROW('alpha-chat-2'))).toBeVisible()

    let hold = false
    let release: () => void = () => {}
    const gate = new Promise<void>((resolve) => { release = resolve })
    await page.route('**/api/tasks?workspace=alpha*', async (route) => {
      if (hold) await gate
      await route.continue()
    })

    hold = true
    await seedInbox(page, { events: [{ type: 'tasks_changed', workspace: 'alpha' }] })
    await page.keyboard.press('2')
    await expect(activeWorkspaceButton(page)).toContainText('Beta')
    await expect(row(page, UNREAD_ROW('beta-chat-2'))).toBeVisible()
    await expect(row(page, UNREAD_ROW('alpha-chat-2'))).toHaveCount(0)
    await expect(row(page, TASK_ROW)).toHaveCount(0)

    hold = false
    release()
    // Let the late alpha answer land, by waiting for the row count to hold still.
    let last = -1
    let stable = 0
    await expect.poll(async () => {
      const n = await page.locator(ROWS).count()
      stable = n === last ? stable + 1 : 0
      last = n
      return stable
    }).toBeGreaterThanOrEqual(5)
    await expect(row(page, TASK_ROW)).toHaveCount(0)
    await expect(row(page, UNREAD_ROW('alpha-chat-2'))).toHaveCount(0)

    await page.keyboard.press('1')
    await expect(activeWorkspaceButton(page)).toContainText('Alpha')
    await expect(row(page, TASK_ROW)).toBeVisible()
    await expect(row(page, UNREAD_ROW('alpha-chat-2'))).toBeVisible()
    await expect(row(page, UNREAD_ROW('beta-chat-2'))).toHaveCount(0)
  })

  test('removed_target_shows_unavailable_and_stays', async ({ page }) => {
    await seedInbox(page, {
      chats: { 'alpha-chat-2': REPLY },
      tasks: { alpha: [REVIEW_TASK] },
    })
    await boot(page, '/notifications', SUMMARY)
    await expect(row(page, UNREAD_ROW('alpha-chat-2'))).toBeVisible()
    await expect(row(page, TASK_ROW)).toBeVisible()
    const base = await bellCount(page)

    // Gone on the server, and nothing told the page.
    await seedInbox(page, { removeChats: ['alpha-chat-2'], tasks: { alpha: [] } })
    const notice = page.locator('[data-testid="inbox-unavailable"]')

    await row(page, UNREAD_ROW('alpha-chat-2')).locator(ROWS).click()
    await expect(notice).toBeVisible()
    await expect(notice).toBeFocused()
    await expect(page).toHaveURL(/\/notifications$/)
    await expect(row(page, UNREAD_ROW('alpha-chat-2'))).toHaveCount(0)
    await expect.poll(() => bellCount(page)).toBeLessThan(base)
    expect((await fixtureWrites(page)).some((w) => w.path === '/api/chats/alpha-chat-2/read')).toBe(false)

    // The task row went with the same read (it is a chats-source row), so the
    // notice stays and nothing else opened. Offer it again and repeat.
    await expect(row(page, TASK_ROW)).toHaveCount(0)
    await seedInbox(page, { tasks: { alpha: [REVIEW_TASK] }, events: [{ type: 'tasks_changed', workspace: 'alpha' }] })
    await expect(row(page, TASK_ROW)).toBeVisible()
    await seedInbox(page, { tasks: { alpha: [] } })
    await row(page, TASK_ROW).locator(ROWS).click()
    await expect(notice).toBeVisible()
    await expect(notice).toBeFocused()
    await expect(page).toHaveURL(/\/notifications$/)
    await expect(row(page, TASK_ROW)).toHaveCount(0)
    expect(await writes(page)).toEqual([])
  })

  test('exact_home_notice_is_revealed_without_running', async ({ page }) => {
    const ACTION_KEY = 'action:shared:e2e-check'
    const UPDATE_KEY = 'update:alpha:review-legacy-rows@1'
    await seedInbox(page, {
      actions: [{
        id: 'e2e-check',
        kind: 'setup-check',
        severity: 20,
        title: 'Check the backup remote',
        detail: 'The backup remote is not set.',
        glyph: '',
        workspace: '',
        run_label: 'Run check',
        chat_label: '',
        chat_prompt: '',
        view_label: '',
        view_route: '',
        blocking: false,
      }],
    })
    expect((await page.request.post('/__fixture__/update-tasks')).ok()).toBe(true)
    await boot(page, '/')
    const action = page.locator(`[data-notice-key="${ACTION_KEY}"]`)
    const update = page.locator(`[data-notice-key="${UPDATE_KEY}"]`)
    await expect(action).toBeVisible()
    await expect(update).toBeVisible()

    await action.locator('.home-notice-close').click()
    await update.locator('.home-notice-close').click()
    await expect(action).toHaveCount(0)
    await expect(update).toHaveCount(0)

    // An SPA navigation, so the in-memory closed set survives: local X is not a dismissal.
    await page.locator(BELL).click()
    const operatorRow = row(page, 'operator:shared:e2e-check')
    const updateRow = row(page, 'update-task:alpha:review-legacy-rows@1')
    await expect(operatorRow).toBeVisible()
    await expect(updateRow).toBeVisible()

    await operatorRow.locator(ROWS).click()
    await expect(page).toHaveURL((u) => u.pathname === '/' && !u.search.includes('notice'))
    await expect(action).toBeVisible()
    await expect(action).toBeFocused()
    await expect(update).toHaveCount(0)

    await page.locator(BELL).click()
    await updateRow.locator(ROWS).click()
    await expect(page).toHaveURL((u) => u.pathname === '/' && !u.search.includes('notice'))
    await expect(update).toBeVisible()
    await expect(update).toBeFocused()
    expect(await writes(page)).toEqual([])

    // A key that names nothing says so and reveals nothing.
    await action.locator('.home-notice-close').click()
    await page.evaluate(() => {
      history.pushState({}, '', '/?notice=action%3Ashared%3Agone')
      window.dispatchEvent(new PopStateEvent('popstate'))
    })
    await expect(page.getByText('This notice is no longer available')).toBeVisible()
    await expect(action).toHaveCount(0)
    expect(await writes(page)).toEqual([])
  })

  for (const variant of ['denied', 'unsupported'] as const) {
    test(`works_without_push_permission_${variant}`, async ({ page }) => {
      const errors: string[] = []
      page.on('pageerror', (e) => errors.push(e.message))
      await page.addInitScript((v) => {
        Object.defineProperty(window, 'Notification', {
          configurable: true,
          value: v === 'denied'
            ? { permission: 'denied', requestPermission: async () => 'denied' }
            : undefined,
        })
      }, variant)
      await seedInbox(page, { chats: { 'alpha-chat-2': REPLY } })
      await boot(page, '/notifications', SUMMARY)

      await expect(row(page, UNREAD_ROW('alpha-chat-2'))).toBeVisible()
      await expect(row(page, UNREAD_ROW('alpha-chat-2')).getByText('Unread')).toHaveCount(1)
      expect(await bellCount(page)).toBeGreaterThan(0)
      expect(errors).toEqual([])
    })
  }

  test('collapsed_rail_bell', async ({ page }) => {
    await boot(page, '/')
    await seedInbox(page, {
      chats: { 'alpha-chat-1': REPLY, 'alpha-chat-2': REPLY },
      events: [
        { type: 'chat_result_ready', chat_id: 'alpha-chat-1', project_id: 'alpha-general', title: 'alpha first conversation', snippet: 'Done' },
        { type: 'chat_result_ready', chat_id: 'alpha-chat-2', project_id: 'alpha-notes', title: 'alpha second conversation', snippet: 'Done' },
      ],
    })
    await expect.poll(() => bellCount(page)).toBeGreaterThanOrEqual(2)
    const count = await bellCount(page)
    const label = await page.locator(BELL).getAttribute('aria-label')

    await page.locator('button[aria-label="Collapse sidebar"]').click()
    await expect(page.locator('.nav-links')).not.toBeVisible()
    const rail = page.locator(RAIL_BELL)
    await expect(rail).toBeVisible()
    await expect(rail).toHaveAttribute('aria-label', label!)
    await expect(rail).toHaveAttribute('title', 'Notifications')
    await expect(page.locator('.sidebar-rail-bell-count')).toHaveText(String(count))
    await page.screenshot({ path: test.info().outputPath('inbox-collapsed.png') })

    await rail.focus()
    await expect(rail).toBeFocused()
    await page.keyboard.press('Enter')
    await expect(page).toHaveURL(/\/notifications$/)
    await expect(page.locator(SUMMARY)).toBeVisible()
    await expect(page.locator(RAIL_BELL)).toBeVisible()
    await expect(page.locator('.nav-links')).not.toBeVisible()
  })

  test('mobile_drawer_closes_on_navigation', async ({ browser }, ti) => {
    const page = await secondDevice(browser, session(ti), {
      viewport: { width: 390, height: 844 },
      hasTouch: true,
      isMobile: true,
    })
    try {
      await seedInbox(page, { chats: { 'alpha-chat-2': REPLY } })
      await page.route('**/api/schedules', (r) => r.fulfill({ status: 500, body: '{}' }))
      await boot(page)
      const open = page.locator('button.header-hamburger[aria-label="Open sidebar"]').first()
      const bell = page.locator(BELL)
      const drawerClosed = async () => {
        await expect(page).toHaveURL(/\/notifications$/)
        await expect(page.locator('.chat-layout.sidebar-open')).toHaveCount(0)
        await expect(page.locator('.sidebar-backdrop')).toHaveCount(0)
        await expect(page.locator(SUMMARY)).toBeVisible()
      }

      await open.click()
      await expect(page.locator('.chat-layout.sidebar-open')).toHaveCount(1)
      await expect(page.locator('.sidebar-backdrop')).toBeVisible()
      expect((await bell.boundingBox())!.height).toBeGreaterThanOrEqual(44)
      await bell.click()
      await drawerClosed()

      // The keyboard path gets the same result.
      await page.goto('/')
      await expect(open).toBeVisible()
      await open.click()
      await expect(page.locator('.chat-layout.sidebar-open')).toHaveCount(1)
      await bell.focus()
      await page.keyboard.press('Enter')
      await drawerClosed()

      await quiesce(page)
      expect((await horizontalOverflow(page)).overflow).toBeLessThanOrEqual(0)
      for (const sel of [ROWS, '.notif-retry']) {
        const box = await page.locator(sel).first().boundingBox()
        expect(box, sel).not.toBeNull()
        expect(box!.height, sel).toBeGreaterThanOrEqual(44)
      }
      await page.screenshot({ path: test.info().outputPath('inbox-mobile.png') })
    } finally {
      await page.context().close()
    }
  })

  test('long_titles_zoom_and_reduced_motion', async ({ page }) => {
    const evil = `${'x'.repeat(200)}<img src=x onerror=1>&"'`
    await seedInbox(page, { chats: { 'alpha-chat-2': { ...REPLY, title: evil } } })
    await boot(page, '/notifications', SUMMARY)
    const r = row(page, UNREAD_ROW('alpha-chat-2'))
    await expect(r).toBeVisible()
    await expect(r.locator('img')).toHaveCount(0)
    await expect(r.locator('.notif-title')).toHaveText(evil)

    await page.setViewportSize({ width: 640, height: 450 })
    await expect(r).toBeVisible()
    await quiesce(page)
    const { overflow, culprits } = await horizontalOverflow(page)
    expect(overflow, `scrolls sideways; widest: ${JSON.stringify(culprits)}`).toBeLessThanOrEqual(0)
    const viewportWidth = 640
    for (const el of await page.locator('.notifications-view a, .notifications-view button').all()) {
      const box = await el.boundingBox()
      expect(box!.x).toBeGreaterThanOrEqual(0)
      expect(box!.x + box!.width).toBeLessThanOrEqual(viewportWidth + 1)
    }
    await page.screenshot({ path: test.info().outputPath('inbox-long-title.png') })

    await page.emulateMedia({ reducedMotion: 'reduce' })
    await quiesce(page)
    const running = await page.evaluate(() =>
      document.getAnimations().filter((a) => {
        const t = a.effect?.getComputedTiming()
        const target = (a.effect as KeyframeEffect | null)?.target
        return t !== undefined && Number.isFinite(t.endTime) && a.playState === 'running'
          && !!target && !!(target as Element).closest('.notifications-view')
      }).length,
    )
    expect(running).toBe(0)

    await expect(page.getByRole('link', { name: /^Notifications, \d+ items?/ })).toBeVisible()
    await expect(page.locator(SUMMARY)).toHaveAttribute('role', 'status')
    await expect(page.getByRole('status').filter({ hasText: /items? needs? a look/ })).toBeVisible()
  })
})
