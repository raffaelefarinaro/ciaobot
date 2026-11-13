// @vitest-environment jsdom
//
// Discovery of untouched file mentions in completed assistant replies. The
// composable batches the mentions to the engine's resolve route; these tests
// pin its budgets, generation barriers and response validation by counting
// requests and inspecting the bodies that were actually sent.

import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'
import { effectScope, shallowRef } from 'vue'
import { flushPromises } from '@vue/test-utils'
import { api } from '../../lib/api'
import type { ChatMessage } from '../../lib/types'
import * as safeMarkdown from '../../lib/safeMarkdown'
import {
  MENTION_BATCH_BYTES,
  MENTION_BATCH_ITEMS,
  MENTION_MAX_ATTEMPTS,
  MENTION_MAX_CANDIDATES,
  MENTION_MAX_CHARS,
  MENTION_MAX_REVISIONS,
  MENTION_MESSAGE_CHARS,
  useFileMentions,
  type FileMentionContext,
} from '../useFileMentions'

vi.mock('../../lib/safeMarkdown', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../lib/safeMarkdown')>()
  return { ...actual, collectAssistantFileMentions: vi.fn(actual.collectAssistantFileMentions) }
})

const RESOLVE_PATH = '/api/chats/chat-1/file-paths/resolve'
const LONE_SURROGATE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/

function asst(content: string, extra: Partial<ChatMessage> = {}): ChatMessage {
  return { role: 'assistant', content, timestamp: '2026-11-13T09:00:00Z', ...extra }
}

function ctx(over: Partial<FileMentionContext> = {}): FileMentionContext {
  return {
    chatId: 'chat-1',
    projectId: 'project-1',
    workspace: 'personal',
    repoPath: '',
    repoLocation: { path: '', exists: false, isWorktree: false, branch: '' },
    ...over,
  }
}

function harness(initial: { rows?: ChatMessage[]; context?: FileMentionContext | null; streaming?: boolean } = {}) {
  const rows = shallowRef<ChatMessage[]>(initial.rows ?? [])
  const context = shallowRef<FileMentionContext | null>(initial.context === undefined ? ctx() : initial.context)
  const streaming = shallowRef(initial.streaming ?? false)
  const scope = effectScope()
  const mentions = scope.run(() => useFileMentions(
    () => context.value,
    () => rows.value,
    () => streaming.value,
  ))!
  return { rows, context, streaming, scope, ...mentions }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

/** A well-formed response: each input answered by `map[input]`, or null. */
function resultsFor(paths: string[], map: Record<string, string | null> = {}) {
  return { results: paths.map(input => ({ input, path: map[input] ?? null })) }
}

function bodyPaths(call: unknown[]): string[] {
  return (call[1] as { paths: string[] }).paths
}

let post: MockInstance<typeof api.post>

beforeEach(() => {
  post = vi.spyOn(api, 'post').mockImplementation((async () => resultsFor([])) as never)
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.mocked(safeMarkdown.collectAssistantFileMentions).mockClear()
})

describe('useFileMentions', () => {
  it('batches untouched completed replies once per generation', async () => {
    const answers = { 'docs/guide.md:17': '/r/docs/guide.md', 'README.md': '/r/README.md' }
    post.mockImplementation((async (_path: string, body: { paths: string[] }) => resultsFor(body.paths, answers)) as never)
    const h = harness({
      rows: [
        asst('See docs/guide.md:17 and notes/a.md.'),
        asst('Also docs/guide.md:17 and README.md.'),
      ],
    })
    await flushPromises()

    expect(post).toHaveBeenCalledTimes(1)
    expect(post.mock.calls[0][0]).toBe(RESOLVE_PATH)
    // Newest reply first; the duplicate spelling is sent once; `:17` is kept.
    expect(post.mock.calls[0][1]).toEqual({ paths: ['docs/guide.md:17', 'README.md', 'notes/a.md'] })
    expect(h.targets.value.get('docs/guide.md:17')).toBe('/r/docs/guide.md')

    h.rows.value = [...h.rows.value]
    await flushPromises()
    expect(post).toHaveBeenCalledTimes(1)
  })

  it('pauses extraction and dispatch throughout streaming', async () => {
    const answers = { 'docs/one.md': '/r/docs/one.md', 'docs/two.md': '/r/docs/two.md' }
    post.mockImplementation((async (_path: string, body: { paths: string[] }) => resultsFor(body.paths, answers)) as never)
    const pending = deferred<unknown>()
    post.mockImplementationOnce(() => pending.promise as never)

    const h = harness({ rows: [asst('a docs/one.md')] })
    await flushPromises()
    expect(post).toHaveBeenCalledTimes(1)

    h.streaming.value = true
    await flushPromises()
    h.rows.value = [...h.rows.value, asst('new docs/two.md')]
    await flushPromises()
    expect(post).toHaveBeenCalledTimes(1)

    // The request sent before streaming finishes and publishes.
    pending.resolve(resultsFor(['docs/one.md'], answers))
    await flushPromises()
    expect(h.targets.value.get('docs/one.md')).toBe('/r/docs/one.md')
    expect(post).toHaveBeenCalledTimes(1)

    // The true -> false edge is one reset and one flush of the next batch.
    h.streaming.value = false
    await flushPromises()
    expect(post).toHaveBeenCalledTimes(2)
    expect(bodyPaths(post.mock.calls[1])).toEqual(['docs/two.md', 'docs/one.md'])
    expect(h.targets.value.get('docs/two.md')).toBe('/r/docs/two.md')
    await flushPromises()
    expect(post).toHaveBeenCalledTimes(2)
  })

  it('caches unchanged extraction without revisiting old history', async () => {
    const h = harness({
      rows: [asst('docs/a.md'), asst('docs/b.md'), asst('docs/c.md')],
    })
    await flushPromises()
    const parsed = vi.mocked(safeMarkdown.collectAssistantFileMentions)
    const afterFirst = parsed.mock.calls.length
    expect(afterFirst).toBe(3)

    // Same objects, same content: no reparse.
    h.rows.value = [...h.rows.value]
    await flushPromises()
    expect(parsed.mock.calls.length).toBe(afterFirst)

    // One new row: only that row is parsed.
    h.rows.value = [...h.rows.value, asst('docs/d.md')]
    await flushPromises()
    expect(parsed.mock.calls.length).toBe(afterFirst + 1)

    // A changed object counts as a new revision and is parsed again.
    const changed = h.rows.value[0]
    changed.content = 'docs/a.md and docs/z.md'
    h.rows.value = [...h.rows.value]
    await flushPromises()
    expect(parsed.mock.calls.length).toBe(afterFirst + 2)
  })

  it('caps revisions characters candidates bytes and attempts', async () => {
    const replyAll = (paths: string[]) => resultsFor(paths)

    // One message over the per-message cap is skipped whole.
    post.mockImplementation((async (_p: string, body: { paths: string[] }) => replyAll(body.paths)) as never)
    let h = harness({ rows: [asst(`docs/huge.md ${'x'.repeat(MENTION_MESSAGE_CHARS + 1)}`), asst('docs/small.md')] })
    await flushPromises()
    expect(post.mock.calls.flatMap(c => bodyPaths(c))).toEqual(['docs/small.md'])
    h.scope.stop()

    // 101 messages: only the newest 100 are examined; the oldest is never sent.
    post.mockClear()
    h = harness({ rows: Array.from({ length: MENTION_MAX_REVISIONS + 1 }, (_, i) => asst(`docs/m${i}.md`)) })
    await flushPromises()
    const sent101 = post.mock.calls.flatMap(c => bodyPaths(c))
    expect(sent101).toHaveLength(MENTION_MAX_REVISIONS)
    expect(sent101).not.toContain('docs/m0.md')
    expect(sent101).toContain(`docs/m${MENTION_MAX_REVISIONS}.md`)
    h.scope.stop()

    // 9 messages of 30,000 chars: 270,000 > 262,144, so the oldest is skipped.
    post.mockClear()
    const big = (i: number) => `docs/c${i}.md ${'y'.repeat(30000 - `docs/c${i}.md `.length)}`
    h = harness({ rows: Array.from({ length: 9 }, (_, i) => asst(big(i))) })
    await flushPromises()
    const sentChars = post.mock.calls.flatMap(c => bodyPaths(c))
    expect(sentChars).toHaveLength(8)
    expect(sentChars).not.toContain('docs/c0.md')
    expect(sentChars).toContain('docs/c8.md')
    expect(MENTION_MAX_CHARS).toBe(262144)
    h.scope.stop()

    // 21 messages of 10 mentions each: at most 200 candidates reach the wire.
    post.mockClear()
    h = harness({
      rows: Array.from({ length: 21 }, (_, m) => asst(
        Array.from({ length: 10 }, (_, j) => `docs/p${m}-${j}.md`).join(' '),
      )),
    })
    await flushPromises()
    const sentCandidates = post.mock.calls.flatMap(c => bodyPaths(c))
    expect(new Set(sentCandidates).size).toBe(MENTION_MAX_CANDIDATES)
    expect(MENTION_MAX_CANDIDATES).toBe(200)
    h.scope.stop()

    // 51 short inputs split into 50 + 1.
    post.mockClear()
    h = harness({
      rows: [asst(Array.from({ length: 51 }, (_, i) => `docs/s${i}.md`).join(' '))],
    })
    await flushPromises()
    expect(post.mock.calls.map(c => bodyPaths(c).length)).toEqual([MENTION_BATCH_ITEMS, 1])
    h.scope.stop()

    // 40 inputs of 998 multibyte chars: every body stays within the byte cap.
    post.mockClear()
    const wide = (i: number) => `${'é'.repeat(990)}${String(i).padStart(5, '0')}.md`
    h = harness({ rows: Array.from({ length: 40 }, (_, i) => asst(wide(i))) })
    await flushPromises()
    expect(wide(0)).toHaveLength(998)
    expect(post.mock.calls.length).toBeGreaterThan(1)
    for (const call of post.mock.calls) {
      const bytes = new TextEncoder().encode(JSON.stringify(call[1])).length
      expect(bytes).toBeLessThanOrEqual(MENTION_BATCH_BYTES)
    }
    expect(new Set(post.mock.calls.flatMap(c => bodyPaths(c))).size).toBe(40)
    h.scope.stop()

    // 21 single-mention replies, each settled before the next, every POST rejecting:
    // exactly 20 requests, and the 21st reply never sends one.
    post.mockClear()
    post.mockImplementation((async () => { throw new Error('offline') }) as never)
    h = harness({ rows: [] })
    await flushPromises()
    for (let i = 0; i < MENTION_MAX_ATTEMPTS + 1; i++) {
      h.rows.value = [...h.rows.value, asst(`docs/r${i}.md`)]
      await flushPromises()
    }
    expect(post).toHaveBeenCalledTimes(MENTION_MAX_ATTEMPTS)
    expect(post.mock.calls.map(c => bodyPaths(c)[0]).at(-1)).toBe(`docs/r${MENTION_MAX_ATTEMPTS - 1}.md`)
    h.scope.stop()
  })

  it('never sends an input over the length cap, a control character or a lone surrogate', async () => {
    post.mockImplementation((async (_p: string, body: { paths: string[] }) => resultsFor(body.paths)) as never)
    const long = `docs/${'a'.repeat(4100)}.md`
    const control = 'docs/ctl\u0007.md'
    const lone = '\uD800x.md'
    const h = harness({ rows: [asst(`${long} ${control}`), asst(lone), asst('docs/ok.md')] })
    await flushPromises()

    const sent = post.mock.calls.flatMap(c => bodyPaths(c))
    expect(sent).toEqual(['docs/ok.md'])
    for (const path of sent) {
      expect(path.length).toBeLessThanOrEqual(4096)
      expect(/[\x00-\x1f\x7f]/.test(path)).toBe(false)
      expect(LONE_SURROGATE.test(path)).toBe(false)
    }
    h.scope.stop()
  })

  it('serializes generations without stale finally corruption', async () => {
    const pending = deferred<unknown>()
    post.mockImplementationOnce(() => pending.promise as never)
    const h = harness({ rows: [asst('docs/a.md')] })
    await flushPromises()
    expect(post).toHaveBeenCalledTimes(1)

    h.context.value = ctx({ repoPath: '/repo' })
    await flushPromises()
    h.streaming.value = true
    await flushPromises()
    h.streaming.value = false
    await flushPromises()
    h.invalidate()
    await flushPromises()
    // The old request still holds the single slot.
    expect(post).toHaveBeenCalledTimes(1)

    // Its late answer belongs to the old context and writes nothing.
    post.mockImplementation((async (_p: string, body: { paths: string[] }) =>
      resultsFor(body.paths, { 'docs/a.md': '/new/docs/a.md' })) as never)
    pending.resolve(resultsFor(['docs/a.md'], { 'docs/a.md': '/old/docs/a.md' }))
    await flushPromises()
    expect(post).toHaveBeenCalledTimes(2)
    expect(h.targets.value.get('docs/a.md')).toBe('/new/docs/a.md')

    // After disposal, a late answer writes nothing and nothing new is sent.
    const after = h.targets.value
    const late = deferred<unknown>()
    h.rows.value = [...h.rows.value, asst('docs/late.md')]
    post.mockImplementationOnce(() => late.promise as never)
    await flushPromises()
    h.scope.stop()
    late.resolve(resultsFor(['docs/late.md'], { 'docs/late.md': '/new/docs/late.md' }))
    await flushPromises()
    expect(h.targets.value).toBe(after)
    expect(post).toHaveBeenCalledTimes(3)
  })

  it('validates response shape and never retries failed batches in a loop', async () => {
    const bad: Array<[string, (paths: string[]) => unknown]> = [
      ['wrong count', () => ({ results: [] })],
      ['wrong order', paths => ({ results: [...paths].reverse().map(input => ({ input, path: null })) })],
      ['mismatched input', paths => ({ results: paths.map(() => ({ input: 'other.md', path: null })) })],
      ['extra key', paths => ({ results: paths.map(input => ({ input, path: null })), extra: 1 })],
      ['relative path', paths => ({ results: paths.map(input => ({ input, path: 'r/docs/a.md' })) })],
      ['path with a line', paths => ({ results: paths.map(input => ({ input, path: '/r/docs/a.md:12' })) })],
      ['control character', paths => ({ results: paths.map(input => ({ input, path: '/r/docs/a\u0007.md' })) })],
      ['UNC path', paths => ({ results: paths.map(input => ({ input, path: '//server/share/a.md' })) })],
      ['path over 4096 chars', paths => ({ results: paths.map(input => ({ input, path: `/${'a'.repeat(4096)}` })) })],
      ['lone surrogate path', paths => ({ results: paths.map(input => ({ input, path: '/r/\uD800.md' })) })],
    ]
    for (const [label, build] of bad) {
      post.mockClear()
      post.mockImplementation((async (_p: string, body: { paths: string[] }) => build(body.paths)) as never)
      const h = harness({ rows: [asst('docs/a.md docs/b.md')] })
      const before = h.targets.value
      await flushPromises()
      await flushPromises()
      expect(post, label).toHaveBeenCalledTimes(1)
      expect(h.targets.value, label).toBe(before)

      // A new generation may retry the same mentions.
      post.mockImplementation((async (_p: string, body: { paths: string[] }) =>
        resultsFor(body.paths, { 'docs/a.md': '/r/docs/a.md' })) as never)
      h.invalidate()
      await flushPromises()
      expect(post, label).toHaveBeenCalledTimes(2)
      expect(h.targets.value.get('docs/a.md'), label).toBe('/r/docs/a.md')
      h.scope.stop()
    }

    // Transport and non-200 failures: plain text, no toast, no retry in the same generation.
    for (const failure of [new TypeError('fetch failed'), Object.assign(new Error('HTTP 500'), { status: 500 })]) {
      post.mockClear()
      post.mockImplementation((async () => { throw failure }) as never)
      const h = harness({ rows: [asst('docs/a.md')] })
      await flushPromises()
      await flushPromises()
      expect(post).toHaveBeenCalledTimes(1)
      expect(h.targets.value.size).toBe(0)
      h.scope.stop()
    }

    // A canonical Windows drive path is a valid answer.
    post.mockClear()
    post.mockImplementation((async (_p: string, body: { paths: string[] }) =>
      resultsFor(body.paths, { 'docs/a.md': 'C:/x/y.md' })) as never)
    const h = harness({ rows: [asst('docs/a.md')] })
    await flushPromises()
    expect(h.targets.value.get('docs/a.md')).toBe('C:/x/y.md')
    h.scope.stop()
  })

  it('publishes immutable maps only on effective change', async () => {
    post.mockImplementation((async (_p: string, body: { paths: string[] }) => resultsFor(body.paths)) as never)
    const h = harness({ rows: [asst('docs/n.md')] })
    await flushPromises()
    const empty = h.targets.value
    expect(empty.size).toBe(0)

    // A null-only result keeps the map's identity.
    h.rows.value = [...h.rows.value, asst('docs/m.md')]
    await flushPromises()
    expect(h.targets.value).toBe(empty)

    // A positive result replaces the map with a new one.
    post.mockImplementation((async (_p: string, body: { paths: string[] }) =>
      resultsFor(body.paths, { 'docs/m.md': '/r/docs/m.md' })) as never)
    h.rows.value = [...h.rows.value, asst('docs/m.md')]
    await flushPromises()
    h.invalidate()
    await flushPromises()
    expect(h.targets.value).not.toBe(empty)
    expect(h.targets.value.get('docs/m.md')).toBe('/r/docs/m.md')

    // A new context clears positives immediately; the new chat's lookup answers null here.
    post.mockImplementation((async (_p: string, body: { paths: string[] }) => resultsFor(body.paths)) as never)
    h.context.value = ctx({ chatId: 'chat-2' })
    await flushPromises()
    expect(h.targets.value.size).toBe(0)
    expect(post.mock.calls.at(-1)?.[0]).toBe('/api/chats/chat-2/file-paths/resolve')
    h.scope.stop()

    // An absolute spelling confirmed as itself creates no basename key.
    post.mockImplementation((async (_p: string, body: { paths: string[] }) =>
      resultsFor(body.paths, { '/x/docs/README.md': '/x/docs/README.md' })) as never)
    const readme = harness({ rows: [asst('see /x/docs/README.md')] })
    await flushPromises()
    expect(readme.targets.value.get('/x/docs/README.md')).toBe('/x/docs/README.md')
    expect(readme.targets.value.has('README.md')).toBe(false)
    readme.scope.stop()
  })

  it('does not collect user tool reasoning or explicit links', async () => {
    const h = harness({
      rows: [
        { role: 'user', content: 'docs/user.md', timestamp: '2026-11-13T09:00:00Z' },
        asst('docs/tool.md', { tool_name: 'Read' }),
        asst('docs/activity.md', { tool_name: '_activity' }),
        asst('docs/card.md', { tool_name: '_filecard' }),
        asst('docs/failed.md', { is_error: true }),
        asst('[see this](docs/link.md)'),
      ],
    })
    await flushPromises()
    expect(post).not.toHaveBeenCalled()
    h.scope.stop()
  })

  it('keeps a confirmed link through a turn-end refetch and drops it on an all-null answer', async () => {
    post.mockImplementation((async (_p: string, body: { paths: string[] }) =>
      resultsFor(body.paths, { 'docs/one.md': '/r/docs/one.md' })) as never)
    const h = harness({ rows: [asst('docs/one.md')] })
    await flushPromises()
    expect(h.targets.value.get('docs/one.md')).toBe('/r/docs/one.md')

    const refetch = deferred<unknown>()
    post.mockImplementationOnce(() => refetch.promise as never)
    h.streaming.value = true
    await flushPromises()
    h.streaming.value = false
    await flushPromises()
    expect(post).toHaveBeenCalledTimes(2)
    // The refetch is pending: the confirmed entry stays on screen.
    expect(h.targets.value.get('docs/one.md')).toBe('/r/docs/one.md')

    refetch.resolve(resultsFor(['docs/one.md'], {}))
    await flushPromises()
    expect(h.targets.value.has('docs/one.md')).toBe(false)
    expect(h.targets.value.size).toBe(0)
    h.scope.stop()
  })

  it('keeps the previous map when a refetch is rejected or malformed, with no extra request', async () => {
    const failures: Array<[string, () => unknown]> = [
      ['rejected', () => Promise.reject(new Error('offline'))],
      ['malformed', () => Promise.resolve({ results: [] })],
    ]
    for (const [label, answer] of failures) {
      post.mockClear()
      post.mockImplementation((async (_p: string, body: { paths: string[] }) =>
        resultsFor(body.paths, { 'docs/one.md': '/r/docs/one.md' })) as never)
      const h = harness({ rows: [asst('docs/one.md')] })
      await flushPromises()
      const confirmed = h.targets.value
      expect(confirmed.get('docs/one.md'), label).toBe('/r/docs/one.md')

      post.mockImplementationOnce(answer as never)
      h.streaming.value = true
      await flushPromises()
      h.streaming.value = false
      await flushPromises()
      await flushPromises()
      expect(post, label).toHaveBeenCalledTimes(2)
      expect(h.targets.value, label).toBe(confirmed)
      h.scope.stop()
    }
  })

  it('merges a still-present entry with a new positive from the refetch', async () => {
    post.mockImplementation((async (_p: string, body: { paths: string[] }) =>
      resultsFor(body.paths, { 'docs/one.md': '/r/docs/one.md' })) as never)
    const h = harness({ rows: [asst('docs/one.md')] })
    await flushPromises()

    const refetch = deferred<unknown>()
    post.mockImplementationOnce(() => refetch.promise as never)
    h.streaming.value = true
    await flushPromises()
    h.rows.value = [...h.rows.value, asst('new docs/two.md')]
    await flushPromises()
    h.streaming.value = false
    await flushPromises()
    // Pending: the old entry stays, and the new mention is not yet confirmed.
    expect(h.targets.value.get('docs/one.md')).toBe('/r/docs/one.md')
    expect(h.targets.value.has('docs/two.md')).toBe(false)

    refetch.resolve(resultsFor(['docs/two.md', 'docs/one.md'], {
      'docs/two.md': '/r/docs/two.md',
      'docs/one.md': '/r/moved/one.md',
    }))
    await flushPromises()
    expect(Object.fromEntries(h.targets.value)).toEqual({
      'docs/two.md': '/r/docs/two.md',
      'docs/one.md': '/r/moved/one.md',
    })
    h.scope.stop()
  })
})
