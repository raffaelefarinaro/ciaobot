/**
 * Every `var(--token)` a component uses must resolve to a token this app defines.
 *
 * The proposal review panel referenced `--surface-2`, `--surface-3`, `--text`,
 * `--text-muted`, `--radius-2`, `--danger` and `--warn` — none of which exist
 * here. CSS custom properties fail silently: each rule quietly used its literal
 * fallback, so the panel ignored the light theme entirely and its sticky batch
 * bar rendered as a 4%-white wash the row underneath showed through, which read
 * as a rendering fault. Nothing failed; it just looked wrong.
 *
 * KNOWN_GAPS records the offenders that predate this guard. They are listed, not
 * fixed, on purpose: each one needs a look at the component in both themes to
 * choose the right replacement, and a blind rename would trade a silent wrong
 * colour for a different silent wrong colour. The point of the list is that it
 * can only shrink — a new undefined token fails this test.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

const SRC = join(__dirname, '..', '..')
const COMPONENTS = join(SRC, 'components')

/** Every source file that could set a custom property at runtime. `--app-h`
 * lives in `lib/viewport.ts` and `--font-scale` in `composables/useFontScale.ts`,
 * so a fixed list of directories would have called both undefined. */
function runtimeSources(): string[] {
  const out: string[] = []
  const walk = (dir: string) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      if (entry.name === 'node_modules' || entry.name === '__tests__') continue
      const path = join(dir, entry.name)
      if (entry.isDirectory()) walk(path)
      else if (path.endsWith('.ts') || path.endsWith('.vue')) out.push(path)
    }
  }
  walk(SRC)
  return out
}

const KNOWN_GAPS: Record<string, string[]> = {
  'ChatPanel.vue': ['--danger', '--fg-muted', '--hover', '--muted'],
  'CsvViewer.vue': ['--text-muted'],
  'ExcalidrawViewer.vue': ['--primary'],
  'FileViewerModal.vue': ['--danger', '--fg-muted', '--ok'],
  'HtmlArtifactViewer.vue': ['--text-muted'],
  'NotificationBell.vue': ['--danger'],
  'PinnedFilePanel.vue': ['--danger', '--fg-muted', '--ok'],
  'ProjectView.vue': ['--fg1'],
  'SettingsView.vue': ['--fg-muted', '--radius-md'],
}

/** Comments describe bugs like this one, so they must not be scanned for them. */
function stripComments(text: string): string {
  return text
    .replace(/<!--[\s\S]*?-->/g, ' ')
    .replace(/\/\*[\s\S]*?\*\//g, ' ')
}

function definedTokens(): Set<string> {
  const names = new Set<string>()
  for (const file of ['App.vue', 'main.ts', 'style.css']) {
    let text = ''
    try {
      text = readFileSync(join(SRC, file), 'utf8')
    } catch {
      continue
    }
    for (const m of stripComments(text).matchAll(/(--[a-z0-9-]+)\s*:/g)) names.add(m[1])
  }
  // Tokens the app sets at runtime (`--app-h`, `--font-scale`) are just as real,
  // and the code that sets them lives wherever it is needed — a component, not
  // only App.vue — so this looks across the tree rather than at a fixed list.
  for (const file of runtimeSources()) {
    for (const m of readFileSync(file, 'utf8').matchAll(/setProperty\(\s*['"](--[a-z0-9-]+)/g)) {
      names.add(m[1])
    }
  }
  return names
}

function usedTokens(text: string): Set<string> {
  const body = stripComments(text)
  const used = new Set<string>()
  for (const m of body.matchAll(/var\((--[a-z0-9-]+)/g)) used.add(m[1])
  // Locally declared or bound via :style — including quoted keys in an object
  // literal — belongs to the component, not to the palette.
  for (const m of body.matchAll(/['"]?(--[a-z0-9-]+)['"]?\s*:/g)) used.delete(m[1])
  for (const m of body.matchAll(/setProperty\(\s*['"](--[a-z0-9-]+)/g)) used.delete(m[1])
  return used
}

describe('CSS custom properties', () => {
  const defined = definedTokens()
  const files = readdirSync(COMPONENTS).filter(f => f.endsWith('.vue'))

  it('reads the palette out of App.vue', () => {
    for (const token of ['--bg2', '--bg3', '--bg-elev', '--fg2', '--fg3', '--warning', '--error']) {
      expect(defined.has(token), token).toBe(true)
    }
    expect(defined.has('--app-h')).toBe(true)   // set at runtime, still real
  })

  it('covers every component', () => {
    expect(files.length).toBeGreaterThan(20)
  })

  for (const file of files) {
    it(`${file} uses only defined tokens`, () => {
      const text = readFileSync(join(COMPONENTS, file), 'utf8')
      const unknown = [...usedTokens(text)].filter(t => !defined.has(t)).sort()
      expect(unknown).toEqual((KNOWN_GAPS[file] ?? []).slice().sort())
    })
  }

  it('the known-gap list stays honest', () => {
    // A file that no longer offends must leave the list, or the list becomes a
    // place where fixed bugs are recorded as outstanding.
    for (const file of Object.keys(KNOWN_GAPS)) {
      expect(files, `${file} is listed but does not exist`).toContain(file)
      const unknown = [...usedTokens(readFileSync(join(COMPONENTS, file), 'utf8'))]
        .filter(t => !defined.has(t))
      expect(unknown.length, `${file} is clean now — remove it from KNOWN_GAPS`).toBeGreaterThan(0)
    }
  })
})
