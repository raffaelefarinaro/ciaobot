import { expect, test, type Page } from '@playwright/test'
import { boot, isolate } from '../support/app'
import { CARE_PROPOSAL_PATH, careReports } from '../../src/lib/careReportFixture'
import { UNSURE_FIXTURE_ITEMS } from '../../src/stories/unsureFixture'

/**
 * Every surface that opens a chat with a file pinned, on a workspace whose
 * vault is a subfolder of its agent root (`alpha/memory-vault`). The engine
 * hands the page install-relative viewer paths (`alpha/memory-vault/...`); a
 * pin anchors a relative path at the chat's agent root (`/install/alpha`), so
 * pinning the raw path is a 404 and the page showed "Could not update pinned
 * file". The page now resolves the viewer path first
 * (`GET /api/workspace-file-path`) and pins the absolute result.
 *
 * The engine's two rules are modelled here: the resolver knows the files under
 * `alpha/` and answers their absolute path, and a pin refuses anything that is
 * not absolute with the engine's own 404.
 */
const PROPOSAL = [
  '---',
  'type: guide-edit',
  'target: AGENTS.md',
  'title: Trim the guide',
  'summary: Removes an outdated status section.',
  'rationale: Nobody uses it.',
  'proposed_change: Delete the section.',
  'created: 2026-11-26',
  'status: draft',
  '---',
  '',
  'Loaded into every chat.',
  '',
].join('\n')
const INSTALL = '/install'
const VAULT = 'alpha/memory-vault'

interface Engine { pins: string[]; creates: { title: string }[] }

async function engine(page: Page): Promise<Engine> {
  const seen: Engine = { pins: [], creates: [] }
  await page.route(/\/api\/care-report\?/, route => route.fulfill({
    json: {
      ...careReports.needsYou,
      workspace: 'alpha',
      report_path: `${VAULT}/Workspace/Care-Report.md`,
      unsure_path: `${VAULT}/Workspace/Unsure.md`,
      needs_you: careReports.needsYou.needs_you.map(item => ({
        ...item,
        open_path: item.path && item.path !== 'AGENTS.md' ? `${VAULT}/${item.path}` : '',
      })),
    },
  }))
  await page.route(/\/api\/memory\/unsure(\?.*)?$/, route => route.fulfill({
    json: {
      workspace: 'alpha', path: `${VAULT}/Workspace/Unsure.md`, found: true,
      items: UNSURE_FIXTURE_ITEMS, counts: { waiting: UNSURE_FIXTURE_ITEMS.length, ticked: 0 },
    },
  }))
  await page.route(/\/api\/workspace-file-path\?/, route => {
    const path = new URL(route.request().url()).searchParams.get('path') || ''
    return path.startsWith('alpha/') || path.startsWith(`${INSTALL}/`)
      ? route.fulfill({ json: { path: path.startsWith('/') ? path : `${INSTALL}/${path}` } })
      : route.fulfill({ status: 404, json: { error: 'not found' } })
  })
  await page.route(/\/api\/workspace-file\?/, route => route.fulfill({ body: PROPOSAL, contentType: 'text/plain' }))
  await page.route(/\/api\/projects$/, async route => {
    const response = await route.fetch()
    const projects = (await response.json()) as { name: string }[]
    await route.fulfill({ response, json: projects.map(p => (p.name === 'General' ? { ...p, is_auto: true } : p)) })
  })
  await page.route(/\/api\/projects\/alpha-general\/chats$/, async route => {
    if (route.request().method() !== 'POST') return route.fallback()
    const { title } = route.request().postDataJSON() as { title: string }
    seen.creates.push({ title })
    const list = await (await page.request.get('/api/chats')).json() as Record<string, unknown>[]
    const row = list.find(c => c.chat_id === 'alpha-chat-2')!
    await route.fulfill({ status: 201, json: { ...row, chat_id: 'care-fix', title, pinned_file_path: '', pin_revision: 0 } })
  })
  await page.route(/\/api\/chats\/care-fix$/, async route => {
    if (route.request().method() !== 'PATCH') return route.fallback()
    const { pin } = route.request().postDataJSON() as { pin: { path: string } }
    seen.pins.push(pin.path)
    if (!pin.path.startsWith('/')) return route.fulfill({ status: 404, json: { error: 'not found' } })
    const list = await (await page.request.get('/api/chats')).json() as Record<string, unknown>[]
    const row = list.find(c => c.chat_id === 'alpha-chat-2')!
    await route.fulfill({ json: { ...row, chat_id: 'care-fix', pinned_file_path: pin.path, pin_revision: 1, dismissed_pin_paths: [] } })
  })
  return seen
}

async function settled(page: Page, seen: Engine, pin: string) {
  await expect.poll(() => seen.pins).toEqual([pin])
  await expect(page.getByText('Could not update pinned file')).toHaveCount(0)
}

test.describe('chats that pin a file, on a subfolder vault', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `care-pin-${testInfo.workerIndex}-${testInfo.title}`)
    await page.setViewportSize({ width: 1280, height: 900 })
  })

  const decisions = [
    ['the note item', 3, `${INSTALL}/${VAULT}/People/User.md`, 'Care: Links in People/User.md and 3'],
  ] as const
  for (const [label, index, pin, title] of decisions) {
    test(`Decide in a chat on ${label} pins the note, with a plain title`, async ({ page }) => {
      const seen = await engine(page)
      await boot(page, '/memory/care', '.mc-need')
      await page.locator('.mc-need').nth(index).getByRole('button', { name: /^Decide in a chat: / }).click()
      await settled(page, seen, pin)
      expect(seen.creates).toHaveLength(1)
      expect(seen.creates[0].title.startsWith(title.replace(/[:,]$/, ''))).toBe(true)
      expect(seen.creates[0].title).not.toMatch(/[\[\]`*]/)
    })
  }

  test('Approve on a proposal pins the proposal file', async ({ page }) => {
    const seen = await engine(page)
    await boot(page, `/memory/care?proposal=${CARE_PROPOSAL_PATH}`, 'text=Trim the guide')
    await page.getByRole('button', { name: 'Approve' }).click()
    await settled(page, seen, `${INSTALL}/${VAULT}/${CARE_PROPOSAL_PATH}`)
  })

  test('Optimize pins the workspace guide', async ({ page }) => {
    const seen = await engine(page)
    await boot(page, '/memory/care', '.mc-need')
    await page.getByRole('button', { name: /^Optimize/ }).click()
    await settled(page, seen, `${INSTALL}/alpha/AGENTS.md`)
  })

  test('Go through the Unsure list pins Unsure.md', async ({ page }) => {
    const seen = await engine(page)
    await boot(page, '/memory/unsure', '.rq-row')
    await page.getByRole('button', { name: 'Go through it with Ciaobot' }).click()
    await settled(page, seen, `${INSTALL}/${VAULT}/Workspace/Unsure.md`)
  })
})
