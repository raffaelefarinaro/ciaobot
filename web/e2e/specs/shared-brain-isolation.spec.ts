import { expect, test, type Locator, type Page } from '@playwright/test'
import { activeWorkspaceButton, boot, isolate, quiesce } from '../support/app'

/**
 * Client-side isolation of Sources and consent across two workspaces (#3614,
 * #3646). One stateful stub plays the engine for alpha and beta, so these tests
 * constrain what the panel shows, requests and sends: the foreign workspace's
 * names never reach the DOM or the requests, a DOM edit or forged token changes
 * nothing the client sends, and a workspace switch drops or ignores pending
 * work. The stub is not evidence of engine isolation; that is covered against
 * real stores by tests/test_share_evidence_isolation.py.
 */
const OVERVIEW = '/settings/workspace'
const SENTINELS = { alpha: ['Atlas Launch', 'grn_aaaa', 'ALPHA-ONLY'], beta: ['Borealis Secret', 'grn_bbbb', 'BETA-ONLY'] } as const
type WS = keyof typeof SENTINELS

const PROJECT: Record<WS, { id: string; name: string; brain: string; decision: string }> = {
  alpha: { id: 'proj-atlas', name: 'Atlas Launch', brain: 'alpha-notes', decision: 'ALPHA-ONLY decision' },
  beta: { id: 'proj-borealis', name: 'Borealis Secret', brain: 'beta-notes', decision: 'BETA-ONLY decision' },
}

function card(page: Page): Locator {
  return page.locator('.card', { has: page.getByText('Shared brains', { exact: true }) })
}

function grantRow(ws: WS, n: number, active = true) {
  const p = PROJECT[ws]
  return {
    grant_id: `grn_${ws === 'alpha' ? 'aaaa' : 'bbbb'}${String(n).padStart(20, '0')}`,
    type: 'snapshot', project_id: p.id, source_id: 'document:Decisions.md', source_kind: 'document',
    source_label: 'Decision document · Decisions.md',
    brain: { name: p.brain, identity: `github.com/acme/${p.brain}`, link_fingerprint: 'f'.repeat(16) },
    audience: `github.com/acme/${p.brain}`, field_mask: ['decision'], producers: ['memory_extraction'],
    issued_at: '2026-11-25T10:00:00Z', start_revision: null, consent_generation: 1, active, linked: true,
    status: active ? 'active' : 'revoked', revoked_at: null, revoked_generation: null,
    withdrawn_at: null, withdrawal_reason: '',
    history: [{ event: 'issued', at: '2026-11-25T10:00:00Z', generation: 1 }],
  }
}

interface Engine {
  requests: string[]
  confirms: Array<{ ws: string; body: Record<string, unknown> }>
  grants: Record<WS, ReturnType<typeof grantRow>[]>
  /** Hold confirm responses until released, to switch workspace while one is in flight. */
  holdConfirm: { release: () => void; gate: Promise<void> } | null
}

async function stubEngine(page: Page, seedBeta = true): Promise<Engine> {
  const engine: Engine = {
    requests: [], confirms: [], holdConfirm: null,
    grants: { alpha: [], beta: seedBeta ? [grantRow('beta', 1)] : [] },
  }
  const tokens = new Map<string, WS>()
  let issued = 0
  const wsOf = (url: string): WS => (/\/workspaces\/(alpha|beta)\//.exec(url)?.[1] ?? 'alpha') as WS
  await page.route(/\/api\/workspaces\/(alpha|beta)\/(shared-brains|share-evidence)/, async (route) => {
    const url = route.request().url()
    const ws = wsOf(url)
    const p = PROJECT[ws]
    const path = new URL(url).pathname
    engine.requests.push(`${route.request().method()} ${path}`)
    if (path.endsWith('/shared-brains')) {
      return route.fulfill({ json: { shared_brains: [{
        name: p.brain, kind: 'git', identity: `github.com/acme/${p.brain}`, path: `/synthetic/${p.brain}`,
        origin_changed: false, missing: false, problem: '', branch: 'main', head: 'abc1234',
        head_date: '2026-11-20', dirty: false, upstream: true, fetched_at: null, last_update: null,
      }] } })
    }
    if (path.endsWith('/share-evidence/preview')) {
      const token = `sev_${ws}_${++issued}`
      tokens.set(token, ws)
      return route.fulfill({ json: {
        preview_token: token,
        preview: {
          grant_type: 'snapshot', scope: 'one_decision', summary: 'Only the decision shown below is permitted, exactly as previewed.',
          future_items_without_review: false, project_id: p.id, source_id: 'document:Decisions.md',
          source_label: 'Decision document · Decisions.md', entry_label: 'Decision 1 in Decisions.md',
          fields: [[{ field: 'decision', value: p.decision }]], field_mask: ['decision'],
          excluded_fields: ['reason', 'effective_date'], producers: ['memory_extraction'],
          recipient: { brain: p.brain, identity: `github.com/acme/${p.brain}`, audience: `github.com/acme/${p.brain}` },
          expected_generation: 'a'.repeat(64), expires_at: '2026-11-25T10:15:00Z',
          notice: 'Changing or deleting the document later does not withdraw this confirmation; revoke it here instead.',
        },
      } })
    }
    if (path.endsWith('/share-evidence/confirm')) {
      const body = route.request().postDataJSON() as Record<string, unknown>
      engine.confirms.push({ ws, body })
      if (engine.holdConfirm) await engine.holdConfirm.gate
      const bound = tokens.get(String(body.preview_token))
      // Engine rule: a token is single-use and only valid in the workspace that issued it.
      if (bound !== ws) return route.fulfill({ status: 409, json: { error: 'This preview is no longer valid. Preview the scope again.', code: 'stale_preview' } })
      tokens.delete(String(body.preview_token))
      const grant = grantRow(ws, engine.grants[ws].length + 1)
      engine.grants[ws].push(grant)
      return route.fulfill({ json: { grant, mode: 'review' } })
    }
    return route.fulfill({ json: {
      workspace: ws, chat_evidence: { available: false, status: 'remote_client' }, mode: 'review', fields: ['decision', 'reason', 'effective_date'],
      producers: ['memory_extraction', 'nightly_curation'], disclosure_tokens: { [p.id]: 'scope' },
      brains: [{ name: p.brain, identity: `github.com/acme/${p.brain}` }],
      projects: [{ project_id: p.id, name: p.name, sources: [{
        source_id: 'document:Decisions.md', kind: 'document', label: 'Decision document · Decisions.md',
        project_id: p.id, project_name: p.name, entry_count: 1,
      }] }],
      grants: engine.grants[ws],
    } })
  })
  return engine
}

async function openSources(page: Page): Promise<Locator> {
  const panel = card(page)
  await panel.getByRole('button', { name: 'Sources and consent' }).click()
  const sources = panel.locator('.sources-panel')
  await expect(sources.getByText('Review', { exact: true })).toBeVisible()
  await sources.getByLabel('Actual source', { exact: true }).selectOption('document:Decisions.md')
  await sources.getByLabel('Only this decision', { exact: true }).check()
  await sources.getByLabel('Decision in Decisions.md', { exact: true }).selectOption('0')
  await sources.getByLabel('reason', { exact: true }).uncheck()
  await sources.getByLabel('effective_date', { exact: true }).uncheck()
  return sources
}

async function switchToBeta(page: Page): Promise<void> {
  await activeWorkspaceButton(page).click()
  await page.locator('#workspace-scope-menu [role="menuitem"]').filter({ hasText: /beta/i }).first().click()
  await expect(activeWorkspaceButton(page)).toContainText('Beta')
}

async function expectNoneOf(page: Page, ws: WS): Promise<void> {
  const text = (await page.locator('body').innerText()) + (await page.content())
  for (const term of SENTINELS[ws]) expect(text, term).not.toContain(term)
}

test.use({ viewport: { width: 1280, height: 900 } })

test.describe('sources and consent across two workspaces', () => {
  test.beforeEach(async ({ page }, testInfo) => {
    await isolate(page, `share-isolation-${testInfo.testId}`)
  })

  test('the foreign workspace never reaches the listing, the preview or the requests', async ({ page }) => {
    const engine = await stubEngine(page)
    await boot(page, OVERVIEW, '.settings-main .section-title')
    const sources = await openSources(page)
    await quiesce(page)
    await expect(sources.getByLabel('Shared brain', { exact: true }).locator('option')).toHaveText([
      'alpha-notes — github.com/acme/alpha-notes',
    ])
    await sources.getByRole('button', { name: 'Preview exact scope' }).click()
    await expect(sources.locator('.sources-preview')).toContainText('ALPHA-ONLY decision')
    await expectNoneOf(page, 'beta')
    // Nothing the page asked for named the other workspace.
    expect(engine.requests.filter((r) => r.includes('/beta/'))).toEqual([])
    expect(engine.grants.beta).toHaveLength(1)
  })

  test('a tampered preview cannot change what is confirmed', async ({ page }) => {
    const engine = await stubEngine(page)
    await boot(page, OVERVIEW, '.settings-main .section-title')
    const sources = await openSources(page)
    await sources.getByRole('button', { name: 'Preview exact scope' }).click()
    const preview = sources.locator('.sources-preview')
    await expect(preview).toContainText('ALPHA-ONLY decision')
    // Editing the rendered values in the DOM changes nothing the client sends.
    await preview.evaluate((el) => {
      const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT)
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        if (node.nodeValue?.includes('ALPHA-ONLY decision')) node.nodeValue = 'FORGED decision'
      }
    })
    await expect(preview).toContainText('FORGED decision')
    await preview.getByRole('button', { name: 'Confirm and grant' }).click()
    await expect.poll(() => engine.grants.alpha.length).toBe(1)
    expect(engine.confirms).toHaveLength(1)
    expect(Object.keys(engine.confirms[0].body).sort()).toEqual(['confirm', 'preview_token'])
  })

  test('a forged token is refused in the panel and no grant is listed', async ({ page }) => {
    const engine = await stubEngine(page)
    // Registered after the engine, so it runs first and rewrites the outgoing token.
    await page.route(/\/share-evidence\/confirm$/, (route) => {
      const body = route.request().postDataJSON() as Record<string, unknown>
      return route.fallback({ postData: JSON.stringify({ ...body, preview_token: 'sev_beta_forged' }) })
    })
    await boot(page, OVERVIEW, '.settings-main .section-title')
    const sources = await openSources(page)
    await sources.getByRole('button', { name: 'Preview exact scope' }).click()
    await sources.getByRole('button', { name: 'Confirm and grant' }).click()
    await expect(sources.getByRole('alert')).toContainText('This preview is no longer valid')
    expect(engine.grants.alpha).toEqual([])
    await expect(sources.locator('.sources-grant-row')).toHaveCount(0)
  })

  test('switching workspace before submit drops the preview and writes nothing', async ({ page }) => {
    const engine = await stubEngine(page)
    await boot(page, OVERVIEW, '.settings-main .section-title')
    const sources = await openSources(page)
    await sources.getByRole('button', { name: 'Preview exact scope' }).click()
    await expect(sources.locator('.sources-preview')).toContainText('ALPHA-ONLY decision')

    await switchToBeta(page)
    await expect(sources.locator('.sources-preview')).toHaveCount(0)
    await expect(sources.locator('.sources-grant-row')).toContainText('grn_bbbb')
    await expectNoneOf(page, 'alpha')
    expect(engine.confirms).toEqual([])
    expect(engine.grants.alpha).toEqual([])
  })

  test('a confirm still in flight when the workspace changes never lands in the other workspace', async ({ page }) => {
    const engine = await stubEngine(page, false)
    let release!: () => void
    engine.holdConfirm = { release: () => release(), gate: new Promise<void>((resolve) => { release = resolve }) }
    await boot(page, OVERVIEW, '.settings-main .section-title')
    const sources = await openSources(page)
    await sources.getByRole('button', { name: 'Preview exact scope' }).click()
    await sources.getByRole('button', { name: 'Confirm and grant' }).click()
    await expect.poll(() => engine.confirms.length).toBe(1)
    expect(engine.confirms[0].ws).toBe('alpha')

    await switchToBeta(page)
    // Wait for the held response to reach the page, then let the UI settle, before any negative check.
    const landed = page.waitForResponse((res) => res.url().endsWith('/alpha/share-evidence/confirm'))
    engine.holdConfirm.release()
    await landed
    await quiesce(page)
    expect(engine.grants.alpha).toHaveLength(1)

    // The alpha grant exists in alpha only; beta's panel stays empty and shows no alpha text.
    await expect(sources.getByText('Review', { exact: true })).toBeVisible()
    await expect(sources.locator('.sources-grant-row')).toHaveCount(0)
    await expect(sources.getByText('The grant was recorded')).toHaveCount(0)
    await expectNoneOf(page, 'alpha')
    expect(engine.grants.beta).toEqual([])
  })
})
