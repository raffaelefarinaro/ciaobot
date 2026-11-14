import { expect, test } from '@playwright/test'
import { boot, COMPOSER, isolate, quiesce } from '../support/app'

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
  await page.locator('a[data-file-path="memory-vault/Workspace/draft.md"]').click()
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

  for (const theme of ['dark', 'light']) {
    test(`shared styles survive loading the folder browser (${theme})`, async ({ page }, testInfo) => {
      await setup(page, `fb-cascade-${theme}-${testInfo.workerIndex}`)
      await page.addInitScript(value => localStorage.setItem('ciao-theme', value), theme)
      await boot(page, '/', '.empty-shell:not(.home-boot)')
      await expect(page.locator('.startup-overlay')).toHaveCount(0)
      await quiesce(page)
      await page.locator('html').evaluate((el, light) => el.classList.toggle('theme-light', light), theme === 'light')
      await page.evaluate(() => {
        for (const [tag, cls, text] of [['button', 'btn-small', 'Neutral probe'], ['button', 'btn-primary btn-small', 'Primary probe'], ['span', '', 'Text probe']]) {
          const el = document.createElement(tag)
          el.className = cls
          el.textContent = text
          el.setAttribute('data-cascade-probe', text)
          document.body.appendChild(el)
        }
      })
      const neutral = page.locator('[data-cascade-probe="Neutral probe"]')
      const primary = page.locator('[data-cascade-probe="Primary probe"]')
      const measure = async () => {
        await primary.focus()
        await page.keyboard.press('Shift+Tab')
        await expect(neutral).toBeFocused()
        return page.evaluate(() => {
          const props = ['color', 'background-color', 'border-top-color', 'border-top-style', 'border-top-width', 'padding-top', 'padding-right', 'padding-bottom', 'padding-left', 'font-size', 'font-family', 'border-radius', 'outline-width', 'outline-style', 'outline-color', 'outline-offset']
          const values = (el: Element, keys: string[]) => Object.fromEntries(keys.map(key => [key, getComputedStyle(el).getPropertyValue(key)]))
          const body = values(document.body, ['color', 'background-color', 'font-family', 'font-size'])
          const buttons = Array.from(document.querySelectorAll('[data-cascade-probe]')).slice(0, 2).map(el => values(el, props))
          const text = values(document.querySelector('[data-cascade-probe="Text probe"]')!, ['color', 'font-family', 'font-size'])
          const tokens = ['--bg', '--fg'].map(token => {
            if (!getComputedStyle(document.documentElement).getPropertyValue(token).trim()) return ''
            const el = document.createElement('span')
            el.style.color = `var(${token})`
            document.body.appendChild(el)
            const value = getComputedStyle(el).color
            el.remove()
            return value
          })
          return { body, buttons, text, tokens, focusVisible: document.activeElement?.matches(':focus-visible') }
        })
      }
      const before = await measure()
      expect(before.tokens.every(Boolean)).toBe(true)
      expect(before.body['background-color']).toBe(before.tokens[0])
      expect(before.body.color).toBe(before.tokens[1])
      expect(before.focusVisible).toBe(true)
      expect(before.buttons[0]['outline-style']).not.toBe('none')
      expect(parseFloat(before.buttons[0]['outline-width'])).toBeGreaterThan(0)
      await page.locator('.chat-item', { hasText: 'alpha first conversation' }).click()
      await expect(page.locator(COMPOSER)).toBeVisible()
      await page.locator('a[data-file-path="memory-vault/Workspace/draft.md"]').click()
      const viewer = page.locator('.fv-modal')
      await expect(viewer).toBeVisible()
      await viewer.getByRole('button', { name: 'More file actions' }).click()
      await page.getByRole('menuitem', { name: 'Show in folder' }).or(page.getByRole('button', { name: 'Show in folder' })).first().click()
      await expect(viewer.locator('.vuefinder')).toBeVisible()
      await expect(viewer.getByText('findings.md').first()).toBeVisible()
      await page.keyboard.press('Escape')
      await expect(viewer).not.toBeVisible()
      await quiesce(page)
      expect(await measure()).toEqual(before)
    })
  }

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
