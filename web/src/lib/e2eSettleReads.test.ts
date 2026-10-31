import { readdirSync, readFileSync } from 'node:fs'
import { join, relative } from 'node:path'
import { describe, expect, it } from 'vitest'

// Why: #2288 and #2295 were one-shot reads taken right after a pointer
// release, racing the re-render or the slide-in. Poll the read, or await
// settled(locator) from e2e/support/app.ts before measuring.
const specsDir = join(import.meta.dirname, '../../e2e/specs')

const RELEASE = /\.mouse\.up\(|\.dragTo\(|\.touchscreen\.tap\(|dispatchEvent\(\s*['"]pointerup|new PointerEvent\(\s*['"]pointerup|releasePointerInSameTask\(/
const ONE_SHOT = /\bexpect\(\(*await\b|\.boundingBox\(\)/
const SETTLE = /\bsettled\(/
const OPT_OUT = /\/\/ settle-ok: \S/
const WINDOW = 6

interface UnsettledRead { line: number; after: number; text: string }

/** Same-file functions that end a drag: their body releases, directly or through another such helper. */
function releaseHelpers(lines: string[]): string[] {
  const fns: Array<{ name: string; body: string }> = []
  lines.forEach((line, i) => {
    const m = /^(?:export )?(?:async )?function (\w+)\(/.exec(line)
    if (!m) return
    let end = i + 1
    while (end < lines.length && !/^}/.test(lines[end])) end++
    fns.push({ name: m[1], body: lines.slice(i + 1, end).join('\n') })
  })
  const helpers = new Set<string>()
  for (let grew = true; grew;) {
    grew = false
    for (const fn of fns) {
      if (helpers.has(fn.name)) continue
      if (RELEASE.test(fn.body) || [...helpers].some((h) => new RegExp(`\\b${h}\\(`).test(fn.body))) {
        helpers.add(fn.name)
        grew = true
      }
    }
  }
  return [...helpers]
}

function findUnsettledReads(source: string): UnsettledRead[] {
  const lines = source.split('\n')
  const helpers = releaseHelpers(lines)
  const isRelease = (line: string) =>
    RELEASE.test(line) || helpers.some((h) => new RegExp(`\\bawait ${h}\\(`).test(line))
  const found: UnsettledRead[] = []
  lines.forEach((line, i) => {
    if (!isRelease(line)) return
    const indent = line.search(/\S/)
    let seen = 0
    for (let j = i + 1; j < lines.length && seen < WINDOW; j++) {
      const text = lines[j].trim()
      if (!text || text.startsWith('//') || text.startsWith('*')) continue
      if (lines[j].search(/\S/) < indent && /^[})]/.test(text)) break
      if (SETTLE.test(text) || isRelease(lines[j])) break
      seen++
      if (ONE_SHOT.test(text) && !OPT_OUT.test(lines[j])) found.push({ line: j + 1, after: i + 1, text })
    }
  })
  return found
}

function specFiles(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name)
    return entry.isDirectory() ? specFiles(path) : entry.name.endsWith('.spec.ts') ? [path] : []
  })
}

describe('findUnsettledReads', () => {
  it('flags expect(await read) right after mouse.up', () => {
    const source = [
      '  await page.mouse.up()',
      '  expect(await rowOrder(page)).toEqual([])',
    ].join('\n')
    const hits = findUnsettledReads(source)
    expect(hits).toHaveLength(1)
    expect(hits[0]).toMatchObject({ line: 2, after: 1 })
  })

  it('flags a boundingBox read after dragTo', () => {
    const source = [
      '  await a.dragTo(b)',
      '  const box = (await a.boundingBox())!',
    ].join('\n')
    expect(findUnsettledReads(source)).toHaveLength(1)
  })

  it('does not flag expect.poll or web-first assertions', () => {
    const source = [
      '  await page.mouse.up()',
      '  await expect.poll(() => rowOrder(page)).toEqual([])',
      '  await expect(x).toHaveCount(0)',
    ].join('\n')
    expect(findUnsettledReads(source)).toEqual([])
  })

  it('settled() ends the window', () => {
    const source = [
      '  await page.mouse.up()',
      '  await settled(tile)',
      '  const box = (await tile.boundingBox())!',
    ].join('\n')
    expect(findUnsettledReads(source)).toEqual([])
  })

  it('stops after six code lines and at the end of the block', () => {
    const fillers = Array.from({ length: 6 }, () => '  expect(sent).toHaveLength(1)')
    const sixLines = ['  await page.mouse.up()', ...fillers, '  expect(await f())'].join('\n')
    expect(findUnsettledReads(sixLines)).toEqual([])

    const blockEnd = [
      'async function drag(page) {',
      '  await page.mouse.up()',
      '}',
      'expect(await f())',
    ].join('\n')
    expect(findUnsettledReads(blockEnd)).toEqual([])
  })

  it('follows a same-file helper that releases, transitively', () => {
    const source = [
      'async function drag(page) {',
      '  await page.mouse.up()',
      '}',
      'async function dragTo2(page) {',
      '  await drag(page)',
      '}',
      'test("drags", async ({ page }) => {',
      '  await dragTo2(page)',
      '  expect(await f())',
      '})',
    ].join('\n')
    const hits = findUnsettledReads(source)
    expect(hits).toHaveLength(1)
    expect(hits[0].line).toBe(9)
  })

  it('honours // settle-ok: with a reason only', () => {
    const exempt = [
      '  await page.mouse.up()',
      '  expect(await rowOrder(page)).toEqual([]) // settle-ok: announced above',
    ].join('\n')
    expect(findUnsettledReads(exempt)).toEqual([])

    const bare = [
      '  await page.mouse.up()',
      '  expect(await rowOrder(page)).toEqual([]) // settle-ok:',
    ].join('\n')
    expect(findUnsettledReads(bare)).toHaveLength(1)
  })
})

describe('Playwright specs', () => {
  it('scan sees the spec suite', () => {
    expect(specFiles(specsDir).length).toBeGreaterThan(40)
  })

  it('do not read the page once right after a pointer release', () => {
    const offenders = specFiles(specsDir).flatMap((file) =>
      findUnsettledReads(readFileSync(file, 'utf8')).map(
        (r) => `${relative(specsDir, file)}:${r.line} (released on L${r.after}): ${r.text}`,
      ),
    )
    expect(
      offenders,
      'Poll one-shot reads with `await expect.poll(() => read())…`, or `await settled(locator)` before measuring geometry, or append `// settle-ok: <why>` if the line is genuinely stable.',
    ).toEqual([])
  })
})
