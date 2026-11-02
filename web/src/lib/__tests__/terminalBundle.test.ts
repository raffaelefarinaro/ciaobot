// @vitest-environment node

/**
 * xterm as the production build ships it.
 *
 * The unit suite stubs xterm, so it cannot see what the bundler does to the
 * real one. Vite minifies every chunk with esbuild at the configured build
 * target, and below ES2021 esbuild lowers `x ||= y`. esbuild 0.25 gets that
 * lowering wrong under minification when `x` is a `let` with no initialiser:
 * it drops the declaration and assigns an undeclared name. xterm 6's DECRQM
 * handler (`CSI ? Ps $ p`, the mode query every modern TUI sends at start-up)
 * is written exactly that way, so the shipped terminal threw a ReferenceError
 * in the middle of a parse, stopped acknowledging writes, and the pane showed
 * nothing until its output queue overflowed.
 *
 * This runs the same transform over xterm's own ES module, with the target
 * the app builds with, and asks the result to answer a mode query.
 */

import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { transformWithEsbuild, type UserConfig } from 'vite'
import { afterAll, describe, expect, it } from 'vitest'
import viteConfig from '../../../vite.config'

type XtermModule = typeof import('@xterm/xterm')

const XTERM_ESM = resolve(__dirname, '../../../node_modules/@xterm/xterm/lib/xterm.mjs')

/** The build target `vite build` uses for the app. */
function appBuildTarget(): string | string[] {
  const config = (viteConfig as (env: { mode: string; command: 'build' }) => UserConfig)({
    mode: 'production',
    command: 'build',
  })
  const target = config.build?.target
  if (typeof target !== 'string' && !Array.isArray(target)) {
    throw new Error('vite.config.ts must set build.target explicitly')
  }
  return target
}

const scratch = mkdtempSync(join(tmpdir(), 'ciao-xterm-bundle-'))

afterAll(() => {
  rmSync(scratch, { recursive: true, force: true })
})

/** xterm, minified the way the production build minifies it. */
async function buildXterm(target: string | string[]): Promise<XtermModule> {
  const source = readFileSync(XTERM_ESM, 'utf8')
  const { code } = await transformWithEsbuild(source, XTERM_ESM, { minify: true, target, format: 'esm' })
  const file = join(scratch, `xterm-${Array.isArray(target) ? target.join('_') : target}.mjs`)
  writeFileSync(file, code)
  return (await import(/* @vite-ignore */ pathToFileURL(file).href)) as XtermModule
}

/** Write bytes and wait for xterm's own write callback. */
function write(terminal: InstanceType<XtermModule['Terminal']>, data: string): Promise<void> {
  return new Promise((resolve) => {
    terminal.write(data, resolve)
  })
}

describe('the xterm the build ships', () => {
  it('answers a DEC private mode query instead of throwing', async () => {
    const { Terminal } = await buildXterm(appBuildTarget())
    const terminal = new Terminal({ cols: 80, rows: 24 })
    const replies: string[] = []
    terminal.onData((data) => replies.push(data))

    // OpenCode's start-up probe, trimmed to the mode queries: synchronized
    // output, bracketed paste, focus reporting, then an ANSI mode.
    await write(terminal, '\x1b[?2026$p\x1b[?2004$p\x1b[?1004$p\x1b[4$p')

    expect(replies).toEqual(['\x1b[?2026;2$y', '\x1b[?2004;2$y', '\x1b[?1004;2$y', '\x1b[4;2$y'])
    // The parser is still alive: the next write is parsed and acknowledged.
    await write(terminal, 'still drawing')
    expect(terminal.buffer.active.getLine(0)?.translateToString(true)).toBe('still drawing')
    terminal.dispose()
  }, 20_000)
})
