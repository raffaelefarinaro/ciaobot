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
      const app = rect(document.querySelector('#ciao-app')!)
      return { visible: b.left >= c.left - 1 && b.right <= c.right + 1 && b.top >= c.top - 1 && b.bottom <= c.bottom + 1 && b.left >= left - 1 && b.right <= right + 1 && b.top >= top - 1 && b.bottom <= bottom + 1 && hits.every(Boolean), hits, card: c.toJSON(), button: b.toJSON(), viewport: { left, top, right, bottom }, app: app.toJSON(), bodyHeight: body.clientHeight, bodyWidth: body.clientWidth, bodyScrollHeight: body.scrollHeight, bodyScrollTop: body.scrollTop, buttonWidth: b.width, buttonHeight: b.height, cancelWidth: rect(cancel).width, cancelHeight: rect(cancel).height, composer: rect(document.querySelector('textarea.chat-input')!), composerBar: rect(document.querySelector('.composer-bar')!), viewportBottom: bottom }
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
    expect(result.card.bottom).toBeLessThanOrEqual(result.app.bottom + 1)
    expect(result.composerBar.bottom).toBeLessThanOrEqual(result.app.bottom + 1)
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

async function expectKeyboardQuestionReachable(page: import('@playwright/test').Page, requireMovement = false) {
  await page.evaluate(() => {
    Object.defineProperty(window.visualViewport!, 'height', { configurable: true, value: 280 })
    window.visualViewport!.dispatchEvent(new Event('resize'))
  })
  await expect.poll(() => page.locator('html').evaluate(el => getComputedStyle(el).getPropertyValue('--app-h').trim())).toBe('280px')
  const composer = page.locator(COMPOSER)
  await composer.focus()
  await expect(composer).toBeFocused()
  const column = page.locator('.chat-column')
  const scrollPositions = [await column.evaluate(el => el.scrollTop)]
  const targets = [page.getByRole('button', { name: 'Send answer' }), page.getByRole('button', { name: 'Cancel' }), page.locator('.question-other').last(), page.locator('.question-option').last()]
  for (const [index, target] of targets.entries()) {
    await page.keyboard.press('Shift+Tab')
    await expect(target).toBeFocused()
    const geometry = await target.evaluate(el => {
      const r = el.getBoundingClientRect(), c = el.closest('.chat-column')!.getBoundingClientRect(), v = window.visualViewport!
      const body = el.closest('.question-form-body')?.getBoundingClientRect()
      const top = Math.max(c.top, v.offsetTop, body?.top ?? -Infinity), bottom = Math.min(c.bottom, v.offsetTop + v.height, body?.bottom ?? Infinity)
      const hit = document.elementFromPoint(r.left + r.width / 2, (Math.max(top, r.top) + Math.min(bottom, r.bottom)) / 2)
      return { top: r.top, bottom: r.bottom, height: r.height, visibleTop: top, visibleBottom: bottom, hit: hit === el || el.contains(hit) }
    })
    expect(geometry.bottom).toBeGreaterThan(geometry.visibleTop)
    expect(geometry.top).toBeLessThan(geometry.visibleBottom)
    expect(geometry.hit, JSON.stringify(geometry)).toBe(true)
    if (geometry.height <= geometry.visibleBottom - geometry.visibleTop) {
      expect(geometry.top).toBeGreaterThanOrEqual(geometry.visibleTop - 1)
      expect(geometry.bottom).toBeLessThanOrEqual(geometry.visibleBottom + 1)
    }
    if (index < 2) await expectQuestionFooterFocus(target)
    scrollPositions.push(await page.locator('.chat-column').evaluate(el => el.scrollTop))
  }
  if (requireMovement) expect(new Set(scrollPositions).size).toBeGreaterThan(1)
  await page.evaluate(() => { delete (window.visualViewport as unknown as { height?: number }).height; window.visualViewport!.dispatchEvent(new Event('resize')) })
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
    await expectKeyboardQuestionReachable(page)
    await page.setViewportSize({ width: 1440, height: 900 })
    await expectQuestionSubmitVisible(page)
    await page.screenshot({ path: testInfo.outputPath('question-desktop.png') })
    await page.setViewportSize(PHONE)
    await page.emulateMedia({ colorScheme: 'dark' })
    await expectQuestionSubmitVisible(page)
    await page.screenshot({ path: testInfo.outputPath('question-portrait-dark.png') })
  })

  for (const font of ['production', 'alternate'] as const) test(`question submission stays visible with multiple blocks and enlarged text (${font === 'alternate' ? 'alternate font and multiline draft' : 'production font'})`, async ({ page }, testInfo) => {
    await page.addInitScript(() => localStorage.setItem('ciao-font-scale', '1.5'))
    const appearance = await page.request.patch('/api/settings/appearance', { data: { font_scale: 1.5 } })
    expect(appearance.ok()).toBe(true)
    const twoQuestions = { questions: [QUESTION.questions[0], { ...QUESTION.questions[0], id: 'timing', question: 'Which rollout timing should we choose?', header: 'Timing' }] }
    await seedQuestion(page, twoQuestions)
    await boot(page, '/chat/alpha-chat-1', COMPOSER)
    if (font === 'alternate') {
      // A real alternate font and multiline draft exercise larger composer metrics.
      await page.addStyleTag({ content: 'textarea.chat-input { font-family: Arial, "Liberation Sans", sans-serif !important; }' })
      await page.locator('textarea.chat-input').fill('A draft with a second line.\nThe textarea grows naturally to fit it.')
    }
    await injectQuestion(page, twoQuestions)
    await expect.poll(() => page.locator('html').evaluate(el => getComputedStyle(el).getPropertyValue('--font-scale').trim())).toBe('1.5')
    await expectQuestionSubmitVisible(page)
    await page.screenshot({ path: testInfo.outputPath('enlarged-portrait.png') })
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
    if (font === 'alternate') {
      const textareaHeight = await page.locator('textarea.chat-input').evaluate(el => el.getBoundingClientRect().height)
      expect(textareaHeight, 'alternate font must preserve the enlarged wrapping trigger').toBeGreaterThanOrEqual(77)
    }
    await expectQuestionSubmitVisible(page)
    await page.screenshot({ path: testInfo.outputPath('enlarged-landscape.png') })
    const landscapeScroll = await body.evaluate(el => {
      el.scrollTop = el.scrollHeight
      return { clientHeight: el.clientHeight, scrollHeight: el.scrollHeight, scrollTop: el.scrollTop }
    })
    expect(landscapeScroll.scrollHeight).toBeGreaterThan(landscapeScroll.clientHeight)
    expect(landscapeScroll.scrollTop).toBeGreaterThan(0)
    await expectQuestionTargetsReachable(page)
    await expectQuestionSubmitVisible(page)
    await page.setViewportSize({ width: 390, height: 500 })
    await expect.poll(() => page.evaluate(() => innerHeight)).toBe(500)
    await page.waitForTimeout(600)
    await expectKeyboardQuestionReachable(page, font === 'alternate')
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


test.describe('question layout under constrained height', () => {
const CHAT = '/chat/alpha-chat-1'
const CHAT_ID = 'alpha-chat-1'
const OPTIONS = Array.from({ length: 6 }, (_, index) => ({
  label: `Option ${index + 1}`,
  description: 'A long option description with enough words to keep the details region scrollable while the answer controls remain reachable.',
}))
const QUESTION = { questions: [
  { id: 'one', question: 'Which option should we choose?', header: 'First', isOther: true, required: true, type: 'string', multiSelect: false, options: OPTIONS },
  { id: 'two', question: 'Which timing should we choose?', header: 'Second', isOther: true, required: true, type: 'string', multiSelect: false, options: OPTIONS },
] }
const TALL_OPTION_QUESTION = { questions: [{
  id: 'long-reading', question: 'Read the full details before choosing?', header: 'Long option', isOther: false,
  required: true, type: 'string', multiSelect: false,
  options: [{ label: 'A detailed proposal', description: 'FIRST-SENTINEL ' + 'This paragraph contains the terms and rationale that the user must be able to read progressively. '.repeat(80) + ' LAST-SENTINEL' }],
}] }


async function mockKeyboardHeight(page: import('@playwright/test').Page): Promise<void> {
  await page.evaluate(() => {
    const viewport = window.visualViewport!
    Object.defineProperty(viewport, 'height', { configurable: true, value: 280 })
    viewport.dispatchEvent(new Event('resize'))
  })
  await expect.poll(() => page.locator('html').evaluate(el => getComputedStyle(el).getPropertyValue('--app-h').trim())).toBe('280px')
  await page.waitForTimeout(600)
}

async function injectQuestion(page: import('@playwright/test').Page, question = QUESTION): Promise<void> {
  const response = await page.request.post('/__fixture__/chat-frame', { data: {
    chat_id: CHAT_ID,
    frames: [{ type: 'tool_use', tool_name: 'AskUserQuestion', request_id: 'prototype-question', session_id: 'prototype-session', tool_input: JSON.stringify(question) }],
  } })
  expect(response.ok()).toBe(true)
}

test('content-driven question layout at 390, 361, and mocked 280 visual viewport', async ({ page }, testInfo) => {
  await isolate(page, `question-layout-${testInfo.workerIndex}`)
  await page.addInitScript(() => localStorage.setItem('ciao-font-scale', '1.5'))
  const appearance = await page.request.patch('/api/settings/appearance', { data: { font_scale: 1.5 } })
  expect(appearance.ok()).toBe(true)
  await seedInbox(page, { chats: { [CHAT_ID]: { pending_question: JSON.stringify(QUESTION) } } })
  await page.setViewportSize({ width: 390, height: 361 })
  await boot(page, CHAT, COMPOSER)
  await injectQuestion(page)
  await page.locator('.question-block').nth(0).locator('.question-option').first().click()
  await page.locator('.question-block').nth(1).locator('.question-option').first().click()

  const composer = page.locator(COMPOSER)
  await composer.fill('A real draft with multiple lines.\nIt grows through normal input handling.')
  const measure = () => page.evaluate(() => {
    const rect = (selector: string) => document.querySelector(selector)!.getBoundingClientRect()
    const column = document.querySelector('.chat-column')!
    const body = document.querySelector('.question-form-body')!
    return {
      app: rect('#ciao-app').toJSON(),
      column: { clientHeight: column.clientHeight, scrollHeight: column.scrollHeight, scrollTop: column.scrollTop, overflowY: getComputedStyle(column).overflowY },
      card: rect('.question-form-card').toJSON(),
      body: { bounds: rect('.question-form-body').toJSON(), clientHeight: body.clientHeight, scrollHeight: body.scrollHeight },
      send: rect('.question-card-actions .primary').toJSON(),
      composer: rect('textarea.chat-input').toJSON(),
    }
  })

  const at361 = await measure()
  expect(at361.app.height).toBe(361)
  expect(at361.column.scrollHeight).toBeGreaterThan(at361.column.clientHeight)
  await composer.focus()
  await expect.poll(() => composer.evaluate(el => el.getBoundingClientRect().bottom <= document.querySelector('#ciao-app')!.getBoundingClientRect().bottom + 1)).toBe(true)
  const composerHit = await composer.evaluate(el => { const r = el.getBoundingClientRect(), hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2); return hit === el })
  expect(composerHit).toBe(true)
  expect(at361.send.bottom, JSON.stringify(at361)).toBeLessThanOrEqual(at361.card.bottom + 1)
  expect(at361.column.overflowY).toBe('auto')

  await page.setViewportSize({ width: 844, height: 390 })
  await page.evaluate(() => window.dispatchEvent(new Event('orientationchange')))
  await expect.poll(() => page.locator('html').evaluate(el => el.classList.contains('keyboard-open'))).toBe(false)
  const at390 = await measure()
  expect(at390.app.height).toBe(390)
  expect(at390.card.bottom).toBeLessThanOrEqual(at390.app.bottom + 1)
  expect(at390.send.bottom).toBeLessThanOrEqual(at390.card.bottom + 1)
  expect(at390.composer.bottom).toBeLessThanOrEqual(at390.app.bottom + 1)

  await page.setViewportSize({ width: 390, height: 361 })
  await page.evaluate(() => window.dispatchEvent(new Event('orientationchange')))
  await expect.poll(() => page.locator('html').evaluate(el => el.classList.contains('keyboard-open'))).toBe(false)
  await composer.focus()
  await mockKeyboardHeight(page)
  await expect(composer).toBeFocused()
  const at280 = await measure()
  expect(at280.body.clientHeight).toBeGreaterThanOrEqual(44)
  expect(at280.send.bottom).toBeLessThanOrEqual(at280.card.bottom + 1)
  expect(at280.column.scrollHeight).toBeGreaterThan(at280.column.clientHeight)

  // Focus traversal itself must reveal the option and fixed answer actions.
  await page.keyboard.press('Shift+Tab')
  const send = page.getByRole('button', { name: 'Send answer' })
  await expect(send).toBeFocused()
  const sendHit = await send.evaluate(el => {
    const r = el.getBoundingClientRect(), hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)
    return hit === el || el.contains(hit)
  })
  expect(sendHit).toBe(true)
  const sendBounds = await measure()
  expect(sendBounds.send.bottom).toBeLessThanOrEqual(sendBounds.app.bottom)

  await page.keyboard.press('Shift+Tab')
  await expect(page.getByRole('button', { name: 'Cancel' })).toBeFocused()
  await page.keyboard.press('Shift+Tab')
  await expect(page.locator('.question-other').last()).toBeFocused()
  await page.keyboard.press('Shift+Tab')
  const lastOption = page.locator('.question-option').last()
  await expect(lastOption).toBeFocused()
  const optionBounds = await lastOption.evaluate(el => {
    const r = el.getBoundingClientRect(), app = document.querySelector('#ciao-app')!.getBoundingClientRect()
    return { top: r.top, bottom: r.bottom, appTop: app.top, appBottom: app.bottom }
  })
  expect(optionBounds.top).toBeGreaterThanOrEqual(optionBounds.appTop)
  expect(optionBounds.bottom).toBeLessThanOrEqual(optionBounds.appBottom)
  expect(at280.composer.bottom, JSON.stringify(at280)).toBeLessThanOrEqual(at280.app.bottom + 1)
})

test('content-driven layout keeps permission actions reachable at keyboard height', async ({ page }, testInfo) => {
  await isolate(page, `question-permission-${testInfo.workerIndex}`)
  const permission = JSON.stringify({ request_id: 'prototype-permission', session_id: '', tool_name: 'Bash', message: 'Run the build?', tool_input: 'npm run build' })
  await seedInbox(page, { chats: { [CHAT_ID]: { pending_permission: permission } } })
  await boot(page, CHAT, COMPOSER)
  await mockKeyboardHeight(page)
  const column = page.locator('.chat-column')
  const approve = page.getByRole('button', { name: 'Approve' })
  await approve.focus()
  await expect(approve).toBeFocused()
  const bounds = await approve.evaluate(el => {
    const r = el.getBoundingClientRect(), app = document.querySelector('#ciao-app')!.getBoundingClientRect()
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)
    return { top: r.top, bottom: r.bottom, appTop: app.top, appBottom: app.bottom, hit: hit === el || el.contains(hit) }
  })
  expect(bounds.top).toBeGreaterThanOrEqual(bounds.appTop)
  expect(bounds.bottom).toBeLessThanOrEqual(bounds.appBottom)
  expect(bounds.hit).toBe(true)
  expect(await column.evaluate(el => el.scrollHeight)).toBeGreaterThan(await column.evaluate(el => el.clientHeight))
})

test('content-driven layout keeps needs-you actions reachable at keyboard height', async ({ page }, testInfo) => {
  await isolate(page, `question-needs-you-${testInfo.workerIndex}`)
  const response = await page.request.post('/__fixture__/chat-action', { data: {
    chat_id: CHAT_ID, kind: 'mcp_auth', params: { server: 'linear', provider: 'opencode' },
  } })
  expect(response.ok()).toBe(true)
  await boot(page, CHAT, COMPOSER)
  await mockKeyboardHeight(page)
  const card = page.locator('.needs-you-card')
  const action = card.getByRole('button', { name: 'Sign in', exact: true })
  await action.focus()
  await expect(action).toBeFocused()
  const bounds = await action.evaluate(el => {
    const r = el.getBoundingClientRect(), app = document.querySelector('#ciao-app')!.getBoundingClientRect()
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)
    return { top: r.top, bottom: r.bottom, appTop: app.top, appBottom: app.bottom, hit: hit === el || el.contains(hit) }
  })
  expect(bounds.top).toBeGreaterThanOrEqual(bounds.appTop)
  expect(bounds.bottom).toBeLessThanOrEqual(bounds.appBottom)
  expect(bounds.hit).toBe(true)
})

test('height shrink does not move the column when focus is in the transcript', async ({ page }, testInfo) => {
  await isolate(page, `question-transcript-focus-${testInfo.workerIndex}`)
  await page.setViewportSize({ width: 390, height: 361 })
  await boot(page, CHAT, COMPOSER)
  const suggestion = page.getByRole('button', { name: 'Review the latest project notes and flag what needs a decision' })
  await suggestion.focus()
  await expect(suggestion).toBeFocused()
  const column = page.locator('.chat-column')
  const before = await column.evaluate(el => el.scrollTop)
  await mockKeyboardHeight(page)
  await expect(suggestion).toBeFocused()
  expect(await column.evaluate(el => el.scrollTop)).toBe(before)
})

test('height shrink keeps the focused question field visible', async ({ page }, testInfo) => {
  await isolate(page, `question-question-field-${testInfo.workerIndex}`)
  await page.setViewportSize({ width: 390, height: 361 })
  await seedInbox(page, { chats: { [CHAT_ID]: { pending_question: JSON.stringify(QUESTION) } } })
  await boot(page, CHAT, COMPOSER)
  await injectQuestion(page)
  const other = page.locator('.question-other').first()
  await other.focus()
  await expect(other).toBeFocused()
  const column = page.locator('.chat-column')
  const before = await column.evaluate(el => el.scrollTop)
  const body = page.locator('.question-form-body')
  const bodyBefore = await body.evaluate(el => el.scrollTop)
  await page.evaluate(() => window.visualViewport!.dispatchEvent(new Event('resize')))
  await page.waitForTimeout(100)
  expect(await body.evaluate(el => el.scrollTop)).toBe(bodyBefore)
  expect(await column.evaluate(el => el.scrollTop)).toBe(before)
  await mockKeyboardHeight(page)
  await expect(other).toBeFocused()
  await expect.poll(() => body.evaluate(el => el.scrollTop)).toBeGreaterThan(bodyBefore)
  const geometry = await other.evaluate(el => {
    const r = el.getBoundingClientRect(), body = el.closest('.question-form-body')!.getBoundingClientRect(), col = el.closest('.chat-column')!.getBoundingClientRect(), v = window.visualViewport!
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)
    return { top: r.top, bottom: r.bottom, left: r.left, right: r.right, minTop: Math.max(body.top, col.top, v.offsetTop), maxBottom: Math.min(body.bottom, col.bottom, v.offsetTop + v.height), minLeft: Math.max(body.left, col.left, v.offsetLeft), maxRight: Math.min(body.right, col.right, v.offsetLeft + v.width), hit: hit === el || el.contains(hit) }
  })
  expect(geometry.top, JSON.stringify(geometry)).toBeGreaterThanOrEqual(geometry.minTop - 1)
  expect(geometry.bottom, JSON.stringify(geometry)).toBeLessThanOrEqual(geometry.maxBottom + 1)
  expect(geometry.left).toBeGreaterThanOrEqual(geometry.minLeft - 1)
  expect(geometry.right).toBeLessThanOrEqual(geometry.maxRight + 1)
  expect(geometry.hit, JSON.stringify(geometry)).toBe(true)
  expect(await column.evaluate(el => el.scrollTop)).toBe(before)
})

for (const viewport of [361, 280]) test(`a question option taller than the ${viewport}px app can be read progressively`, async ({ page }, testInfo) => {
  await isolate(page, `question-tall-option-${viewport}-${testInfo.workerIndex}`)
  await page.setViewportSize({ width: 390, height: 361 })
  await seedInbox(page, { chats: { [CHAT_ID]: { pending_question: JSON.stringify(TALL_OPTION_QUESTION) } } })
  await boot(page, CHAT, COMPOSER)
  await injectQuestion(page, TALL_OPTION_QUESTION)
  if (viewport === 280) await mockKeyboardHeight(page)
  const metrics = await page.evaluate(() => {
    const column = document.querySelector('.chat-column')!
    const card = document.querySelector('.question-form-card')!
    const body = document.querySelector('.question-form-body')!
    const app = document.querySelector('#ciao-app')!.getBoundingClientRect()
    const option = document.querySelector('.question-option')!.getBoundingClientRect()
    const description = document.querySelector('.question-option-desc')!.getBoundingClientRect()
    const actions = document.querySelector('.question-card-actions')!.getBoundingClientRect()
    return {
      appHeight: app.height, optionHeight: option.height, descriptionHeight: description.height,
      columnClient: column.clientHeight, columnScroll: column.scrollHeight,
      cardHeight: card.getBoundingClientRect().height, cardMax: getComputedStyle(card).maxHeight,
      bodyClient: body.clientHeight, bodyScroll: body.scrollHeight,
      headerTop: document.querySelector('.question-card-header')!.getBoundingClientRect().top,
      actionsBottom: actions.bottom, appBottom: app.bottom,
    }
  })
  expect(metrics.optionHeight).toBeGreaterThan(metrics.appHeight)
  expect(metrics.descriptionHeight).toBeGreaterThan(metrics.appHeight)
  expect(metrics.cardHeight).toBeLessThanOrEqual(Number.parseFloat(metrics.cardMax) + 1)
  expect(metrics.bodyScroll).toBeGreaterThan(metrics.bodyClient)
  expect(metrics.headerTop).toBeGreaterThanOrEqual(0)
  expect(metrics.actionsBottom).toBeLessThanOrEqual(metrics.cardHeight + metrics.headerTop + 1)
  const region = page.getByRole('region', { name: 'Question details' })
  await region.focus()
  await expect(region).toBeFocused()
  const sentinelVisible = async (last: boolean) => page.evaluate(last => {
    const description = document.querySelector('.question-option-desc')!
    const text = description.firstChild!, content = text.textContent!
    const needle = last ? 'LAST-SENTINEL' : 'FIRST-SENTINEL'
    const start = content.indexOf(needle), range = document.createRange()
    range.setStart(text, start); range.setEnd(text, start + needle.length)
    const rect = range.getBoundingClientRect(), body = document.querySelector('.question-form-body')!.getBoundingClientRect()
    const column = document.querySelector('.chat-column')!.getBoundingClientRect(), viewport = window.visualViewport!
    const top = Math.max(body.top, column.top, viewport.offsetTop), bottom = Math.min(body.bottom, column.bottom, viewport.offsetTop + viewport.height)
    const left = Math.max(body.left, column.left, viewport.offsetLeft), right = Math.min(body.right, column.right, viewport.offsetLeft + viewport.width)
    return rect.top >= top - 1 && rect.bottom <= bottom + 1 && rect.left >= left - 1 && rect.right <= right + 1
  }, last)
  const bodyBounds = await region.boundingBox()
  await page.mouse.move(bodyBounds!.x + bodyBounds!.width / 2, bodyBounds!.y + bodyBounds!.height / 2)
  await page.keyboard.press('Home')
  for (let i = 0; i < 100 && !(await sentinelVisible(false)); i++) {
    await page.mouse.wheel(0, 8)
    await page.waitForTimeout(30)
  }
  expect(await sentinelVisible(false)).toBe(true)
  await page.keyboard.press('PageDown')
  await expect.poll(() => region.evaluate(el => (el as HTMLElement).scrollTop)).toBeGreaterThan(0)
  await page.keyboard.press('End')
  await expect.poll(() => sentinelVisible(true)).toBe(true)
  for (let i = 0; i < 40 && await region.evaluate(el => el.scrollTop) > 0; i++) {
    await page.mouse.wheel(0, -1200)
    await page.waitForTimeout(20)
  }
  await expect.poll(() => region.evaluate(el => el.scrollTop)).toBe(0)
  for (let i = 0; i < 30 && !(await sentinelVisible(false)); i++) {
    await page.mouse.wheel(0, 8)
    await page.waitForTimeout(30)
  }
  expect(await sentinelVisible(false)).toBe(true)
  const { cardBottom, actionBottom } = await page.evaluate(() => ({
    cardBottom: document.querySelector('.question-form-card')!.getBoundingClientRect().bottom,
    actionBottom: document.querySelector('.question-card-actions')!.getBoundingClientRect().bottom,
  }))
  expect(actionBottom).toBeLessThanOrEqual(cardBottom + 1)
})

test('content-size containment keeps composer pickers inside the app with and without a question', async ({ page }, testInfo) => {
  await isolate(page, `question-picker-edges-${testInfo.workerIndex}`)
  await page.route('**/api/commands**', route => route.fulfill({ json: { commands: [{ name: 'help', description: 'Show help', argument_hint: '', source: 'builtin', path: '' }], skills: [] } }))
  await page.route('**/api/agent-assets', route => route.fulfill({ json: { subagents: [{ name: 'reviewer', description: 'Review code' }] } }))
  for (const scenario of [
    { width: 390, height: 844, question: false },
    { width: 390, height: 844, question: true },
    { width: 1200, height: 800, question: false },
    { width: 1200, height: 800, question: true },
  ]) {
    await page.setViewportSize({ width: scenario.width, height: scenario.height })
    await boot(page, CHAT, COMPOSER)
    if (scenario.question) await injectQuestion(page)
    const composer = page.locator(COMPOSER)
    const checkBounds = async (selector: string) => {
      const result = await page.locator(selector).evaluate(el => {
        const r = el.getBoundingClientRect(), app = document.querySelector('#ciao-app')!.getBoundingClientRect()
        return { top: r.top, bottom: r.bottom, left: r.left, right: r.right, appTop: app.top, appBottom: app.bottom, appLeft: app.left, appRight: app.right }
      })
      expect(result.top, JSON.stringify({ scenario, selector, result })).toBeGreaterThanOrEqual(result.appTop)
      expect(result.bottom, JSON.stringify({ scenario, selector, result })).toBeLessThanOrEqual(result.appBottom)
      expect(result.left, JSON.stringify({ scenario, selector, result })).toBeGreaterThanOrEqual(result.appLeft)
      expect(result.right, JSON.stringify({ scenario, selector, result })).toBeLessThanOrEqual(result.appRight)
    }
    const checkHit = async (selector: string) => {
      const result = await page.locator(selector).first().evaluate(el => {
        const r = el.getBoundingClientRect(), hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)
        return hit === el || el.contains(hit)
      })
      expect(result, `${JSON.stringify(scenario)} ${selector}`).toBe(true)
    }
    await page.locator('.composer-model .model-picker-summary').click()
    await expect(page.locator('.model-selector__popover')).toBeVisible()
    await checkBounds('.model-selector__popover')
    await checkHit('.model-selector__item')
    await page.keyboard.press('Escape')
    await composer.fill('')
    await composer.pressSequentially('/')
    const commands = page.locator('.commands-picker:not(.mention-picker)')
    await expect(commands).toBeVisible()
    await checkBounds('.commands-picker:not(.mention-picker)')
    await checkHit('.commands-picker-row')
    await composer.fill('')
    await composer.pressSequentially('@')
    const mentions = page.locator('.mention-picker')
    await expect(mentions).toBeVisible()
    await checkBounds('.mention-picker')
    await checkHit('.mention-picker-row')
    await page.goto('about:blank')
  }
})

test('focused composer follows live dock layout, keyboard resize reversal and picker hit targets', async ({ page }, testInfo) => {
  await isolate(page, `question-focused-layout-${testInfo.workerIndex}`)
  await page.route('**/api/commands**', route => route.fulfill({ json: { commands: [{ name: 'help', description: 'Show help', argument_hint: '', source: 'builtin', path: '' }], skills: [] } }))
  await page.route('**/api/agent-assets', route => route.fulfill({ json: { subagents: [{ name: 'reviewer', description: 'Review code' }] } }))
  await page.addInitScript(() => localStorage.setItem('ciao-font-scale', '1.5'))
  const appearance = await page.request.patch('/api/settings/appearance', { data: { font_scale: 1.5 } })
  expect(appearance.ok()).toBe(true)
  await page.setViewportSize({ width: 390, height: 361 })
  await boot(page, CHAT, COMPOSER)
  const composer = page.locator(COMPOSER)
  await composer.fill('draft line one\ndraft line two\ndraft line three')
  await composer.focus()
  const panelBefore = await page.locator('.chat-panel').boundingBox()
  await injectQuestion(page)
  await expect(page.locator('.question-form-card')).toBeVisible()
  await expect(composer).toBeFocused()
  await expect(composer).toHaveValue('draft line one\ndraft line two\ndraft line three')
  await expect.poll(() => composer.evaluate(el => {
    const input = el.getBoundingClientRect(), column = document.querySelector('.chat-column')!.getBoundingClientRect()
    return input.bottom <= column.bottom + 1 && input.top >= column.top - 1
  })).toBe(true)
  const afterQuestion = await page.evaluate(() => {
    const column = document.querySelector('.chat-column') as HTMLElement
    const card = document.querySelector('.question-form-card')!
    const actions = document.querySelector('.question-card-actions')!.getBoundingClientRect()
    const cardRect = card.getBoundingClientRect(), col = column.getBoundingClientRect()
    const input = document.querySelector('textarea.chat-input')!.getBoundingClientRect()
    return { scrollTop: column.scrollTop, scrollHeight: column.scrollHeight, clientHeight: column.clientHeight, cardTop: cardRect.top, cardBottom: cardRect.bottom, actionsBottom: actions.bottom, columnTop: col.top, columnBottom: col.bottom, inputTop: input.top, inputBottom: input.bottom }
  })
  const panelAfter = await page.locator('.chat-panel').boundingBox()
  expect(panelAfter!.height).toBe(panelBefore!.height)
  expect(afterQuestion.inputBottom).toBeLessThanOrEqual(afterQuestion.columnBottom + 1)
  expect(afterQuestion.actionsBottom).toBeLessThanOrEqual(afterQuestion.cardBottom + 1)
  // If the natural content exceeds the column, wheel back to the card; if it fits, its header is already reachable.
  const column = page.locator('.chat-column')
  const columnMetrics = await column.evaluate(el => ({ scrollTop: (el as HTMLElement).scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight }))
  if (columnMetrics.scrollHeight > columnMetrics.clientHeight) {
    const bounds = await column.boundingBox()
    await page.mouse.move(bounds!.x + 2, bounds!.y + bounds!.height / 2)
    await page.mouse.wheel(0, -1200)
    await expect.poll(() => column.evaluate(el => (el as HTMLElement).scrollTop)).toBeLessThan(columnMetrics.scrollTop)
  } else {
    const cardTop = await page.locator('.question-form-card').evaluate(el => el.getBoundingClientRect().top)
    expect(cardTop).toBeGreaterThanOrEqual(afterQuestion.columnTop - 1)
  }
  const dismiss = page.locator('.question-card-dismiss')
  const dismissBounds = await dismiss.evaluate(el => { const r = el.getBoundingClientRect(), app = document.querySelector('#ciao-app')!.getBoundingClientRect(); return { top: r.top, bottom: r.bottom, appTop: app.top, appBottom: app.bottom } })
  expect(dismissBounds.top).toBeGreaterThanOrEqual(dismissBounds.appTop)
  expect(dismissBounds.bottom).toBeLessThanOrEqual(dismissBounds.appBottom)
  // Keyboard focus remains available for reaching the dock control as well.
  await dismiss.focus()
  await expect(dismiss).toBeFocused()
  const dismissHit = await dismiss.evaluate(el => {
    const r = el.getBoundingClientRect(), hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)
    return hit === el || el.contains(hit)
  })
  expect(dismissHit).toBe(true)
  await composer.focus()
  await expect.poll(() => composer.evaluate(el => el.getBoundingClientRect().bottom <= document.querySelector('.chat-column')!.getBoundingClientRect().bottom + 1)).toBe(true)

  await mockKeyboardHeight(page)
  await expect(composer).toBeFocused()
  await expect.poll(() => composer.evaluate(el => el.getBoundingClientRect().bottom <= document.querySelector('.chat-column')!.getBoundingClientRect().bottom + 1)).toBe(true)
  const beforeGrow = await page.locator('.chat-column').evaluate(el => (el as HTMLElement).scrollTop)
  await page.evaluate(() => {
    const viewport = window.visualViewport!
    Object.defineProperty(viewport, 'height', { configurable: true, value: 361 })
    viewport.dispatchEvent(new Event('resize'))
  })
  await expect.poll(() => page.locator('html').evaluate(el => el.classList.contains('keyboard-open'))).toBe(false)
  await expect.poll(() => page.locator('#ciao-app').evaluate(el => el.getBoundingClientRect().height)).toBe(361)
  await expect.poll(() => composer.evaluate(el => el.getBoundingClientRect().bottom <= document.querySelector('.chat-column')!.getBoundingClientRect().bottom + 1)).toBe(true)
  const afterGrow = await page.locator('.chat-column').evaluate(el => (el as HTMLElement).scrollTop)
  expect(afterGrow).toBeLessThanOrEqual(beforeGrow + 1)

  const checkVisibleHit = async (selector: string) => {
    await expect.poll(() => page.locator(selector).first().evaluate(el => {
      const r = el.getBoundingClientRect(), col = document.querySelector('.chat-column')!.getBoundingClientRect(), v = window.visualViewport!
      const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)
      return r.top >= Math.max(col.top, v.offsetTop) - 1 && r.bottom <= Math.min(col.bottom, v.offsetTop + v.height) + 1 && (hit === el || el.contains(hit))
    })).toBe(true)
  }
  await composer.fill('')
  await composer.focus()
  await expect(composer).toBeFocused()
  await composer.pressSequentially('/')
  await expect(page.locator('.commands-picker:not(.mention-picker)')).toBeVisible()
  await expect(composer).toBeFocused()
  await checkVisibleHit(COMPOSER)
  await checkVisibleHit('.commands-picker:not(.mention-picker)')
  await checkVisibleHit('.commands-picker-row')
  await page.locator('.commands-picker-row').first().click()
  await expect(composer).toHaveValue('/help')
  await composer.fill('')
  await composer.focus()
  await composer.pressSequentially('@')
  await expect(page.locator('.mention-picker')).toBeVisible()
  await expect(composer).toBeFocused()
  await checkVisibleHit(COMPOSER)
  await checkVisibleHit('.mention-picker')
  await checkVisibleHit('.mention-picker-row')

})

const NATURAL_SINGLE_QUESTION = { questions: [{
  id: 'single-short', question: 'Which setting should we use?', header: 'Default', isOther: false,
  required: true, type: 'string', multiSelect: false,
  options: [{ label: 'Recommended', description: 'Use the recommended project setting.' }],
}] }
const NATURAL_MULTI_QUESTION = { questions: [{
  id: 'multi-short', question: 'Which approach should we use?', header: 'Approach', isOther: false,
  required: true, type: 'string', multiSelect: false,
  options: [
    { label: 'Keep current behavior', description: 'Preserve existing project behavior and configuration.' },
    { label: 'Use the updated behavior', description: 'Apply the updated behavior across this project.' },
    { label: 'Ask the project owner', description: 'Wait for the owner to choose a project-specific approach.' },
  ],
}] }

test('short one and multiple option questions keep natural body height when content fits', async ({ page }, testInfo) => {
  await isolate(page, `question-natural-question-${testInfo.workerIndex}`)
  for (const size of [{ width: 390, height: 844 }, { width: 1200, height: 800 }]) {
    for (const [label, question] of [['single', NATURAL_SINGLE_QUESTION], ['multiple', NATURAL_MULTI_QUESTION]] as const) {
      await page.goto('about:blank')
      await page.setViewportSize(size)
      await seedInbox(page, { chats: { [CHAT_ID]: { pending_question: JSON.stringify(question) } } })
      await boot(page, CHAT, COMPOSER)
          await injectQuestion(page, question)
      await expect(page.locator('.question-form-card')).toBeVisible()
      const metrics = await page.evaluate(() => {
        const body = document.querySelector('.question-form-body')!
        const card = document.querySelector('.question-form-card')!
        const cardRect = card.getBoundingClientRect(), app = document.querySelector('#ciao-app')!.getBoundingClientRect()
        const actions = document.querySelector('.question-card-actions')!.getBoundingClientRect()
        return { bodyClient: body.clientHeight, bodyScroll: body.scrollHeight, cardTop: cardRect.top, cardBottom: cardRect.bottom, appBottom: app.bottom, actionsBottom: actions.bottom, cardMax: getComputedStyle(card).maxHeight }
      })
      expect(metrics.bodyClient, `${size.width}x${size.height} ${label}: ${JSON.stringify(metrics)}`).toBeGreaterThan(44)
      expect(metrics.bodyScroll, `${size.width}x${size.height} ${label}: ${JSON.stringify(metrics)}`).toBe(metrics.bodyClient)
      expect(metrics.cardBottom).toBeLessThanOrEqual(Number.parseFloat(metrics.cardMax) + metrics.cardTop + 1)
      expect(metrics.actionsBottom).toBeLessThanOrEqual(metrics.cardBottom + 1)
      expect(metrics.cardBottom).toBeLessThanOrEqual(metrics.appBottom + 1)
    }
  }
})

test('measured minimum follows wrapped header and actions', async ({ page }, testInfo) => {
  await isolate(page, `question-wrapped-${testInfo.workerIndex}`)
  await page.setViewportSize({ width: 320, height: 361 })
  await seedInbox(page, { chats: { [CHAT_ID]: { pending_question: JSON.stringify(QUESTION) } } })
  await boot(page, CHAT, COMPOSER)
  await page.addStyleTag({ content: '.question-card-header, .question-card-actions { font-family: Arial, sans-serif !important; font-size: 30px !important; } .question-card-actions { flex-wrap: wrap !important; } .question-card-actions button { min-width: 65% !important; }' })
  await injectQuestion(page)
  await expect.poll(() => page.locator('.question-form-card').evaluate(el => parseFloat((el as HTMLElement).style.getPropertyValue('--question-min')))).toBeGreaterThan(200)
  const geometry = await page.evaluate(() => {
    const rect = (sel: string) => document.querySelector(sel)!.getBoundingClientRect()
    const card = rect('.question-form-card'), header = rect('.question-card-header'), actions = rect('.question-card-actions'), body = rect('.question-form-body')
    return { card: card.toJSON(), header: header.toJSON(), actions: actions.toJSON(), body: body.toJSON() }
  })
  expect(geometry.body.height).toBeGreaterThanOrEqual(44)
  expect(geometry.actions.bottom).toBeLessThanOrEqual(geometry.card.bottom + 1)
  expect(geometry.actions.height).toBeGreaterThan(44)
})

test('focused fixed-size composer stays visible when a dock arrives', async ({ page }, testInfo) => {
  for (const kind of ['question', 'permission'] as const) {
    await isolate(page, `question-arrival-${kind}-${testInfo.workerIndex}`)
    await page.setViewportSize({ width: 390, height: 361 })
    await boot(page, CHAT, COMPOSER)
      await page.addStyleTag({ content: 'textarea.chat-input { height: 100px !important; min-height: 100px !important; max-height: 100px !important; }' })
    const composer = page.locator(COMPOSER)
    await composer.focus()
      const before = await page.evaluate(() => ({ panel: document.querySelector('.chat-panel')!.getBoundingClientRect().height, input: document.querySelector('textarea.chat-input')!.getBoundingClientRect().height }))
    if (kind === 'question') await injectQuestion(page)
    else {
      const response = await page.request.post('/__fixture__/chat-frame', { data: { chat_id: CHAT_ID, frames: [{ type: 'permission_request', tool_name: 'Bash', request_id: 'arrival-permission', message: 'Run the build?', tool_input: 'npm run build' }] } })
      expect(response.ok()).toBe(true)
    }
    await expect(page.locator(kind === 'question' ? '.question-form-card' : '.permission-card').first()).toBeVisible()
    const after = await page.evaluate(() => ({ panel: document.querySelector('.chat-panel')!.getBoundingClientRect().height, input: document.querySelector('textarea.chat-input')!.getBoundingClientRect().height }))
    expect(after).toEqual(before)
    const metrics = await page.locator('.chat-column').evaluate(el => ({ height: el.clientHeight, scroll: el.scrollHeight, top: el.scrollTop }))
    expect(metrics.scroll, JSON.stringify({ kind, metrics })).toBeGreaterThan(metrics.height)
    await expect.poll(() => composer.evaluate(el => { const r = el.getBoundingClientRect(), c = document.querySelector('.chat-column')!.getBoundingClientRect(); return r.bottom <= c.bottom + 1 && r.top >= c.top - 1 }), kind).toBe(true)
    await page.goto('about:blank')
  }
})


test('question layout remains usable beside a rail and a pinned file', async ({ page }, testInfo) => {
  for (const pinned of [false, true]) {
    await isolate(page, `question-pane-${pinned}-${testInfo.workerIndex}`)
    await page.setViewportSize({ width: 1600, height: 900 })
    if (pinned) await page.route(/\/api\/chats(\/alpha-chat-1)?(\?.*)?$/, async route => {
      const response = await route.fetch()
      const data = await response.json() as Record<string, unknown> | Array<Record<string, unknown>>
      const pin = (chat: Record<string, unknown>) => chat.chat_id === CHAT_ID ? { ...chat, pinned_file_path: 'notes/plan.md' } : chat
      await route.fulfill({ response, json: Array.isArray(data) ? data.map(pin) : pin(data) })
    })
    await boot(page, CHAT, COMPOSER)
    if (pinned) await expect(page.locator('.chat-split-side')).toBeVisible()
    else await expect(page.locator('.chat-rail')).toBeVisible()
    await injectQuestion(page)
    await expectQuestionSubmitVisible(page)
    await expectQuestionTargetsReachable(page)
    await page.goto('about:blank')
    if (pinned) await page.unrouteAll({ behavior: 'wait' })
  }
})

test('rapid viewport changes respect focus changes and unmount', async ({ page }, testInfo) => {
  await isolate(page, `question-resize-life-${testInfo.workerIndex}`)
  await page.setViewportSize({ width: 390, height: 361 })
  await boot(page, CHAT, COMPOSER)
  await injectQuestion(page)
  const input = page.locator(COMPOSER)
  await input.fill('draft one\ndraft two\ndraft three')
  await input.focus()
  await page.evaluate(() => {
    const vv = window.visualViewport!
    Object.defineProperty(vv, 'height', { configurable: true, value: 280 })
    vv.dispatchEvent(new Event('resize'))
    Object.defineProperty(vv, 'height', { configurable: true, value: 361 })
    vv.dispatchEvent(new Event('resize'))
  })
  await expect(input).not.toBeFocused() // Closing the keyboard intentionally releases focus.
  await expect(input).toHaveValue('draft one\ndraft two\ndraft three')
  await input.focus()
  await expect.poll(() => input.evaluate(el => el.getBoundingClientRect().bottom <= document.querySelector('.chat-column')!.getBoundingClientRect().bottom + 1)).toBe(true)
  const other = page.locator('.question-other').first()
  await page.evaluate(() => { window.visualViewport!.dispatchEvent(new Event('resize')); (document.querySelector('.question-other') as HTMLElement).focus() })
  await expect(other).toBeFocused()
  const scroll = await page.locator('.chat-column').evaluate(el => el.scrollTop)
  await page.waitForTimeout(100)
  expect(await page.locator('.chat-column').evaluate(el => el.scrollTop)).toBe(scroll)
  await page.evaluate(() => { window.visualViewport!.dispatchEvent(new Event('resize')) })
  await page.goto('about:blank')
  await page.evaluate(() => { delete (window.visualViewport as unknown as { height?: number }).height; window.visualViewport!.dispatchEvent(new Event('resize')) })
})

test('question remains usable at a 200 percent desktop zoom viewport', async ({ page }, testInfo) => {
  await isolate(page, `question-zoom-${testInfo.workerIndex}`)
  // 600×400 CSS pixels is the viewport exposed by a 1200×800 window at 200% zoom.
  await page.setViewportSize({ width: 1200, height: 800 })
  await boot(page, CHAT, COMPOSER)
  await injectQuestion(page)
  await page.setViewportSize({ width: 600, height: 400 })
  const cancel = page.getByRole('button', { name: 'Cancel' })
  await cancel.focus()
  await expect(cancel).toBeFocused()
  await expect(cancel).toBeInViewport({ ratio: 1 })
  await page.screenshot({ path: testInfo.outputPath('question-zoom-200.png') })
})


test('keyboard navigation keeps slash and mention highlights visible in short panes', async ({ page }, testInfo) => {
  await isolate(page, `question-picker-keyboard-${testInfo.workerIndex}`)
  const commands = Array.from({ length: 12 }, (_, i) => ({ name: `command${i}`, description: `Command ${i}`, argument_hint: '', source: 'builtin', path: '' }))
  const subagents = Array.from({ length: 12 }, (_, i) => ({ name: `agent${i}`, description: `Agent ${i}` }))
  await page.route('**/api/commands**', route => route.fulfill({ json: { commands, skills: [] } }))
  await page.route('**/api/agent-assets', route => route.fulfill({ json: { subagents } }))
  await page.setViewportSize({ width: 390, height: 361 })
  await boot(page, CHAT, COMPOSER)
  await injectQuestion(page)
  const composer = page.locator(COMPOSER)
  for (const [trigger, selector] of [['/', '.commands-picker:not(.mention-picker)'], ['@', '.mention-picker']] as const) {
    await composer.fill('')
    await composer.focus()
    await composer.pressSequentially(trigger)
    const picker = page.locator(selector)
    await expect(picker).toBeVisible()
    expect(await picker.locator('.commands-picker-row').count()).toBeGreaterThan(8)
    expect(await picker.evaluate(el => el.scrollHeight)).toBeGreaterThan(await picker.evaluate(el => el.clientHeight))
    const check = async () => {
      const row = picker.locator('[aria-selected="true"]')
      await expect(row).toHaveCount(1)
      const geometry = await row.evaluate(el => {
        const row = el.getBoundingClientRect(), parent = el.parentElement!, box = parent.getBoundingClientRect()
        return { top: row.top, bottom: row.bottom, visibleTop: box.top + parent.clientTop, visibleBottom: box.top + parent.clientTop + parent.clientHeight }
      })
      expect(geometry.top, JSON.stringify(geometry)).toBeGreaterThanOrEqual(geometry.visibleTop - 1)
      expect(geometry.bottom, JSON.stringify(geometry)).toBeLessThanOrEqual(geometry.visibleBottom + 1)
    }
    await check()
    const columnBefore = await page.locator('.chat-column').evaluate(el => el.scrollTop)
    await composer.press('ArrowUp')
    await check()
    await composer.press('ArrowDown')
    await check()
    for (let i = 0; i < 8; i++) { await composer.press('ArrowDown'); await check() }
    expect(await page.locator('.chat-column').evaluate(el => el.scrollTop)).toBe(columnBefore)
    const selected = (await picker.locator('[aria-selected="true"] .commands-picker-name').innerText()).trim()
    await composer.press('Enter')
    await expect(composer).toHaveValue(new RegExp(selected.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
    await expect(picker).toHaveCount(0)
  }
})

})

