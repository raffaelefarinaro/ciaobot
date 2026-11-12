import { readFileSync } from 'node:fs'
import { expect, test, type Page } from '@playwright/test'
import { boot, COMPOSER, isolate } from '../support/app'

/**
 * Commenting on an HTML artifact behaves like commenting on Markdown.
 *
 * Only a real browser can show this: the artifact runs in a sandboxed,
 * opaque-origin frame, jsdom has no CSS Custom Highlight API, and focus and
 * keystrokes inside a frame are exactly what never reached the page.
 *
 * - A selection in the frame gets the page's own `.fv-comment-trigger` (the
 *   bridge used to draw a different orange pill inside the frame).
 * - Typing with a frame selection opens the composer seeded with the key.
 * - Commenting across <th> cells leaves the header row's child count alone:
 *   a <mark> around the indentation between cells rendered as a phantom
 *   column and shifted every row under it.
 */
const BRIDGE = (() => {
  const py = readFileSync(new URL('../../../ciao/web/artifact_bridge.py', import.meta.url), 'utf8')
  const open = 'BRIDGE_SCRIPT = r"""'
  const from = py.indexOf(open) + open.length
  return py.slice(from, py.indexOf('"""', from))
})()

const TABLE_DOC = `<!doctype html><html><head><script>
${BRIDGE}
</script><style>body{font:16px/1.5 sans-serif;padding:16px}th,td{padding:4px 12px;border:1px solid #ccc}</style></head>
<body>
  <table>
    <thead>
      <tr>
        <th>Name</th>
        <th>Owner</th>
        <th>Status</th>
      </tr>
    </thead>
    <tbody>
      <tr><td>Alpha</td><td>Ana</td><td>Open</td></tr>
    </tbody>
  </table>
</body></html>`

async function openArtifact(page: Page, session: string, doc = TABLE_DOC) {
  await isolate(page, session)
  const canonical = '/fixture/work/memory-vault/Workspace/table.html'
  await page.route('**/api/chats/alpha-chat-1/messages*', route => route.fulfill({ json: [
    { role: 'user', content: 'Build a table', turn_index: 0 },
    { role: 'assistant', content: 'Open [table](memory-vault/Workspace/table.html).', turn_index: 0 },
  ] }))
  await page.route('**/api/chats/alpha-chat-1/file-path?*', route => route.fulfill({ json: { path: canonical } }))
  await page.route('**/api/workspace-file?*', route => route.fulfill({ body: doc, contentType: 'text/html' }))
  await page.route('**/api/workspace-html?*', route => route.fulfill({ body: doc, contentType: 'text/html' }))
  // A refused pin falls back to the file viewer modal.
  await page.route('**/api/chats/alpha-chat-1', route => route.request().method() === 'PATCH'
    ? route.fulfill({ status: 404, json: { error: 'not found' } })
    : route.fallback())

  await boot(page, '/chat/alpha-chat-1', COMPOSER)
  await page.locator('a[data-file-path="memory-vault/Workspace/table.html"]').click()
  await expect(page.locator('.fv-modal .hav-frame')).toBeVisible()
  const frame = page.frameLocator('.fv-modal .hav-frame')
  await expect(frame.locator('body')).toBeVisible()
  return frame
}

test.use({ viewport: { width: 1280, height: 900 } })

test('a selection across header cells comments like Markdown and keeps the table intact', async ({ page }, testInfo) => {
  const frame = await openArtifact(page, `artifact-comments-${testInfo.workerIndex}`)
  const row = frame.locator('thead tr')
  const before = await row.evaluate(tr => tr.childNodes.length)

  // Focus the frame, then select "Name … Status" across the three <th>s.
  await frame.locator('tbody td').first().click()
  await row.evaluate(tr => {
    const ths = tr.querySelectorAll('th')
    const range = document.createRange()
    range.setStart(ths[0].firstChild as Node, 0)
    range.setEnd(ths[2].firstChild as Node, 6)
    const sel = getSelection()
    sel?.removeAllRanges()
    sel?.addRange(range)
  })

  // The page's own Comment button, not a pill inside the frame.
  const trigger = page.locator('.fv-comment-trigger')
  await expect(trigger).toBeVisible()
  await expect(trigger).toHaveText('Comment')
  await expect(frame.locator('#ciao-comment-pill')).toHaveCount(0)
  await page.screenshot({ path: testInfo.outputPath('artifact-comment-trigger.png'), fullPage: false })

  // Typing with focus in the frame opens the composer, seeded with the key.
  await page.keyboard.press('W')
  const dialog = page.getByRole('dialog', { name: 'Add comment', exact: true })
  await expect(dialog).toBeVisible()
  const textarea = dialog.locator('textarea')
  await expect(textarea).toHaveValue('W')
  await expect(textarea).toBeFocused()
  await expect(trigger).toBeHidden()
  await page.keyboard.type('hich owner?')
  await dialog.getByRole('button', { name: 'Add comment', exact: true }).click()
  await expect(dialog).toBeHidden()

  // The comment is painted without touching the document: same row, no marks.
  await expect.poll(() => frame.locator('body').evaluate(() => CSS.highlights.has('ciao-comment'))).toBe(true)
  expect(await row.evaluate(tr => tr.childNodes.length)).toBe(before)
  await expect(frame.locator('mark')).toHaveCount(0)
  expect(await frame.locator('tbody tr').evaluate(tr => tr.children.length)).toBe(3)

  await page.screenshot({ path: testInfo.outputPath('artifact-comment.png'), fullPage: false })
})

const CONTROL_DOC = `<!doctype html><html><head><script>
${BRIDGE}
</script><style>body{font:16px/1.5 sans-serif;padding:16px}p{margin:0 0 12px}</style></head>
<body>
  <div id="box">
    <p>First paragraph of the comment.</p>
    <p>Second one with <button id="go" onclick="this.textContent='pressed'">Go</button> inside it.</p>
  </div>
</body></html>`

test('a control inside a commented span keeps working', async ({ page }, testInfo) => {
  // getClientRects on a range that contains whole elements returns their
  // border boxes, so the whole commented block (and its button) used to
  // open the comment and swallow the click.
  const frame = await openArtifact(page, `artifact-controls-${testInfo.workerIndex}`, CONTROL_DOC)
  const box = frame.locator('#box')
  await frame.locator('#box p').first().click()
  await box.evaluate(el => {
    const range = document.createRange()
    range.selectNodeContents(el)
    getSelection()?.removeAllRanges()
    getSelection()?.addRange(range)
  })
  await expect(page.locator('.fv-comment-trigger')).toBeVisible()
  await page.keyboard.press('N')
  const dialog = page.getByRole('dialog', { name: 'Add comment', exact: true })
  await expect(dialog.locator('textarea')).toHaveValue('N')
  await dialog.locator('textarea').fill('Note')
  await dialog.getByRole('button', { name: 'Add comment', exact: true }).click()
  await expect(dialog).toBeHidden()
  await expect.poll(() => box.evaluate(() => CSS.highlights.has('ciao-comment'))).toBe(true)

  await frame.locator('#go').click()
  await expect(frame.locator('#go')).toHaveText('pressed')

  // The text itself still shows the pointer.
  await frame.locator('#box p').first().hover({ position: { x: 20, y: 10 } })
  await expect.poll(() => box.evaluate(() => document.documentElement.classList.contains('ciao-comment-hover'))).toBe(true)
})
