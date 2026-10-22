// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const script = readFileSync(resolve(process.cwd(), '../site/assets/site.js'), 'utf8')
const homepage = readFileSync(resolve(process.cwd(), '../site/index.html'), 'utf8')
const repo = 'https://api.github.com/repos/raffaelefarinaro/ciaobot'

describe('website download statistics', () => {
  beforeEach(() => {
    document.body.innerHTML = homepage
    sessionStorage.clear()
    // Only exercise the statistics block, not the homepage's interactive demos.
    document.body.innerHTML = document.querySelector('#repo-stats')!.outerHTML
  })

  afterEach(() => vi.unstubAllGlobals())

  async function render(assets: { name: string; download_count: number }[]) {
    const fetch = vi.fn(async (url: string) => ({
      ok: true,
      json: async () => url === repo ? { stargazers_count: 12 } : [{ assets }],
    }))
    vi.stubGlobal('fetch', fetch)
    new Function(script)()
    await vi.waitFor(() => expect(document.querySelector<HTMLElement>('#repo-stats')!.hidden).toBe(false))
    return fetch
  }

  it('counts current engine wheels without counting supporting release assets', async () => {
    await render([
      { name: 'ciaobot-1.6.1-py3-none-any.whl', download_count: 3 },
      { name: 'install.sh', download_count: 40 },
      { name: 'install-engine.sh', download_count: 20 },
      { name: 'install.ps1', download_count: 30 },
      { name: 'ciaobot-engine-manifest.json', download_count: 50 },
      { name: 'ciaobot-engine-manifest.json.sig', download_count: 50 },
      { name: 'ciaobot-server-host-macos-universal-v1.tar.gz', download_count: 10 },
      { name: 'other-1.0-py3-none-any.whl', download_count: 100 },
      { name: 'ciaobot-1.6.1-py3-none-any.whl.sig', download_count: 100 },
    ])
    expect(document.querySelector('[data-stat="downloads"]')!.textContent).toBe('3')
    expect(document.querySelector('[data-stat="stars"]')!.textContent).toBe('12')
  })

  it('keeps historical desktop downloads in the total', async () => {
    await render([
      { name: 'ciaobot-1.6.1-py3-none-any.whl', download_count: 3 },
      { name: 'Ciaobot.app.tar.gz', download_count: 5 },
      { name: 'Ciaobot.dmg', download_count: 7 },
      { name: 'Ciaobot.app.tar.gz.sig', download_count: 99 },
    ])
    expect(document.querySelector('[data-stat="downloads"]')!.textContent).toBe('15')
  })

  it('ignores old cached totals and reuses the corrected cache for ten minutes', async () => {
    sessionStorage.setItem('ciaobot-repo-stats', JSON.stringify({ stars: 1, downloads: 999, at: Date.now() }))
    const fetch = await render([{ name: 'ciaobot-1.6.1-py3-none-any.whl', download_count: 3 }])
    expect(document.querySelector('[data-stat="downloads"]')!.textContent).toBe('3')
    fetch.mockClear()
    new Function(script)()
    expect(fetch).not.toHaveBeenCalled()
    expect(document.querySelector('[data-stat="downloads"]')!.textContent).toBe('3')
  })

  it('refreshes corrected totals once the ten-minute cache expires', async () => {
    sessionStorage.setItem('ciaobot-repo-stats-v2', JSON.stringify({
      stars: 1, downloads: 999, at: Date.now() - 600001,
    }))
    const fetch = await render([{ name: 'ciaobot-1.6.1-py3-none-any.whl', download_count: 4 }])
    expect(fetch).toHaveBeenCalledTimes(2)
    expect(document.querySelector('[data-stat="downloads"]')!.textContent).toBe('4')
  })
})
