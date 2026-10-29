import { expect, test, type Page } from '@playwright/test'
import { boot, COMPOSER, isolate, quiesce } from '../support/app'

/**
 * #2313: the queued-message chip above the composer carried hard-coded 10–12px
 * type, so it was the one composer control that ignored Settings → Appearance
 * → text size and read as fine print on a phone. jsdom applies no CSS, so only
 * a real engine under touch emulation can hold this.
 *
 * The chip follows `--text-base` rather than the textarea's own computed size:
 * at phone width the composer input is deliberately floored at 16px, scaled
 * (`max(16px, 16px * --font-scale)` in ChatPanel's `(max-width: 700px)` block),
 * so iOS does not zoom the field on focus. The chip's contract is the token
 * the composer declares for its body text, which is what the Appearance
 * setting multiplies.
 */
const CHAT_ID = 'alpha-chat-1'

test.use({ viewport: { width: 430, height: 932 }, isMobile: true, hasTouch: true, deviceScaleFactor: 3 })

type Control = { cls: string; width: number; height: number; glyph: number }

type Measurement = {
  queuedText: number
  queuedMode: number | null
  waitingLabel: number | null
  sendNow: number | null
  controls: Control[]
  fontScale: number
}

async function measure(page: Page): Promise<Measurement> {
  // A lazy route's stylesheet can land after its elements are visible; the
  // coarse-pointer sizes only exist once it has applied.
  await quiesce(page)
  return page.evaluate(() => {
    const px = (el: Element | null) =>
      el ? Number.parseFloat(getComputedStyle(el as HTMLElement).fontSize) : null
    const box = (el: Element) => {
      const r = el.getBoundingClientRect()
      return { width: Math.round(r.width), height: Math.round(r.height) }
    }
    const q = <T extends Element>(sel: string) => document.querySelector<T>(sel)
    return {
      queuedText: px(q('.queued-text')) ?? 0,
      queuedMode: px(q('.queued-mode-option')),
      waitingLabel: px(q('.queued-label')),
      sendNow: px(q('.queued-send-now')),
      controls: Array.from(document.querySelectorAll<HTMLElement>('.queued-chip .queued-action, .queued-chip .queued-remove'))
        .map((el) => {
          const svg = el.querySelector('svg')
          return {
            cls: el.className,
            ...box(el),
            glyph: svg ? Math.round(svg.getBoundingClientRect().width) : 0,
          }
        }),
      fontScale: Number.parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--font-scale')) || 1.2,
    }
  })
}

/** Every edit/remove control keeps a 44px target and an 18px glyph. */
function expectControlTargets(m: Measurement) {
  expect(m.controls.length, 'the queued chip has edit controls to measure').toBeGreaterThan(0)
  for (const c of m.controls) {
    expect(c.width, `${c.cls} width`).toBeGreaterThanOrEqual(44)
    expect(c.height, `${c.cls} height`).toBeGreaterThanOrEqual(44)
    expect(c.glyph, `${c.cls} glyph`).toBeGreaterThanOrEqual(18)
  }
}

/** Setting the Appearance scale moves the chip instead of leaving it behind. */
async function expectFollowsScale(page: Page, before: Measurement, modeOptions: boolean) {
  const factor = 1.5 / before.fontScale
  await page.evaluate(() => document.documentElement.style.setProperty('--font-scale', '1.5'))
  const grown = await measure(page)
  const textGrowth = grown.queuedText / before.queuedText
  expect(textGrowth, 'queued text grows with --font-scale').toBeGreaterThan(factor - 0.1)
  expect(textGrowth, 'queued text grows with --font-scale').toBeLessThan(factor + 0.1)
  if (modeOptions && before.queuedMode !== null && grown.queuedMode !== null) {
    const modeGrowth = grown.queuedMode / before.queuedMode
    expect(modeGrowth, 'Queue/Steer labels grow with --font-scale').toBeGreaterThan(factor - 0.1)
    expect(modeGrowth, 'Queue/Steer labels grow with --font-scale').toBeLessThan(factor + 0.1)
  }
}

test('a parked queued message is set in the composer type scale', async ({ page }, testInfo) => {
  await isolate(page, `queued-type-parked-${testInfo.workerIndex}`)
  await page.route('**/api/chats?active_only=1', async route => {
    const res = await route.fetch()
    const body = await res.json() as Array<Record<string, unknown>>
    for (const chat of body) {
      if (chat.chat_id === CHAT_ID) chat.pending_queue = [{ id: 'q1', text: 'second thing that has to wait' }]
    }
    await route.fulfill({ response: res, json: body })
  })

  await boot(page, `/chat/${CHAT_ID}`, COMPOSER)
  await expect(page.locator('.queued-chip')).toBeVisible()

  const m = await measure(page)
  // 14px × the Appearance scale, not a literal.
  expect(m.queuedText).toBeCloseTo(14 * m.fontScale, 1)
  // 11px × scale for the "Waiting" micro label.
  expect(m.waitingLabel).toBeCloseTo(11 * m.fontScale, 1)
  // 12px × scale for "Send now".
  if (m.sendNow !== null) expect(m.sendNow).toBeCloseTo(12 * m.fontScale, 1)
  expectControlTargets(m)
  await page.screenshot({ path: testInfo.outputPath('queued-parked.png') })

  await expectFollowsScale(page, m, false)
  await page.screenshot({ path: testInfo.outputPath('queued-parked-scaled.png') })
})

test('a steerable queued message keeps readable controls on a phone', async ({ page }, testInfo) => {
  await isolate(page, `queued-type-live-${testInfo.workerIndex}`)
  await boot(page, `/chat/${CHAT_ID}`, COMPOSER)
  await page.evaluate(async (chatId) => {
    await fetch('/__fixture__/chat-frame', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ chat_id: chatId, frames: [{ type: 'tool_use', tool_use_id: 't1', tool_name: 'shell', tool_input: 'sleep 300' }] }),
    })
  }, CHAT_ID)
  await expect(page.locator('.send-btn.is-stop')).toBeVisible({ timeout: 8_000 })

  await page.locator('textarea.chat-input').fill('second thing that has to wait')
  // Touch keyboards insert a newline on Enter; the button is the send path here.
  await page.locator('.send-btn').click()
  await expect(page.locator('.queued-chip')).toBeVisible()

  const m = await measure(page)
  expect(m.queuedMode).not.toBeNull()
  expect(m.queuedMode!).toBeCloseTo(12 * m.fontScale, 1)
  const modeBoxes = await page.locator('.queued-mode-option').evaluateAll(els =>
    els.map(el => Math.round(el.getBoundingClientRect().height)))
  expect(modeBoxes.length).toBeGreaterThan(0)
  for (const height of modeBoxes) expect(height).toBeGreaterThanOrEqual(44)
  expectControlTargets(m)
  await page.screenshot({ path: testInfo.outputPath('queued-live.png') })

  await expectFollowsScale(page, m, true)
  await page.screenshot({ path: testInfo.outputPath('queued-live-scaled.png') })
})
