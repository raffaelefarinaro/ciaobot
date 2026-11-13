import { expect, test } from '@playwright/test'
import { boot, COMPOSER, isolate } from '../support/app'

const WS = '/fixture/work'
const DIR = `${WS}/memory-vault/Workspace`
const now = 1762790400
const listings: Record<string, unknown> = {
  [DIR]: {
    path: DIR, root: { name: 'workspace', path: WS }, roots: [{ name: 'workspace', path: WS }],
    parent: `${WS}/memory-vault`, truncated: false,
    entries: [
      { name: 'drafts', path: `${DIR}/drafts`, type: 'dir', size: null, mtime: now, ext: '' },
      { name: 'exports', path: `${DIR}/exports`, type: 'dir', size: null, mtime: now, ext: '' },
      { name: 'draft.md', path: `${DIR}/draft.md`, type: 'file', size: 2140, mtime: now, ext: 'md' },
      { name: 'findings.md', path: `${DIR}/findings.md`, type: 'file', size: 5310, mtime: now - 600, ext: 'md' },
      { name: 'metrics.csv', path: `${DIR}/metrics.csv`, type: 'file', size: 880, mtime: now - 1200, ext: 'csv' },
      { name: 'dashboard.html', path: `${DIR}/dashboard.html`, type: 'file', size: 18400, mtime: now - 1800, ext: 'html' },
    ],
  },
  [`${DIR}/drafts`]: {
    path: `${DIR}/drafts`, root: { name: 'workspace', path: WS }, roots: [{ name: 'workspace', path: WS }],
    parent: DIR, truncated: false,
    entries: [{ name: 'v2.md', path: `${DIR}/drafts/v2.md`, type: 'file', size: 300, mtime: now, ext: 'md' }],
  },
}

async function setup(page: import('@playwright/test').Page, name: string) {
  await isolate(page, name)
  await page.route('**/api/chats/alpha-chat-1/messages*', route => route.fulfill({ json: [
    { role: 'user', content: 'Where is the draft?', turn_index: 0 },
    { role: 'assistant', content: 'Open `memory-vault/Workspace/draft.md`.', turn_index: 0 },
  ] }))
  await page.route('**/api/chats/alpha-chat-1/file-path?*', route => {
    const p = new URL(route.request().url()).searchParams.get('path') || ''
    return route.fulfill({ json: { path: p.startsWith('/') ? p : `${WS}/${p}` } })
  })
  await page.route('**/api/chats/alpha-chat-1/file-paths/resolve', route => route.fulfill({ json: { results: route.request().postDataJSON().paths.map((input: string) => ({ input, path: input === 'memory-vault/Workspace/draft.md' ? `${WS}/memory-vault/Workspace/draft.md` : null })) } }))
  await page.route('**/api/workspace-file?*', route => {
    const p = new URL(route.request().url()).searchParams.get('path') || ''
    return route.fulfill({ body: `# ${p.split('/').pop()}\n\nOpened from \`${p}\`.\n\n## Notes\n\nSome text to comment on.`, contentType: 'text/plain' })
  })
  await page.route('**/api/workspace-dir?*', route => {
    const p = new URL(route.request().url()).searchParams.get('path') || ''
    const hit = listings[p.replace(/\/$/, '')]
    return hit ? route.fulfill({ json: hit }) : route.fulfill({ status: 404, json: { error: 'not found' } })
  })
  await page.route('**/api/chats/alpha-chat-1', route => route.request().method() === 'PATCH'
    ? route.fulfill({ status: 404, json: { error: 'not found' } }) : route.fallback())
}

async function openFolder(page: import('@playwright/test').Page) {
  await boot(page, '/chat/alpha-chat-1', COMPOSER)
  await expect(page.locator('.startup-overlay')).toHaveCount(0)
  await page.locator(`a[data-file-path="${WS}/memory-vault/Workspace/draft.md"]`).click()
  const viewer = page.locator('.fv-modal')
  await expect(viewer).toBeVisible()
  await viewer.getByRole('button', { name: 'More file actions' }).click()
  await page.getByRole('menuitem', { name: 'Show in folder' }).or(page.getByRole('button', { name: 'Show in folder' })).first().click()
  await expect(viewer.locator('.vuefinder')).toBeVisible()
  await expect(viewer.getByText('findings.md').first()).toBeVisible()
  return viewer
}

test.describe('folder browser on a phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true })

  test('the tree stays closed so a file can be tapped', async ({ page }, testInfo) => {
    await setup(page, `fb-phone-${testInfo.workerIndex}`)
    const viewer = await openFolder(page)
    await expect(page.locator('.vuefinder__treeview__backdrop')).not.toBeVisible()
    await viewer.getByText('findings.md').first().tap()
    await expect(viewer).toContainText('Opened from')
  })
})

test.describe('folder browser on desktop', () => {
  test.use({ viewport: { width: 1280, height: 900 } })

  test('a subfolder and a file open in the viewer, and Back returns to the folder', async ({ page }, testInfo) => {
    await setup(page, `fb-desktop-${testInfo.workerIndex}`)
    const viewer = await openFolder(page)
    await viewer.getByText('drafts').first().dblclick()
    await expect(viewer.getByText('v2.md').first()).toBeVisible()
    await viewer.getByText('v2.md').first().dblclick()
    await expect(viewer).toContainText('Opened from')
    await page.goBack()
    await expect(viewer.locator('.vuefinder')).toBeVisible()
  })
})
