import { expect, test, type Locator } from '@playwright/test'
import { COMPOSER, boot, horizontalOverflow, isolate, openChatDetails, seedInbox } from '../support/app'

/**
 * Why this is a browser test and not a vitest one.
 *
 * jsdom has no layout engine: every `getBoundingClientRect()` is 0x0 and
 * `scrollWidth` is always 0, so none of the traps web/README.md documents — a
 * flex child with an unbreakable string widening its parent past the viewport,
 * a tap target that renders smaller than `--touch: 44px` — can be detected by
 * mounting a component. The fixture deliberately ships a project whose name is
 * one long unbreakable token for exactly this reason.
 */
const PHONE = { width: 390, height: 844 }

test.use({ viewport: PHONE, isMobile: true, hasTouch: true })

const QUESTION_OPTIONS = [
  { label: 'Recommended staged rollout', description: 'Start with a small pilot group, gather feedback, validate system behavior and expand the rollout to everyone only after the pilot is stable.' },
  { label: 'All at once', description: 'Deploy to every user immediately and monitor the results with the support team available to respond to any problems.' },
  { label: 'Wait for more evidence', description: 'Delay the change until we have enough data to understand the operational impact and agree on the best option.' },
]
const QUESTION = { questions: [{ id: 'rollout', question: 'Which rollout should we choose?', header: 'Approach', isOther: true, required: true, type: 'string', multiSelect: false, options: QUESTION_OPTIONS }] }

async function seedQuestion(page: import('@playwright/test').Page, question = QUESTION) {
  await seedInbox(page, { chats: { 'alpha-chat-1': { pending_question: JSON.stringify(question) } } })
}

async function injectQuestion(page: import('@playwright/test').Page, question = QUESTION) {
  const response = await page.request.post('/__fixture__/chat-frame', { data: {
    chat_id: 'alpha-chat-1',
    frames: [{ type: 'tool_use', tool_name: 'AskUserQuestion', request_id: 'question-2601', session_id: 'session-2601', tool_input: JSON.stringify(question) }],
  } })
  expect(response.ok()).toBe(true)
}

async function expectQuestionSubmitVisible(page: import('@playwright/test').Page) {
  const card = page.locator('.question-form-card')
  const body = page.getByRole('region', { name: 'Question details' })
  const button = card.getByRole('button', { name: 'Send answer' })
  await expect(card).toBeVisible()
  await expect(async () => {
    const result = await page.evaluate(() => {
      const card = document.querySelector('.question-form-card')!
      const body = card.querySelector('.question-form-body')!
      const button = card.querySelector('.question-card-actions .primary')!
      const rect = (el: Element) => el.getBoundingClientRect()
      const c = rect(card), b = rect(button), v = window.visualViewport
      const left = v?.offsetLeft ?? 0, top = v?.offsetTop ?? 0
      const right = left + (v?.width ?? innerWidth), bottom = top + (v?.height ?? innerHeight)
      // Stay inside the painted button surface; a 2px inset falls in the
      // rounded transparent corners and correctly hit-tests its parent.
      const points = [[b.left + b.width / 2, b.top + b.height / 2], [b.left + 10, b.top + 10], [b.right - 10, b.top + 10], [b.left + 10, b.bottom - 10], [b.right - 10, b.bottom - 10]]
      const hits = points.map(([x, y]) => { const hit = document.elementFromPoint(x, y); return hit === button || button.contains(hit) })
      const cancel = card.querySelector('.question-card-actions .btn-sm:not(.primary)')!
      return { visible: b.left >= c.left - 1 && b.right <= c.right + 1 && b.top >= c.top - 1 && b.bottom <= c.bottom + 1 && b.left >= left - 1 && b.right <= right + 1 && b.top >= top - 1 && b.bottom <= bottom + 1 && hits.every(Boolean), hits, card: c.toJSON(), button: b.toJSON(), viewport: { left, top, right, bottom }, bodyHeight: body.clientHeight, bodyWidth: body.clientWidth, bodyScrollHeight: body.scrollHeight, bodyScrollTop: body.scrollTop, buttonWidth: b.width, buttonHeight: b.height, cancelWidth: rect(cancel).width, cancelHeight: rect(cancel).height, composer: rect(document.querySelector('textarea.chat-input')!), composerBar: rect(document.querySelector('.composer-bar')!), viewportBottom: bottom }
    })
    expect(result.visible, `Send answer must stay visible and hit-testable: ${JSON.stringify(result)}`).toBe(true)
    expect(result.bodyHeight).toBeGreaterThanOrEqual(44)
    expect(result.bodyWidth).toBeGreaterThan(0)
    expect(result.buttonWidth).toBeGreaterThanOrEqual(44)
    expect(result.buttonHeight).toBeGreaterThanOrEqual(44)
    expect(result.cancelWidth).toBeGreaterThanOrEqual(44)
    expect(result.cancelHeight).toBeGreaterThanOrEqual(44)
    expect(result.composer.top).toBeGreaterThanOrEqual(-1)
    expect(result.composer.bottom).toBeLessThanOrEqual(result.viewportBottom + 1)
    // The whole composer, send/stop included, stays on screen (#2774).
    expect(result.composerBar.bottom).toBeLessThanOrEqual(result.viewportBottom + 1)
  }).toPass({ timeout: 5_000 })
  await expect(card).toBeVisible()
  await expect(body).toBeVisible()
  await expect(button).toBeVisible()
}

async function expectQuestionFooterFocus(control: Locator) {
  await expect(control).toBeFocused()
  const style = await control.evaluate(el => {
    const computed = getComputedStyle(el)
    const b = el.getBoundingClientRect(), cardEl = el.closest('.question-card')!, card = cardEl.getBoundingClientRect(), cs = getComputedStyle(cardEl)
    const extent = parseFloat(computed.outlineWidth) + parseFloat(computed.outlineOffset)
    const visualViewport = window.visualViewport
    const viewportLeft = visualViewport?.offsetLeft ?? 0, viewportTop = visualViewport?.offsetTop ?? 0
    return {
      outlineStyle: computed.outlineStyle,
      outlineWidth: computed.outlineWidth,
      outlineColor: computed.outlineColor,
      outlineOffset: computed.outlineOffset,
      focus: el === document.activeElement,
      focusVisible: el.matches(':focus-visible'),
      bounds: { left: b.left - extent, right: b.right + extent, top: b.top - extent, bottom: b.bottom + extent },
      paddingBox: { left: card.left + parseFloat(cs.borderLeftWidth), right: card.right - parseFloat(cs.borderRightWidth), top: card.top + parseFloat(cs.borderTopWidth), bottom: card.bottom - parseFloat(cs.borderBottomWidth) },
      viewport: { left: viewportLeft, right: viewportLeft + (visualViewport?.width ?? innerWidth), top: viewportTop, bottom: viewportTop + (visualViewport?.height ?? innerHeight) },
    }
  })
  expect(style.outlineStyle, JSON.stringify(style)).toBe('solid')
  expect(parseFloat(style.outlineWidth)).toBeGreaterThanOrEqual(2)
  expect(style.outlineColor).not.toBe('rgba(0, 0, 0, 0)')
  expect(parseFloat(style.outlineOffset)).toBeGreaterThanOrEqual(2)
  expect(style.focus).toBe(true)
  expect(style.focusVisible).toBe(true)
  expect(style.bounds.left).toBeGreaterThanOrEqual(style.paddingBox.left - 1)
  expect(style.bounds.right).toBeLessThanOrEqual(style.paddingBox.right + 1)
  expect(style.bounds.top).toBeGreaterThanOrEqual(style.paddingBox.top - 1)
  expect(style.bounds.bottom).toBeLessThanOrEqual(style.paddingBox.bottom + 1)
  expect(style.bounds.left).toBeGreaterThanOrEqual(style.viewport.left - 1)
  expect(style.bounds.right).toBeLessThanOrEqual(style.viewport.right + 1)
  expect(style.bounds.top).toBeGreaterThanOrEqual(style.viewport.top - 1)
  expect(style.bounds.bottom).toBeLessThanOrEqual(style.viewport.bottom + 1)
}

async function expectQuestionTargetsReachable(page: import('@playwright/test').Page) {
  const body = page.getByRole('region', { name: 'Question details' })
  for (const target of [body.locator('.question-option').last(), body.locator('.question-other').last()]) {
    await target.evaluate(el => el.scrollIntoView({ block: 'nearest' }))
    const geometry = await target.evaluate(el => {
      const target = el.getBoundingClientRect(), body = el.closest('.question-form-body')!.getBoundingClientRect()
      const v = window.visualViewport, left = v?.offsetLeft ?? 0, top = v?.offsetTop ?? 0
      const right = left + (v?.width ?? innerWidth), bottom = top + (v?.height ?? innerHeight)
      return { target: target.toJSON(), body: body.toJSON(), viewport: { left, top, right, bottom }, visible: target.width > 0 && target.height > 0 && target.left >= body.left - 1 && target.right <= body.right + 1 && target.top >= body.top - 1 && target.bottom <= body.bottom + 1 && target.left >= left - 1 && target.right <= right + 1 && target.top >= top - 1 && target.bottom <= bottom + 1 }
    })
    expect(geometry.visible, `Question field must be reachable by scrolling the details region: ${JSON.stringify(geometry)}`).toBe(true)
  }
}

test.describe('narrow viewport', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `narrow-${testInfo.workerIndex}`)
  })

  test('the home view does not scroll sideways at 390px', async ({ page }) => {
    await boot(page)

    const { overflow, culprits } = await horizontalOverflow(page)
    expect(
      overflow,
      `document scrolls ${overflow}px past the viewport; widest unclipped: ${JSON.stringify(culprits)}`,
    ).toBeLessThanOrEqual(0)

    // The workbench command canvas must keep its controls inside the surface at
    // phone width: the project chip and the New action are the two that can
    // push past the shell.
    const form = await page.locator('.home-intake-form').boundingBox()
    expect(form).not.toBeNull()
    // The file input is hidden (the paperclip button opens it), so it has no box.
    const controls = page.locator('.home-intake-form input:not([type=file]), .home-intake-form textarea, .home-intake-form select, .home-intake-form button')
    for (const control of await controls.all()) {
      const box = await control.boundingBox()
      expect(box).not.toBeNull()
      expect(box!.x).toBeGreaterThanOrEqual(form!.x - 1)
      expect(box!.x + box!.width).toBeLessThanOrEqual(form!.x + form!.width + 1)
    }
  })

  test('the usage page does not scroll sideways at 390px', async ({ page }) => {
    await boot(page, '/stats', '.usage-heat')

    const { overflow, culprits } = await horizontalOverflow(page)
    expect(
      overflow,
      `document scrolls ${overflow}px past the viewport; widest unclipped: ${JSON.stringify(culprits)}`,
    ).toBeLessThanOrEqual(0)

    // The heatmap is 182 day cells; its strip scrolls inside its own card.
    await expect(page.locator('.usage-heat-cell:not(.usage-heat-cell--pad):not(.usage-legend-swatch)')).toHaveCount(182)
    const strip = await page.locator('.usage-heat').boundingBox()
    const card = await page.locator('.usage-card:has(.usage-heat)').boundingBox()
    expect(strip).not.toBeNull()
    expect(card).not.toBeNull()
    expect(strip!.width).toBeLessThanOrEqual(card!.width)
  })

  // On a phone every brand-row control is a 44px touch box; the bell used to
  // size itself to 44px on top of its touch padding, which pushed the collapse
  // control past the 320px drawer's edge.
  test('the drawer brand row keeps the collapse control inside the drawer', async ({ page }) => {
    await boot(page)
    const open = page.getByRole('button', { name: /open sidebar|menu/i }).first()
    if (await open.isVisible().catch(() => false)) await open.click()
    const row = page.locator('.sidebar-brand-row')
    await expect(row).toBeVisible()
    // The drawer slides in, so measure once it has settled.
    await expect(async () => {
      const aside = (await page.locator('aside.sidebar').boundingBox())!
      const rowBox = (await row.boundingBox())!
      const toggle = (await row.locator('.toggle-btn').boundingBox())!
      expect(aside.x).toBe(0)
      expect(toggle.width).toBeGreaterThanOrEqual(44)
      expect(toggle.x + toggle.width).toBeLessThanOrEqual(aside.x + aside.width)
      // The visible 30px face ends on the row's edge, where the cards below end.
      expect(toggle.x + toggle.width - 7).toBeLessThanOrEqual(rowBox.x + rowBox.width + 1)
    }).toPass({ timeout: 5_000 })
  })

  test('an expired Google account row keeps its name and email clear of the actions', async ({ page }) => {
    const profile = {
      name: 'alpha', label: 'Personal', purpose: '', examples: [], configured: true,
      credentials_present: true, client_secret_present: true, config_dir: '/synthetic/alpha',
      workspaces: ['alpha'], setup_command: '', headless_auth_command: '',
      email: 'a-long-address-for-a-phone@example.com', token_valid: false,
      token_error: 'Token has been expired or revoked.', needs_relogin: true, loopback_eligible: true,
    }
    await page.route('**/api/integrations/gws*', route => route.fulfill({
      json: { installed: true, binary_path: '/usr/local/bin/gws', cli_available: true, profiles: [profile] },
    }))
    await boot(page, '/settings/workspace', '.settings-main .section-title')
    const head = page.locator('.gws-account .set-row-head')
    await expect(head).toContainText('Sign-in expired')
    const main = (await head.locator('.set-row-main').boundingBox())!
    const actions = (await head.locator('.set-row-actions').boundingBox())!
    // The actions wrap below the name instead of squeezing it to "Pe...".
    expect(actions.y).toBeGreaterThanOrEqual(main.y + main.height - 1)
    const title = head.locator('.set-row-title')
    expect(await title.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true)
  })

  test('an open chat does not scroll sideways at 390px', async ({ page }) => {
    await boot(page, '/chat/alpha-chat-1', COMPOSER)

    const { overflow, culprits } = await horizontalOverflow(page)
    expect(
      overflow,
      `document scrolls ${overflow}px past the viewport; widest unclipped: ${JSON.stringify(culprits)}`,
    ).toBeLessThanOrEqual(0)
  })

  test('question submission stays visible with long options in portrait and landscape', async ({ page }, testInfo) => {
    await seedQuestion(page)
    await boot(page, '/chat/alpha-chat-1', COMPOSER)
    await injectQuestion(page)
    await expectQuestionSubmitVisible(page)
    await page.screenshot({ path: testInfo.outputPath('question-portrait.png') })
    await page.getByRole('button', { name: /Recommended staged rollout/ }).click()
    await expectQuestionSubmitVisible(page)
    await page.getByRole('region', { name: 'Question details' }).evaluate(el => { el.scrollTop = el.scrollHeight })
    await expectQuestionSubmitVisible(page)
    await page.locator('.question-option').last().click()
    await expectQuestionSubmitVisible(page)
    await page.setViewportSize({ width: 844, height: 390 })
    await expectQuestionSubmitVisible(page)
    await page.screenshot({ path: testInfo.outputPath('question-landscape.png') })
    const landscapeBody = page.getByRole('region', { name: 'Question details' })
    const landscapeScroll = await landscapeBody.evaluate(el => {
      el.scrollTop = el.scrollHeight
      return { clientHeight: el.clientHeight, scrollHeight: el.scrollHeight, scrollTop: el.scrollTop }
    })
    expect(landscapeScroll.scrollHeight).toBeGreaterThan(landscapeScroll.clientHeight)
    expect(landscapeScroll.scrollTop).toBeGreaterThan(0)
    await expectQuestionTargetsReachable(page)
    await expectQuestionSubmitVisible(page)
    await page.setViewportSize({ width: 390, height: 500 })
    await expectQuestionSubmitVisible(page)
    // lib/viewport.ts re-measures on resize and again 50/200/500ms later
    // (RESETTLE_DELAYS_MS); a late re-measure removed the hand-set keyboard
    // state below (#2775). Let the resize land and the settling pass end first.
    await expect.poll(() => page.evaluate(() => innerHeight)).toBe(500)
    await page.waitForTimeout(600)
    await page.evaluate(() => {
      document.documentElement.style.setProperty('--app-h', '280px')
      document.documentElement.classList.add('keyboard-open')
    })
    const keyboardLayout = await page.evaluate(() => {
      const root = document.documentElement, card = document.querySelector('.question-form-card')!
      return { className: root.className, appHeight: getComputedStyle(root).getPropertyValue('--app-h'), selectorMatches: card.matches(':root.keyboard-open .question-form-card'), maxHeight: getComputedStyle(card).maxHeight }
    })
    expect(keyboardLayout, JSON.stringify(keyboardLayout)).toMatchObject({ appHeight: '280px', selectorMatches: true, maxHeight: '168px' })
    await expectQuestionSubmitVisible(page)
    await page.evaluate(() => {
      document.documentElement.classList.remove('keyboard-open')
      document.documentElement.style.removeProperty('--app-h')
    })
    await page.setViewportSize({ width: 1440, height: 900 })
    await expectQuestionSubmitVisible(page)
    await page.screenshot({ path: testInfo.outputPath('question-desktop.png') })
    await page.setViewportSize(PHONE)
    await page.emulateMedia({ colorScheme: 'dark' })
    await expectQuestionSubmitVisible(page)
    await page.screenshot({ path: testInfo.outputPath('question-portrait-dark.png') })
  })

  test('question submission stays visible with multiple blocks and enlarged text', async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('ciao-font-scale', '1.5'))
    const appearance = await page.request.patch('/api/settings/appearance', { data: { font_scale: 1.5 } })
    expect(appearance.ok()).toBe(true)
    const twoQuestions = { questions: [QUESTION.questions[0], { ...QUESTION.questions[0], id: 'timing', question: 'Which rollout timing should we choose?', header: 'Timing' }] }
    await seedQuestion(page, twoQuestions)
    await boot(page, '/chat/alpha-chat-1', COMPOSER)
    await injectQuestion(page, twoQuestions)
    await expect.poll(() => page.locator('html').evaluate(el => getComputedStyle(el).getPropertyValue('--font-scale').trim())).toBe('1.5')
    await expectQuestionSubmitVisible(page)
    await page.locator('.question-block').nth(0).getByRole('button', { name: /Recommended staged rollout/ }).click()
    await expectQuestionSubmitVisible(page)
    await expect(page.getByRole('button', { name: 'Send answer' })).toBeDisabled()
    await page.locator('.question-block').nth(1).getByRole('button', { name: /Recommended staged rollout/ }).click()
    await expect(page.getByRole('button', { name: 'Send answer' })).toBeEnabled()
    await expectQuestionSubmitVisible(page)
    const body = page.getByRole('region', { name: 'Question details' })
    const scroll = await body.evaluate(el => {
      el.scrollTop = el.scrollHeight
      return { clientHeight: el.clientHeight, scrollHeight: el.scrollHeight, scrollTop: el.scrollTop }
    })
    expect(scroll.scrollHeight).toBeGreaterThan(scroll.clientHeight)
    expect(scroll.scrollTop).toBeGreaterThan(0)
    await expectQuestionTargetsReachable(page)
    await expectQuestionSubmitVisible(page)
    await page.setViewportSize({ width: 844, height: 390 })
    await expectQuestionSubmitVisible(page)
    const landscapeScroll = await body.evaluate(el => {
      el.scrollTop = el.scrollHeight
      return { clientHeight: el.clientHeight, scrollHeight: el.scrollHeight, scrollTop: el.scrollTop }
    })
    expect(landscapeScroll.scrollHeight).toBeGreaterThan(landscapeScroll.clientHeight)
    expect(landscapeScroll.scrollTop).toBeGreaterThan(0)
    await expectQuestionTargetsReachable(page)
    await expectQuestionSubmitVisible(page)
  })

  test('a short pane folds the composer under a question and closes its model picker', async ({ page }) => {
    await page.setViewportSize({ width: 844, height: 390 })
    await boot(page, '/chat/alpha-chat-1', COMPOSER)
    await page.locator('.composer-model .model-picker-summary').click()
    await expect(page.locator('.model-selector')).toBeVisible()
    await injectQuestion(page)
    await expect(page.locator('.question-form-card')).toBeVisible()
    await expect(page.locator('.model-selector')).toHaveCount(0)
    await expect(page.locator('.composer-model')).toBeHidden()
    await expect(page.locator('.image-btn')).toBeHidden()
    await expect(page.locator('.send-btn')).toBeInViewport({ ratio: 1 })
  })

  test('a live question preserves composer focus and keyboard entry', async ({ page }) => {
    await boot(page, '/chat/alpha-chat-1', COMPOSER)
    const composer = page.locator('textarea')
    await composer.focus()
    await composer.fill('Draft remains')
    await page.request.post('/__fixture__/chat-frame', { data: { chat_id: 'alpha-chat-1', frames: [{ type: 'tool_use', tool_name: 'AskUserQuestion', request_id: 'question-2601', session_id: 'session-2601', tool_input: JSON.stringify(QUESTION) }] } })
    await expect(page.locator('.question-form-card')).toBeVisible()
    await expect(composer).toBeFocused()
    await expect(composer).toHaveValue('Draft remains')
    const other = page.locator('.question-other')
    await other.focus()
    await page.keyboard.type('123')
    await expect(other).toHaveValue('123')
    await expect(page.locator('.question-option.selected')).toHaveCount(0)
    await page.keyboard.press('Tab')
    const cancel = page.getByRole('button', { name: 'Cancel' })
    await expect(cancel).toBeFocused()
    await expectQuestionFooterFocus(cancel)
    await page.keyboard.press('Tab')
    const submit = page.getByRole('button', { name: 'Send answer' })
    await expect(submit).toBeFocused()
    await expectQuestionFooterFocus(submit)
    await page.locator('.question-option').first().focus()
    await page.keyboard.press('Shift+Tab')
    const body = page.getByRole('region', { name: 'Question details' })
    await expect(body).toBeFocused()
    await expect.poll(() => body.evaluate(el => el.matches(':focus-visible'))).toBe(true)
    const bodyStyle = await body.evaluate(el => getComputedStyle(el))
    expect(bodyStyle.outlineStyle).toBe('solid')
    expect(parseFloat(bodyStyle.outlineWidth)).toBeGreaterThanOrEqual(2)
    expect(bodyStyle.outlineColor).not.toBe('rgba(0, 0, 0, 0)')
    expect(bodyStyle.outlineOffset).toBe('-2px')
    await page.keyboard.press('Tab')
    await expect(page.locator('.question-option').first()).toBeFocused()
    await expectQuestionSubmitVisible(page)
  })

  test('the Repository section fits the Chat details drawer at 390px', async ({ page }) => {
    await boot(page, '/chat/alpha-chat-1', COMPOSER)
    // The header's code badge is hidden on phones; Chat details opens from the ⓘ button.
    await openChatDetails(page)
    const rail = page.locator('#chat-work-rail')
    await expect(rail).toBeVisible()
    const sideways = await rail.evaluate((el) => el.scrollWidth - el.clientWidth)
    expect(sideways, 'Chat details scrolls sideways').toBe(0)
    const row = page.locator('.agent-context-repo')
    await expect(row).toContainText('alpha-app/AGENTS.md')
    await expect(row).toContainText('tokens')
    const { overflow, culprits } = await horizontalOverflow(page)
    expect(
      overflow,
      `document scrolls ${overflow}px past the viewport; widest unclipped: ${JSON.stringify(culprits)}`,
    ).toBeLessThanOrEqual(0)
  })

  test('a reply shows its actions on a phone and keeps them inside its turn', async ({ page }) => {
    // There is no hover on touch, so the action row is simply visible there.
    // It is always laid out, so it must fit inside its own turn: the sent
    // comment-reference card in the next turn is the surface a bleeding
    // footer used to clash with (#968).
    await boot(page, '/chat/alpha-chat-1', COMPOSER)
    await page.evaluate(() => fetch('/__fixture__/transcript', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ shape: 'comment' }),
    }))
    await page.reload()
    await page.waitForSelector('.message-wrap.assistant .message-actions')
    await page.waitForSelector('user-comment-reference')

    const measured = await page.evaluate(() => {
      const wrap = document.querySelector('.message-wrap.assistant')!
      const actions = wrap.querySelector('.message-actions')!
      const next = wrap.nextElementSibling as HTMLElement
      return {
        opacity: getComputedStyle(actions).opacity,
        actionsBottom: actions.getBoundingClientRect().bottom,
        nextTop: next.getBoundingClientRect().top,
      }
    })
    expect(measured.opacity, 'the action row is hidden on touch').toBe('1')
    expect(measured.actionsBottom - measured.nextTop, 'the action row crosses the next turn').toBeLessThanOrEqual(0)
  })

  test('turn facts stay on one line and scroll sideways without a scrollbar at 390px', async ({ page }) => {
    await boot(page, '/chat/alpha-chat-1', COMPOSER)
    await page.evaluate(() => fetch('/__fixture__/transcript', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ shape: 'turn-facts' }),
    }))
    await page.reload()
    await page.waitForSelector('.message-wrap.assistant .message-meta')

    const measured = await page.evaluate(() => {
      const replies = document.querySelectorAll('.message-wrap.assistant')
      const wrap = replies[replies.length - 1] as HTMLElement
      const actions = wrap.querySelector('.message-actions') as HTMLElement
      const meta = actions.querySelector('.message-meta') as HTMLElement
      const copy = actions.querySelector('.message-action-btn') as HTMLElement
      const first = meta.firstElementChild as HTMLElement
      const last = meta.lastElementChild as HTMLElement
      const wrapBox = wrap.getBoundingClientRect()
      const actionsBox = actions.getBoundingClientRect()
      const metaBox = meta.getBoundingClientRect()
      const copyBox = copy.getBoundingClientRect()
      const style = getComputedStyle(meta)
      // The reply's action row pulls 8px left so the icon's own padding lines
      // its glyph up with the prose; that negative margin is the only part of
      // the row allowed outside the turn.
      const actionsIndent = Math.abs(parseFloat(getComputedStyle(actions).marginLeft) || 0)

      meta.scrollLeft = 0
      const firstLeft = first.getBoundingClientRect().left - meta.getBoundingClientRect().left
      meta.scrollLeft = meta.scrollWidth
      const lastRight = last.getBoundingClientRect().right - meta.getBoundingClientRect().right

      return {
        flexWrap: style.flexWrap,
        whiteSpace: style.whiteSpace,
        overflowX: style.overflowX,
        scrollbarWidth: style.scrollbarWidth,
        centreDelta: Math.abs(
          (metaBox.top + metaBox.bottom) / 2 - (copyBox.top + copyBox.bottom) / 2,
        ),
        actionsInsideWrap:
          actionsBox.left >= wrapBox.left - actionsIndent - 1 &&
          actionsBox.right <= wrapBox.right + 1,
        actionsWidth: actionsBox.width,
        wrapWidth: wrapBox.width,
        metaInsideActions: metaBox.left >= actionsBox.left - 1 && metaBox.right <= actionsBox.right + 1,
        scrollWidth: meta.scrollWidth,
        clientWidth: meta.clientWidth,
        firstLeft,
        lastRight,
      }
    })

    expect(measured.flexWrap, 'the facts wrap onto their own line').toBe('nowrap')
    expect(measured.whiteSpace).toBe('nowrap')
    expect(measured.overflowX).toBe('auto')
    // The strip still scrolls; it just must not draw a bar. `scrollbar-width`
    // is the computed value the engine uses, and `::-webkit-scrollbar` covers
    // the engines that ignore it.
    expect(measured.scrollbarWidth, 'the facts strip draws a scrollbar').toBe('none')
    expect(measured.centreDelta, 'the facts do not share the action row').toBeLessThanOrEqual(1)
    expect(
      measured.actionsInsideWrap,
      `the action row (${measured.actionsWidth}px) is not confined to its turn (${measured.wrapWidth}px)`,
    ).toBe(true)
    expect(measured.metaInsideActions, 'the facts strip overflows its action row').toBe(true)
    expect(measured.scrollWidth, 'the facts are not overflowing, so the strip does not scroll')
      .toBeGreaterThan(measured.clientWidth)
    expect(measured.firstLeft, 'the first fact is clipped at the start of the strip').toBeGreaterThanOrEqual(-1)
    expect(measured.lastRight, 'the last fact is clipped at the end of the strip').toBeLessThanOrEqual(1)

    const { overflow, culprits } = await horizontalOverflow(page)
    expect(
      overflow,
      `document scrolls ${overflow}px past the viewport; widest unclipped: ${JSON.stringify(culprits)}`,
    ).toBeLessThanOrEqual(0)
  })

  test('icon-only controls still hit the 44px touch minimum', async ({ page }) => {
    // The open chat is where the icon-only controls live (hamburger, close,
    // model picker, archive); the home view has one.
    await boot(page, '/chat/alpha-chat-1', COMPOSER)

    // `.btn-icon` and `.touch-hit` are the two utilities App.vue declares as
    // enforcing `--touch: 44px`. Measuring them in a real browser is the only
    // way to know the rule survived a component-level override.
    const boxes = await page.locator('.btn-icon:visible, .touch-hit:visible').evaluateAll((els) =>
      els.map((el) => {
        const r = el.getBoundingClientRect()
        return { cls: String(el.className).slice(0, 60), w: Math.round(r.width), h: Math.round(r.height) }
      }),
    )
    // Without this the test would keep passing if a refactor renamed the
    // utilities and the selector started matching nothing at all. At 390px the
    // open chat shows two of them (☰ and the ⋯ menu); Back, Chat details, Archive
    // and the bell sit in the ⋯ menu or are hidden there.
    expect(boxes.length, 'no touch targets were measured').toBeGreaterThanOrEqual(2)
    expect(boxes.some((box) => box.cls.includes('chat-more-btn')), 'the ⋯ trigger is not measured').toBe(true)

    const small = boxes.filter((box) => box.w < 44 || box.h < 44)
    expect(small, `controls below the 44px touch minimum: ${JSON.stringify(small)}`).toEqual([])
  })

  test('the home composer controls meet the 44px touch minimum at phone width', async ({ page }) => {
    await boot(page)

    const boxes = await page.locator('.home-intake-form button:visible, .home-intake-form textarea:visible').evaluateAll((els) =>
      els.map((el) => {
        const r = el.getBoundingClientRect()
        return {
          name: (el.getAttribute('aria-label') || el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 60),
          w: Math.round(r.width),
          h: Math.round(r.height),
        }
      }),
    )
    expect(boxes.length, 'no home composer controls were measured').toBeGreaterThanOrEqual(2)
    const small = boxes.filter((box) => box.h < 44)
    expect(small, `home composer controls below the 44px touch minimum: ${JSON.stringify(small)}`).toEqual([])
  })

  test('a memory-insight row fits a phone and opens the pass it stands for', async ({ page }) => {
    // A pass is a real chat, so the surface Home shows for it is the one place
    // the row's own text has to survive a phone: a long conversation title, a
    // status word, and (on one row) a pending question. jsdom has no layout
    // engine, so only a real browser can say the row neither overflows nor
    // drops below the touch minimum.
    await boot(page, undefined, '.home-insights')

    const rows = page.locator('.home-insight-row')
    await expect(rows).toHaveCount(2)
    // Newest conversation first, and named for the conversation — never for the
    // pass's own internal title.
    await expect(rows.nth(0).locator('.home-chat-title'))
      .toHaveText('alpha conversation with a running memory pass')
    await expect(rows.nth(1).locator('.home-chat-title'))
      .toHaveText('alpha conversation waiting on the pass')
    await expect(page.locator('.home-tier--working, .home-tier--unread')).toHaveCount(0)
    await expect(page.getByText('Memory pass · conversation with a running memory pass')).toHaveCount(0)

    for (const row of await rows.all()) {
      const box = await row.boundingBox()
      expect(box).not.toBeNull()
      expect(box!.height, 'an insight row is shorter than the touch minimum').toBeGreaterThanOrEqual(44)
    }

    const { overflow } = await horizontalOverflow(page)
    expect(overflow).toBeLessThanOrEqual(0)

    const open = rows.nth(0).locator('.home-chat-item')
    await expect(open).toHaveAttribute('title', 'Open the memory insight for alpha conversation with a running memory pass')
    await open.click()
    await expect(page).toHaveURL(/\/chat\/alpha-chat-5$/)
  })

  test('a selected memory note opens an actionable sheet on a phone', async ({ page }) => {
    await boot(page, '/memory/map', '.mm-toolbar')
    await page.getByRole('button', { name: 'List', exact: true }).click()
    await page.getByRole('button', { name: 'Open Launch decision' }).click()

    const detail = page.getByRole('dialog', { name: 'Details for Launch decision' })
    await expect(detail).toBeVisible()
    await expect(detail.getByRole('button', { name: 'Close note' })).toBeVisible()
    await detail.getByRole('button', { name: 'Close note' }).click()
    await expect(detail).toBeHidden()
  })

  test('the categories list and its drawer meet the 44px touch minimum', async ({ page }) => {
    // Three controls sit in a row here — the label button, its Built-in/Custom
    // chip and the switch — and the drawer adds the whole form on top, so this
    // is where a narrow pane would quietly drop one below the touch minimum.
    await boot(page, '/memory/categories', '.cat-table-wrap')

    const small = async (selector: string) => {
      const boxes = await page.locator(selector).evaluateAll((els) =>
        els.map((el) => {
          const r = el.getBoundingClientRect()
          return {
            name: (el.getAttribute('aria-label') || el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 60),
            w: Math.round(r.width),
            h: Math.round(r.height),
          }
        }),
      )
      return boxes
    }

    const rows = await small('.cat-row .cat-name-btn:visible, .cat-row .cat-switch:visible')
    expect(rows.length, 'no category row controls were measured').toBeGreaterThanOrEqual(4)
    // Both directions: a 44px-tall 40px-wide switch is under the minimum, so
    // height alone would call it a pass.
    expect(rows.filter((b) => b.w < 44 || b.h < 44), `row controls below 44px: ${JSON.stringify(rows.filter((b) => b.w < 44 || b.h < 44))}`).toEqual([])

    // And the list must not push the document sideways.
    const { overflow, culprits } = await horizontalOverflow(page)
    expect(overflow, `document scrolls ${overflow}px past the viewport; widest: ${JSON.stringify(culprits)}`).toBeLessThanOrEqual(0)

    // The drawer is a bottom sheet at this width, so its own controls are the
    // only thing between a tap and an edit.
    await page.getByRole('button', { name: 'Add category' }).click()
    const drawer = page.getByRole('dialog', { name: 'Add category' })
    await expect(drawer).toBeVisible()
    const fields = await small('.cat-drawer button:visible, .cat-drawer input:visible, .cat-drawer select:visible, .cat-drawer textarea:visible')
    expect(fields.length, 'no drawer controls were measured').toBeGreaterThanOrEqual(6)
    expect(fields.filter((b) => b.h < 44), `drawer controls below 44px: ${JSON.stringify(fields.filter((b) => b.h < 44))}`).toEqual([])
  })
})
