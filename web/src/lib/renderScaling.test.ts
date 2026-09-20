// @vitest-environment jsdom
/**
 * Transcript rendering must not rebuild the known-path rules per span.
 *
 * Measured in #494 and filed as #501: a 5x longer transcript took ~85x
 * longer to render. The dominant factor was rule-building in `linkifyHtml`.
 * `buildKnownPathRules` ran once per *text span* of every message, over a
 * known-path list that itself grows with the transcript, and the build was
 * quadratic in the size of that list. Three terms, all growing together.
 *
 * That factor is what these tests pin. They do NOT prove the render is
 * linear: `knownPathMatches` still scans every rule per span, so an
 * O(paths x spans) term survives. Removing it is an algorithm change, not a
 * cache, and is deliberately not in this change.
 *
 * The guard here is a work count, not a stopwatch. A duration depends on the
 * machine and on what else the suite is running; "the rules were computed
 * once for this path list" does not, and it is the entire invariant — a
 * rebuild that creeps back into a per-span or per-message loop shows up as a
 * count, immediately and without flaking.
 */
import { describe, expect, it } from 'vitest'
import { buildKnownPathRules, knownPathRulesBuildCount, linkifyHtml } from './filePaths'
import { renderMarkdown } from './safeMarkdown'

function knownPaths(n: number): string[] {
  return Array.from(
    { length: n },
    (_, i) => `/Users/x/repos/proj/src/module${i}/file${i}.ts`,
  )
}

// A message shaped like a real assistant turn: prose, an inline path, a list
// and a fenced block. Its markdown renders to dozens of text spans, which is
// what made the per-span rule rebuild so expensive.
const BODY = 'Updated the loader in `src/module3/file3.ts` and re-ran the suite.\n\n'
  + '- handles the empty manifest edge case\n'
  + '- fixes the regression from yesterday\n\n'
  + '```ts\nconst x = load(manifest)\n```\n\n'
  + 'See notes.md for details.\n'

describe('transcript render cost', () => {
  it('builds the known-path rules once for a whole transcript', () => {
    const paths = knownPaths(100)
    const before = knownPathRulesBuildCount()
    for (let i = 0; i < 200; i++) renderMarkdown(`${BODY}${i}`, paths)
    // 200 messages, dozens of text spans each, one rule build.
    expect(knownPathRulesBuildCount() - before).toBe(1)
  })

  it('builds the rules once per document, not once per span', () => {
    const paths = knownPaths(20)
    buildKnownPathRules(paths)  // warm the entry for this array
    const html = renderMarkdown(BODY, [])
    expect(html.split('<').length).toBeGreaterThan(10)  // many spans
    const before = knownPathRulesBuildCount()
    linkifyHtml(html, paths)
    expect(knownPathRulesBuildCount() - before).toBe(0)
  })

  it('serves one rule set per path list and never a stale one', () => {
    // Memoised on array identity, which is what lets one render pass over a
    // whole transcript share a single rule set.
    const paths = knownPaths(50)
    expect(buildKnownPathRules(paths)).toBe(buildKnownPathRules(paths))
    // A different list is a different entry, so a changed path set is never
    // served the previous set's rules.
    const other = knownPaths(50)
    expect(buildKnownPathRules(other)).not.toBe(buildKnownPathRules(paths))
    expect(buildKnownPathRules(other)).toEqual(buildKnownPathRules(paths))
  })

  it('renders identically whether or not the rules came from the cache', () => {
    const paths = knownPaths(60)
    const text = `${BODY} ${paths[7]} file7.ts`
    const first = renderMarkdown(text, paths)
    const second = renderMarkdown(text, [...paths])
    expect(second).toBe(first)
  })
})
