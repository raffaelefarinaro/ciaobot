// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

// The composer model popover's footer (Thinking segmented control and the
// Permission mode tiles) is laid out by ChatPanel's scoped CSS. jsdom does not
// lay out, so these guards read the rules that keep the popover width honest.
const here = dirname(fileURLToPath(import.meta.url))
const panel = readFileSync(join(here, '../ChatPanel.vue'), 'utf8')

function ruleBody(selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const match = new RegExp(`(?:^|\\n)${escaped}\\s*\\{([^}]*)\\}`).exec(panel)
  if (!match) throw new Error(`No rule for ${selector}`)
  return match[1]
}

describe('composer model popover footer layout', () => {
  it('keeps the Thinking segments on one row that scrolls, never wrapping Max onto a second line', () => {
    const chips = ruleBody('.thinking-levels__chips')
    expect(chips).toMatch(/flex-wrap:\s*nowrap/)
    expect(chips).toMatch(/overflow-x:\s*auto/)
    expect(chips).not.toMatch(/flex-wrap:\s*wrap/)
    expect(ruleBody('.thinking-levels')).toMatch(/flex-direction:\s*column/)
    expect(ruleBody('.thinking-levels')).not.toMatch(/space-between/)
    // Each segment keeps its own width, so the row can scroll instead of squeezing.
    expect(ruleBody('.thinking-levels__chips .thinking-chip')).toMatch(/flex:\s*1 0 auto/)
  })

  it('draws the permission tiles as an even grid: equal row heights, two lines allowed', () => {
    const choices = ruleBody('.permission-modes__choices')
    expect(choices).toMatch(/grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\)/)
    expect(choices).toMatch(/grid-auto-rows:\s*1fr/)
    // Tiles stay at least a 44px target, and a tall description cannot shrink them.
    expect(ruleBody('.permission-mode')).toMatch(/min-height:\s*44px/)
  })
})
