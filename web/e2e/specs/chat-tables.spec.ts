import { expect, test, type Page } from '@playwright/test'
import { boot, COMPOSER, horizontalOverflow, isolate, quiesce } from '../support/app'

const prose = 'A long description explaining the details of this particular item in a natural sentence'
const other = 'Another column contains a similarly long explanation with several useful words to read'
const token = 'unbrokenIdentifier'.repeat(30)
const markdown = `| Description | Details |
| --- | --- |
| ${prose} | ${other} |

| Short | Value |
| --- | --- |
| One | Two |

| A long first column header that should wrap naturally on a phone | Other |
| --- | --- |
| Small | Value |

| Identifier | Value |
| --- | --- |
| ${token} | Value |

| First | Second | Third |
| --- | --- | --- |
| Some prose with \`inline code\` and a [link](https://example.com) | More readable text | Third value |`

async function openTables(page: Page, session: string) {
  await isolate(page, session)
  await page.route('**/api/chats/alpha-chat-1/messages*', route => route.fulfill({ json: [
    { role: 'assistant', content: markdown, turn_index: 0 },
  ] }))
  await boot(page, '/chat/alpha-chat-1', COMPOSER)
  await expect(page.locator('.message-content .markdown-table-scroll')).toHaveCount(5)
  await quiesce(page)
  return page.locator('.message-content .markdown-table-scroll')
}

for (const width of [1280, 390]) {
  test(`prose columns share the available width at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 844 })
    const tables = await openTables(page, `table-prose-${width}-${testInfo.workerIndex}`)
    const table = tables.nth(0).locator('table')
    const geometry = await table.evaluate(el => {
      const first = el.querySelector('tbody td:first-child') as HTMLElement
      const second = el.querySelector('thead th:nth-child(2)') as HTMLElement
      const style = getComputedStyle(first)
      const line = Number.parseFloat(style.lineHeight)
      return { firstHeight: first.clientHeight - Number.parseFloat(style.paddingTop) - Number.parseFloat(style.paddingBottom), line, ratio: second.offsetWidth / el.getBoundingClientRect().width }
    })
    expect(geometry.ratio).toBeGreaterThanOrEqual(0.4)
    expect(geometry.firstHeight).toBeGreaterThan(geometry.line * 1.5)
    await expect(tables.nth(4).locator('code')).toHaveText('inline code')
    await expect(tables.nth(4).locator('a')).toHaveText('link')
    await expect(tables.nth(4).locator('th')).toHaveCount(3)
  })
}

test('compact tables, wrapping headers and keyboard scrolling survive at phone width', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const tables = await openTables(page, `table-phone-${testInfo.workerIndex}`)
  const compact = await tables.nth(1).evaluate(el => ({ width: el.clientWidth, parent: el.parentElement!.clientWidth }))
  expect(compact.width).toBeLessThan(compact.parent * 0.8)
  const header = await tables.nth(2).locator('th').first().evaluate(el => {
    const s = getComputedStyle(el)
    return { height: el.clientHeight - parseFloat(s.paddingTop) - parseFloat(s.paddingBottom), line: parseFloat(s.lineHeight) }
  })
  expect(header.height).toBeGreaterThan(header.line * 1.5)
  const region = tables.nth(3)
  await expect(region).toHaveAttribute('role', 'region')
  await expect(region).toHaveAttribute('aria-label', /.+/)
  await expect(region).toHaveAttribute('tabindex', '0')
  expect(await region.evaluate(el => el.scrollWidth > el.clientWidth)).toBe(true)
  expect((await horizontalOverflow(page)).overflow).toBeLessThanOrEqual(1)
  await region.focus()
  await expect(region).toBeFocused()
  const outline = await region.evaluate(el => getComputedStyle(el).outlineStyle)
  expect(outline).not.toBe('none')
  await page.keyboard.press('ArrowRight')
  await expect.poll(() => region.evaluate(el => el.scrollLeft)).toBeGreaterThan(0)
  await page.keyboard.press('Tab')
  await expect(region).not.toBeFocused()
})

test('prose wraps with enlarged text in a narrow pane', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const tables = await openTables(page, `table-scale-${testInfo.workerIndex}`)
  await page.evaluate(() => document.documentElement.style.setProperty('--font-scale', '1.5'))
  await quiesce(page)
  const ratio = await tables.first().locator('table').evaluate(el => el.querySelector('th:nth-child(2)')!.getBoundingClientRect().width / el.getBoundingClientRect().width)
  expect(ratio).toBeGreaterThanOrEqual(0.4)
  const first = await tables.first().locator('tbody td:first-child').evaluate(el => {
    const style = getComputedStyle(el)
    return { height: el.clientHeight - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom), line: parseFloat(style.lineHeight) }
  })
  expect(first.height).toBeGreaterThan(first.line * 1.5)
})

test('prose wraps in a constrained desktop chat pane', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  const tables = await openTables(page, `table-split-${testInfo.workerIndex}`)
  const row = page.locator('.chat-item', { hasText: 'alpha second conversation' })
  await row.hover()
  await row.locator('.chat-beside-btn').click()
  await expect(page.locator('.chat-split-side--chat')).toBeVisible()
  await expect(page.locator('.chat-split-resizer')).toBeVisible()
  await quiesce(page)
  const paneWidth = await page.locator('.chat-main > .chat-body').evaluate(el => el.getBoundingClientRect().width)
  expect(paneWidth).toBeLessThan(700)
  const geometry = await tables.first().locator('table').evaluate(el => {
    const first = el.querySelector('tbody td:first-child') as HTMLElement
    const style = getComputedStyle(first)
    return {
      height: first.clientHeight - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom),
      line: parseFloat(style.lineHeight),
      ratio: el.querySelector('th:nth-child(2)')!.getBoundingClientRect().width / el.getBoundingClientRect().width,
    }
  })
  expect(geometry.ratio).toBeGreaterThanOrEqual(0.4)
  expect(geometry.height).toBeGreaterThan(geometry.line * 1.5)
})
