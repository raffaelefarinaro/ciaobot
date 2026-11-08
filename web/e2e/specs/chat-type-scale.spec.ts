import { expect, test, type Page } from '@playwright/test'
import { boot, COMPOSER, isolate, quiesce } from '../support/app'

/**
 * #2707: at the default scale the transcript asked for 120% of every token
 * (16.8px prose, 27.7px leading) while the phone composer was pinned to 16px,
 * so text visibly grew on send. The default is now the platform's 16px body
 * (14 × 1.15 = 16.1px), a reply is 1.55 leading, headings are 1.35/1.18/1.08em
 * and the phone composer follows the same body token instead of a fixed 16px.
 *
 * Only a real layout engine can prove computed sizes: jsdom loads no scoped
 * CSS and reports every box as 0x0.
 */
const CHAT = '/chat/alpha-chat-1'

test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

type Measured = {
  /** The request bubble's computed size. */
  request: number
  /** The reply's computed size. */
  reply: number
  /** The reply's line-height as a multiple of its font size. */
  leading: number
  composer: number
  /** An h2 inside the reply, in px. */
  heading: number
}

async function measure(page: Page): Promise<Measured> {
  return page.evaluate(() => {
    const px = (el: Element | null) => {
      if (!el) throw new Error('type scale: expected element missing')
      return Number.parseFloat(getComputedStyle(el as HTMLElement).fontSize)
    }
    const request = document.querySelector('.messages-content .message.user')
    const reply = document.querySelector<HTMLElement>('.messages-content .message.assistant')
    const content = reply?.querySelector<HTMLElement>('.message-content')
    const composer = document.querySelector('.chat-input')
    if (!request || !reply || !content || !composer) {
      throw new Error('type scale: the transcript or composer is missing')
    }
    // The fixture's transcript carries no Markdown heading, and a reply's
    // content is v-html, so this is the same seam: :deep(h2) styles any h2
    // inside .message-content.
    let heading = content.querySelector('h2')
    if (!heading) {
      heading = document.createElement('h2')
      heading.textContent = 'A heading in a reply'
      content.appendChild(heading)
    }
    const replySize = px(reply)
    return {
      request: px(request),
      reply: replySize,
      leading: Number.parseFloat(getComputedStyle(reply).lineHeight) / replySize,
      composer: px(composer),
      heading: px(heading),
    }
  })
}

test('phone prose matches the composer and the platform body size', async ({ page }, testInfo) => {
  await isolate(page, `type-scale-${testInfo.workerIndex}`)
  await boot(page, CHAT, COMPOSER, {
    beforeNavigate: async () => {
      await page.request.post('/__fixture__/transcript', { data: {} })
    },
  })
  await quiesce(page)

  const before = await measure(page)

  // (a) The platform's 16px body, and a reply's rhythm tightens to 1.55.
  expect(Math.abs(before.request - 16.1), 'request prose is the 16px body').toBeLessThanOrEqual(0.3)
  expect(Math.abs(before.reply - 16.1), 'reply prose is the 16px body').toBeLessThanOrEqual(0.3)
  expect(before.leading, 'reply leading is at most 1.55').toBeLessThanOrEqual(1.56)

  // (b) The composer renders the same size the transcript does, before and
  // after the user types: text must not grow on send.
  expect(Math.abs(before.composer - 16.1), 'the composer is the 16px body').toBeLessThanOrEqual(0.3)
  expect(Math.abs(before.composer - before.reply), 'composer and reply agree').toBeLessThanOrEqual(0.3)

  await page.locator('.chat-input').fill('The same text, typed and sent.')
  const typed = await measure(page)
  expect(typed.composer, 'typing does not change the field size').toBe(before.composer)

  // A saved scale still multiplies both surfaces: this is what proves the
  // composer tracks the token rather than sitting on the 16px floor, since the
  // default's 16.1px prose is within tolerance of a pinned 16px field.
  await page.evaluate(() => document.documentElement.style.setProperty('--font-scale', '1.3'))
  const scaled = await measure(page)
  expect(scaled.reply, 'prose follows a stored scale').toBeGreaterThan(before.reply + 1)
  expect(scaled.composer, 'the composer follows a stored scale').toBeGreaterThan(before.composer + 1)

  // (c) A heading is 1.18em of the message font (≤ 19.2px at 16.1px).
  expect(before.heading, 'a reply heading is 1.18em').toBeLessThanOrEqual(19.2)
  expect(before.heading, 'a reply heading is larger than the prose').toBeGreaterThan(before.reply)
})

test('a wide touch device keeps the composer above the iOS zoom floor', async ({ page }, testInfo) => {
  await isolate(page, `type-scale-wide-${testInfo.workerIndex}`)
  await boot(page, CHAT, COMPOSER, {
    beforeNavigate: async () => {
      await page.request.post('/__fixture__/transcript', { data: {} })
    },
  })
  // An iPad in landscape, or an iPhone in landscape: coarse pointer, no phone
  // breakpoint. The composer must still never fall under 16px, the size Safari
  // zooms below on focus, even at a stored scale that makes the body 14px.
  await page.setViewportSize({ width: 1024, height: 768 })
  await page.evaluate(() => document.documentElement.style.setProperty('--font-scale', '1.0'))
  const composer = await page.evaluate(() => {
    const el = document.querySelector('.chat-input')
    if (!el) throw new Error('type scale: composer missing at 1024px')
    return Number.parseFloat(getComputedStyle(el).fontSize)
  })
  expect(composer, 'the composer keeps the 16px auto-zoom floor').toBeGreaterThanOrEqual(16)
})
