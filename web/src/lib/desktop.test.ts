/**
 * The app-presence abstraction is gone (#659). The PWA has one runtime: a
 * browser or installed PWA served by the engine.
 *
 * The retired Tauri shell injected a document-start marker on `window` and
 * exposed a command bridge beside it. Nothing sets either now that the app
 * tree is gone, so the helper that read them was permanently false and every
 * branch it guarded took the browser path while still reading as live
 * behaviour. This guard keeps them gone: a marker that nothing sets is not a
 * feature, and a call site that tests for it looks like it is doing something.
 *
 * `isApplePlatform()` stays — that one is about the user's keyboard, not about
 * which app is running.
 */
import { describe, expect, it } from 'vitest'
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import * as desktop from './desktop'

const SRC = join(__dirname, '..')
const SELF = join(SRC, 'lib', 'desktop.test.ts')

/**
 * The retired names: the helper, the document-start marker and the command
 * bridge. They are assembled from fragments so this file does not match its own
 * scan — spelling them out would put the very references the guard forbids back
 * into `web/src`, which is the one thing the guard exists to prevent. The
 * acceptance check for #659 is a plain grep over that tree, and it has to come
 * back empty.
 */
const RETIRED: Record<string, string> = {
  helper: 'is' + 'DesktopApp',
  marker: '__CIAOBOT' + '_DESKTOP__',
  bridge: '__TAU' + 'RI__',
}

/** Every `.ts`/`.vue` under `web/src`, `__tests__` included: a test that sets
 * the marker is a test asserting a branch that can no longer be taken. */
function sourceFiles(): string[] {
  const out: string[] = []
  const walk = (dir: string) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      if (entry.name === 'node_modules') continue
      const path = join(dir, entry.name)
      if (entry.isDirectory()) walk(path)
      else if (path.endsWith('.ts') || path.endsWith('.vue')) out.push(path)
    }
  }
  walk(SRC)
  return out
}

describe('app-presence abstraction', () => {
  it('isApplePlatform is the only thing lib/desktop exports', () => {
    expect(Object.keys(desktop)).toEqual(['isApplePlatform'])
  })

  it('has no reference to the retired markers anywhere in web/src', () => {
    const offenders: string[] = []
    for (const file of sourceFiles()) {
      if (file === SELF) continue
      const text = readFileSync(file, 'utf8')
      for (const [label, needle] of Object.entries(RETIRED)) {
        if (text.includes(needle)) offenders.push(`${file.slice(SRC.length + 1)}: ${label}`)
      }
    }
    expect(offenders).toEqual([])
  })
})
