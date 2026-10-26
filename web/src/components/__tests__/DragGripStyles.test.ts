import { describe, expect, it } from 'vitest'
import source from '../DragGrip.vue?raw'

// A scoped `:global(row) .drag-grip` selector compiles to the row alone, which
// hid the grip and dimmed the hovered row (#2150). The reveal rules must stay in
// an unscoped block as plain descendant selectors.
describe('DragGrip reveal styles', () => {
  const blocks = [...source.matchAll(/<style([^>]*)>([\s\S]*?)<\/style>/g)]

  it('keeps the row-hover reveal in an unscoped style block', () => {
    const unscoped = blocks.filter(([, attrs]) => !/\bscoped\b/.test(attrs)).map(([, , css]) => css).join('\n')
    expect(unscoped).toContain('[data-drag-row]:hover .drag-grip')
    expect(unscoped).toContain('[data-drag-row]:focus-within .drag-grip')
  })

  it('never wraps the row in :global() inside a scoped block', () => {
    const scoped = blocks.filter(([, attrs]) => /\bscoped\b/.test(attrs)).map(([, , css]) => css).join('\n')
    expect(scoped).not.toMatch(/:global\(\[data-drag-row\]/)
  })
})
