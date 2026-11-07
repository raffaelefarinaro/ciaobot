/**
 * The app-presence abstraction is gone (#659). The PWA has one runtime: a
 * browser or installed PWA served by the engine.
 *
 * The retired Tauri shell injected a document-start marker on `window` and
 * exposed a command bridge beside it. The bridge is gone for good, and so is
 * the helper that read both: a call site testing for something nothing sets
 * looks like it is doing something. This guard keeps them gone.
 *
 * The marker itself is live again (#2656): Ciaobot for Mac, a separate client
 * repository, sets it before the page runs. It has exactly one reader,
 * `isDesktopClient()` in `lib/pwaPlatform.ts`; every surface asks that probe
 * instead of reading the global, so there is one place to find and change.
 *
 * `isApplePlatform()` stays — that one is about the user's keyboard, not about
 * which app is running.
 */
import { describe, expect, it } from 'vitest'
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import * as platform from './platform'

const SRC = join(__dirname, '..')
const SELF = join(SRC, 'lib', 'platform.test.ts')

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
  bridge: '__TAU' + 'RI__',
}

/** The live marker, assembled the same way, and the one file allowed to read it. */
const MARKER = '__CIAOBOT' + '_DESKTOP__'
const MARKER_READER = join(SRC, 'lib', 'pwaPlatform.ts')

/** Every `.ts`/`.vue` under `web/src`, `__tests__` included: a test that uses
 * the retired helper or bridge asserts a branch that can no longer be taken. */
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
  it('isApplePlatform is the only thing lib/platform exports', () => {
    expect(Object.keys(platform)).toEqual(['isApplePlatform'])
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

  it('reads the desktop marker only in lib/pwaPlatform.ts', () => {
    const readers = sourceFiles()
      .filter(file => !file.includes('__tests__') && !file.endsWith('.test.ts'))
      .filter(file => readFileSync(file, 'utf8').includes(MARKER))
    expect(readers).toEqual([MARKER_READER])
  })
})
