import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const css = readFileSync(resolve(__dirname, '../App.vue'), 'utf8')

function rule(selector: string): string {
  const start = css.indexOf(`\n${selector} {`)
  expect(start, `${selector} rule`).toBeGreaterThan(-1)
  const end = css.indexOf('}', start)
  return css.slice(start, end)
}

describe('shared button styles', () => {
  it('a small primary has exactly the btn-small box', () => {
    const body = rule('.btn-primary.btn-small')
    expect(body).toMatch(/padding:\s*8px 16px/)
    expect(body).toMatch(/font-size:\s*var\(--text-sm\)/)
  })

  it('a disabled primary is a neutral surface, never a faded accent', () => {
    const body = rule('.btn-primary:disabled')
    expect(body).not.toContain('opacity')
    expect(body).toMatch(/background:\s*var\(--bg3\)/)
  })

  it('the warning hint uses full-contrast text', () => {
    expect(rule('.hint--warn')).toMatch(/color:\s*var\(--fg\);/)
  })
})
