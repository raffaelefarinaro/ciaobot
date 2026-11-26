import { describe, expect, it } from 'vitest'
import { readdirSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { AA_BODY, contrastRatio } from '../contrast'

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
  ])('--error-text clears AA on every %s surface', (_name, selector, base) => {
    const css = block(selector)
    const fallback = base ? block(base) : css
    const fg = /--error-text:\s*var\(--error\)/.test(css) ? token(css, 'error') : token(css, 'error-text')
    for (const surface of ['bg', 'bg2', 'bg3', 'bg-elev']) {
      const bg = (css.includes('--' + surface + ':') ? css : fallback)
      expect(contrastRatio(fg, token(bg, surface)), surface).toBeGreaterThanOrEqual(AA_BODY)
    }
  })

  it('no component paints text with the fill token --error', () => {
    const files = Object.fromEntries(
      (readdirSync(srcDir, { recursive: true }) as string[])
        .filter(f => /\.(vue|css)$/.test(f))
        .map(f => [f, readFileSync(join(srcDir, f), 'utf8')]),
    )
    const offenders = Object.entries(files)
      .filter(([, src]) => /(?<![-\w])color:\s*var\(--error(?![-\w])/.test(src))
      .map(([path]) => path)
    expect(offenders).toEqual([])
  })
})
