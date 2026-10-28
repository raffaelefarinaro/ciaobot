import { expect, test, type Page } from '@playwright/test'
import { COMPOSER, boot, fixtureWrites, isolate, quiesce } from '../support/app'

const TASK_ID = '0123456789abcdef0123456789abcdef'
const TITLE = 'Reply to Ivo in the Feedback Tracker'
const EDITOR = '.task-detail-sheet'

/**
 * Why this is a browser test: whether a link opens a second tab, reloads the
 * page or opens in place is the browser's default action against the click
 * handler, and jsdom performs no navigation at all. So is whether the editor
 * over a chat meets the 44px touch minimum, which needs real CSS.
 */
async function openTaskLink(page: Page) {
  await boot(page, '/chat/alpha-chat-1', COMPOSER)
  await page.evaluate(() => fetch('/__fixture__/transcript', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ shape: 'task-link' }),
  }))
  await page.reload()
  await page.waitForSelector(COMPOSER)

  const link = page.locator('.message-wrap.assistant a', { hasText: 'Reply to Ivo' })
  await expect(link).toHaveAttribute('href', `/tasks/${TASK_ID}`)
  await expect(link).not.toHaveAttribute('target', /.*/)
  // A full page load would clear this marker; opening in place keeps it.
  await page.evaluate(() => { (window as unknown as { __kept: boolean }).__kept = true })
  return link
}

test.describe('a task link in a chat reply', () => {
  test.use({ viewport: { width: 1280, height: 900 } })

  test.beforeEach(async ({ page }, testInfo) => {
    // Per test, not per worker: the fixture keeps a task write per session, so a
    // shared id would let one test's saved title decide what another one reads.
    await isolate(page, `task-link-${testInfo.workerIndex}-${testInfo.testId}`)
  })

  test('opens the full editor over the chat, keeps the address, and reopens what it saved', async ({ page, context }) => {
    const link = await openTaskLink(page)

    // A plain click opens the board's editor over the chat and leaves the
    // address alone. It is the editor, not a read-only peek: every field and
    // the Agent block are here.
    await link.click()
    const editor = page.locator(EDITOR)
    await expect(editor).toBeVisible()
    await expect(editor.locator('#task-detail-name')).toHaveValue(TITLE)
    await expect(editor).toContainText('Find his comment and draft a reply.')
    await expect(editor.locator('#task-detail-project')).toBeVisible()
    await expect(editor.locator('.task-delegate-actions')).toContainText('Delegate to agent')
    await expect(page).toHaveURL(/\/chat\/alpha-chat-1$/)
    expect(context.pages()).toHaveLength(1)

    // The title is the editor's own field: typed text is what a close writes.
    await editor.locator('#task-detail-name').fill('Reply to Ivo with the draft')
    await page.keyboard.press('Escape')
    await expect(editor).toBeHidden()
    await expect(page).toHaveURL(/\/chat\/alpha-chat-1$/)
    // Focus is back on the link that opened the overlay, not on the body.
    await expect(link).toBeFocused()
    expect(await page.evaluate(() => (window as unknown as { __kept?: boolean }).__kept)).toBe(true)
    const writes = await fixtureWrites(page)
    expect(writes.some(write => write.method === 'PATCH' && write.path === `/api/tasks/${TASK_ID}`)).toBe(true)

    // Reopening shows the title that was written, not the one the link was for.
    await link.click()
    await expect(editor).toBeVisible()
    await expect(editor.locator('#task-detail-name')).toHaveValue('Reply to Ivo with the draft')
  })

  test('a refused save keeps the editor open and Escape does not lose the draft', async ({ page }) => {
    const link = await openTaskLink(page)
    await page.route('**/api/tasks/*', async route => {
      if (route.request().method() !== 'PATCH') return route.fallback()
      await route.fulfill({
        status: 409,
        json: { error: { code: 'task_revision_conflict', message: 'that task changed on disk', retryable: true } },
      })
    })

    await link.click()
    const editor = page.locator(EDITOR)
    await expect(editor).toBeVisible()
    await editor.locator('#task-detail-name').fill('A title that cannot be saved')
    await page.keyboard.press('Escape')

    // The close was refused: the route, the editor and the typed title stay.
    await expect(editor).toBeVisible()
    await expect(editor.locator('#task-detail-name')).toHaveValue('A title that cannot be saved')
    await expect(page).toHaveURL(/\/chat\/alpha-chat-1$/)
  })

  test('a modified click still opens the task page in another tab', async ({ page, context }) => {
    const link = await openTaskLink(page)

    const [taskPage] = await Promise.all([
      context.waitForEvent('page'),
      link.click({ modifiers: ['ControlOrMeta'] }),
    ])
    await taskPage.waitForLoadState()
    await expect(taskPage).toHaveURL(new RegExp(`/tasks/${TASK_ID}$`))
    // The chat stays where it was, with no overlay over it.
    await expect(page).toHaveURL(/\/chat\/alpha-chat-1$/)
    await expect(page.locator(EDITOR)).toBeHidden()
  })

  test('a direct /tasks/<id> still opens the board route editor', async ({ page }) => {
    await boot(page, `/tasks/${TASK_ID}`, EDITOR)

    // The board keeps its own route and its own editor: this is not the
    // overlay, and the address is the one the user navigated to.
    await expect(page).toHaveURL(new RegExp(`/tasks/${TASK_ID}$`))
    await expect(page.locator('.task-lanes')).toBeVisible()
    await expect(page.locator(EDITOR).locator('#task-detail-name')).toHaveValue(TITLE)
  })
})

test.describe('the task editor over a chat on a phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true })

  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `task-link-phone-${testInfo.workerIndex}-${testInfo.testId}`)
  })

  test('meets the 44px touch minimum', async ({ page }) => {
    const link = await openTaskLink(page)
    await link.click()
    await expect(page.locator(EDITOR)).toBeVisible()
    // Measure only once the sheet's styles (including the coarse-pointer sizes)
    // have applied: a size read before them is the fine-pointer one.
    await quiesce(page)

    const small = await page.evaluate((selector) => {
      const selectorList = 'button, a[href], [role=button], [role=switch], [role=tab], [role=checkbox], '
        + '[role=radio], [role=menuitem], [role=option], input:not([type=hidden]), select, summary'
      const bad: string[] = []
      for (const raw of Array.from(document.querySelectorAll(`${selector} :is(${selectorList})`))) {
        const el = raw instanceof HTMLInputElement && (raw.type === 'checkbox' || raw.type === 'radio')
          ? (raw.closest('label') ?? raw)
          : raw
        const parent = el.parentElement
        // An inline prose link sits among sentence text (WCAG 2.5.8 exception).
        if (el.tagName === 'A' && parent && ['P', 'LI'].includes(parent.tagName)
          && Array.from(parent.childNodes).some(node => node.nodeType === Node.TEXT_NODE && (node.textContent ?? '').trim() !== '')) {
          continue
        }
        const cs = getComputedStyle(el)
        if (cs.visibility === 'hidden') continue
        const rect = el.getBoundingClientRect()
        if (rect.width === 0 || rect.height === 0) continue
        if (rect.height < 43.5 || rect.width < 43.5) {
          bad.push(`"${(el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 40)}" ${Math.round(rect.height)}×${Math.round(rect.width)}`)
        }
      }
      return bad
    }, EDITOR)
    expect(small).toEqual([])
  })
})
