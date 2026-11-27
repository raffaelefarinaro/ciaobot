// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

// The composer model popover's footer (the Thinking segmented control and the
// Permission mode menu button) is laid out by ChatPanel's scoped CSS, and the
// list's share of the popover by ModelSelector's. jsdom does not lay out, so
// these guards read the rules that keep the model list the roomy part.
const here = dirname(fileURLToPath(import.meta.url))
const panel = readFileSync(join(here, '../ChatPanel.vue'), 'utf8')
const selector = readFileSync(join(here, '../ModelSelector.vue'), 'utf8')

function ruleBody(source: string, selectorText: string): string {
  const escaped = selectorText.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const match = new RegExp(`(?:^|\\n)${escaped}\\s*\\{([^}]*)\\}`).exec(source)
  if (!match) throw new Error(`No rule for ${selectorText}`)
  return match[1]
}

describe('composer model popover footer layout', () => {
  it('keeps the Thinking segments on one row that scrolls, never wrapping Max onto a second line', () => {
    const chips = ruleBody(panel, '.thinking-levels__chips')
    expect(chips).toMatch(/flex-wrap:\s*nowrap/)
    expect(chips).toMatch(/overflow-x:\s*auto/)
    expect(chips).not.toMatch(/flex-wrap:\s*wrap/)
    // Each segment keeps its own width, so the row can scroll instead of squeezing.
    expect(ruleBody(panel, '.thinking-levels__chips .thinking-chip')).toMatch(/flex:\s*1 0 auto/)
  })

  it('lays Thinking and Permission mode out as two label/control rows that stack on a narrow sheet', () => {
    const groups = ruleBody(panel, '.picker-footer-groups')
    expect(groups).toMatch(/display:\s*grid/)
    expect(groups).toMatch(/grid-template-columns:\s*max-content minmax\(0,\s*1fr\)/)
    expect(groups).toMatch(/container-type:\s*inline-size/)
    expect(ruleBody(panel, '.thinking-levels,\n.permission-modes')).toMatch(/display:\s*contents/)
    expect(panel).toMatch(/@container \(max-width: 420px\) \{\s*\.picker-footer-groups \{\s*grid-template-columns:\s*minmax\(0,\s*1fr\)/)
    // The note lines run under both rows.
    expect(ruleBody(panel, '.permission-modes__note,\n.permission-modes__unknown,\n.permission-modes__status')).toMatch(/grid-column:\s*1 \/ -1/)
  })

  it('gives the model list the popover: wide, up to 640px tall, the list the only flexible part', () => {
    const popover = ruleBody(selector, '.model-selector__popover')
    expect(popover).toMatch(/width:\s*min\(520px,\s*calc\(100vw - 24px\)\)/)
    expect(popover).toMatch(/max-height:\s*min\(\s*640px/)
    expect(ruleBody(selector, '.model-selector__list')).toMatch(/flex:\s*1 1 auto/)
    expect(ruleBody(selector, '.model-selector__list')).toMatch(/overflow-y:\s*auto/)
    expect(ruleBody(selector, '.model-selector__section-header')).toMatch(/position:\s*sticky/)
  })
})
