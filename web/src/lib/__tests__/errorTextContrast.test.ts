import { describe, expect, it } from 'vitest'
import { readdirSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { AA_BODY, contrastRatio, mix } from '../contrast'

const srcDir = join(dirname(fileURLToPath(import.meta.url)), '..', '..')
const appCss = readFileSync(join(srcDir, 'styles', 'app.css'), 'utf8')

// #3668: red text at 0.8rem on --bg2 measured 4.19:1 in the dark theme. Red
// text uses --error-text; --error stays for fills and borders.
function block(selector: string): string {
  const start = appCss.indexOf(selector + ' {')
  expect(start, selector).toBeGreaterThan(-1)
  return appCss.slice(start, appCss.indexOf('\n}', start))
}

function token(css: string, name: string): string {
  const m = new RegExp('--' + name + ':\\s*(#[0-9a-fA-F]{6})').exec(css)
  expect(m, name).not.toBeNull()
  return m![1]
}

describe('error text contrast', () => {
  it.each([
    ['dark', ':root', null],
    ['light', ':root.theme-light', ':root'],
  ])('--error-text clears AA on every %s surface, bare and under the badge tint', (_name, selector, base) => {
    const css = block(selector)
    const fallback = base ? block(base) : css
    const fg = token(css, 'error-text')
    // .badge--error and the 12% red lines tint the surface with rgb(244 67 54).
    const tint = '#f44336'
    for (const surface of ['bg', 'bg2', 'bg3', 'bg-elev']) {
      const bg = token(css.includes('--' + surface + ':') ? css : fallback, surface)
      expect(contrastRatio(fg, bg), surface).toBeGreaterThanOrEqual(AA_BODY)
      for (const t of [0.12, 0.15]) {
        expect(contrastRatio(fg, mix(bg, tint, t)), surface + ' tint ' + t).toBeGreaterThanOrEqual(AA_BODY)
      }
    }
  })

  it('the e2e harness defines the token next to --error', () => {
    const html = readFileSync(join(srcDir, '..', 'e2e', 'fixture', 'harness', 'index.html'), 'utf8')
    expect(html).toContain('--error-text: ' + token(block(':root'), 'error-text'))
  })

  it('nothing paints text with the fill token --error, directly or through color-mix', () => {
    const files = (readdirSync(srcDir, { recursive: true }) as string[])
      .filter(f => /\.(vue|css|ts)$/.test(f) && !f.includes('__tests__') && !/\.test\.ts$/.test(f))
      .map(f => [f, readFileSync(join(srcDir, f), 'utf8')] as const)
    // `color:` only: border-color, background and text-decoration-color are fills.
    const direct = /(?<![-\w])color:\s*var\(--error(?![-\w])/
    const mixed = /(?<![-\w])color:\s*color-mix\([^;]*var\(--error(?![-\w])/
    const offenders = files.filter(([, src]) => direct.test(src) || mixed.test(src)).map(([f]) => f)
    expect(offenders).toEqual([])
  })
})
