import { computed, onScopeDispose, shallowRef, watch, type ComputedRef } from 'vue'
import { api } from '../lib/api'
import type { ChatMessage } from '../lib/types'
import type { FileMentionTargets } from '../lib/filePaths'
import { collectAssistantFileMentions } from '../lib/safeMarkdown'

// Discovery of untouched file mentions in completed assistant replies. The
// engine confirms each spelling (POST .../file-paths/resolve) and only the
// confirmed ones become links. Results live for one generation; a generation
// ends on a context change, a finished turn, `invalidate()` or disposal.

export const MENTION_MAX_REVISIONS = 100
export const MENTION_MAX_CHARS = 262144
export const MENTION_MAX_CANDIDATES = 200
export const MENTION_MAX_ATTEMPTS = 20
export const MENTION_BATCH_ITEMS = 50
export const MENTION_BATCH_BYTES = 32768
export const MENTION_INPUT_CHARS = 4096
export const MENTION_MESSAGE_CHARS = 32768

/** What the resolve route is asked about: the chat and the layout its roots come from. */
export type FileMentionContext = {
  chatId: string
  projectId: string
  workspace: string
  repoPath: string
  repoLocation: { path: string; exists: boolean; isWorktree: boolean; branch: string }
}

const CONTROL = /[\x00-\x1f\x7f]/
const LONE_SURROGATE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/
const ABSOLUTE = /^(?:\/(?!\/)|[A-Za-z]:\/)/
const LINE_SUFFIX = /:\d+$/
const encoder = new TextEncoder()

function sendable(input: string): boolean {
  return input.length <= MENTION_INPUT_CHARS && !CONTROL.test(input) && !LONE_SURROGATE.test(input)
}

function validPath(path: string): boolean {
  return path.length <= MENTION_INPUT_CHARS && !CONTROL.test(path) && !LONE_SURROGATE.test(path)
    && ABSOLUTE.test(path) && !LINE_SUFFIX.test(path)
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** The engine's answer for `submitted`, or null when the shape is not exactly the contract. */
function parseResults(body: unknown, submitted: string[]): Array<string | null> | null {
  if (!isPlainObject(body) || Object.keys(body).join() !== 'results') return null
  const rows = body.results
  if (!Array.isArray(rows) || rows.length !== submitted.length) return null
  const out: Array<string | null> = []
  for (let i = 0; i < rows.length; i++) {
    const row = rows[i]
    if (!isPlainObject(row) || Object.keys(row).sort().join() !== 'input,path') return null
    if (row.input !== submitted[i]) return null
    if (row.path === null) {
      out.push(null)
      continue
    }
    if (typeof row.path !== 'string' || !validPath(row.path)) return null
    out.push(row.path)
  }
  return out
}

function sameEntries(a: FileMentionTargets, b: FileMentionTargets): boolean {
  if (a.size !== b.size) return false
  for (const [input, path] of b) if (a.get(input) !== path) return false
  return true
}

function ctxKeyOf(c: FileMentionContext | null): string | null {
  if (!c) return null
  const l = c.repoLocation
  return JSON.stringify([c.chatId, c.projectId, c.workspace, c.repoPath, l.path, l.exists, l.isWorktree, l.branch])
}

/**
 * Confirmed targets for the untouched mentions in the completed assistant
 * replies of one chat. `messages` is the final reply bubbles; `streaming`
 * pauses all extraction and dispatch.
 */
export function useFileMentions(
  context: () => FileMentionContext | null,
  messages: () => readonly ChatMessage[],
  streaming: () => boolean,
): { targets: ComputedRef<FileMentionTargets>; invalidate: () => void } {
  const state = shallowRef<FileMentionTargets>(new Map())
  const targets = computed(() => state.value)

  let stopped = false
  let generation = 0
  let contextKey: string | null | undefined
  let ctxNow: FileMentionContext | null = null
  let wasStreaming = false
  // The published map before this generation's reset; entries survive a turn
  // reset until the new answers replace them, so links do not flicker.
  let kept: FileMentionTargets = new Map()
  let answers = new Map<string, string | null>()
  let candidates = new Set<string>()
  let attempted = new Set<string>()
  let examined = new WeakMap<ChatMessage, string>()
  let revisions = 0
  let chars = 0
  let posts = 0
  let busy = false
  let scheduled = false
  // Extraction cache: a parsed message is not reparsed while its content is unchanged.
  const parsed = new WeakMap<ChatMessage, { content: string; mentions: string[] }>()

  function publish(next: FileMentionTargets): void {
    if (sameEntries(state.value, next)) return
    state.value = next
  }

  function compose(): FileMentionTargets {
    const next = new Map<string, string>()
    for (const [input, path] of kept) {
      if (!answers.has(input) && candidates.has(input)) next.set(input, path)
    }
    for (const [input, path] of answers) if (path !== null) next.set(input, path)
    return next
  }

  function reset(keep: boolean): void {
    generation++
    kept = keep ? state.value : new Map()
    answers = new Map()
    candidates = new Set()
    attempted = new Set()
    examined = new WeakMap()
    revisions = 0
    chars = 0
    posts = 0
    if (!keep) publish(new Map())
  }

  function mentionsOf(message: ChatMessage): string[] {
    const hit = parsed.get(message)
    if (hit && hit.content === message.content) return hit.mentions
    const mentions = collectAssistantFileMentions(message.content)
    parsed.set(message, { content: message.content, mentions })
    return mentions
  }

  // Newest first, so the caps cut the oldest history.
  function extract(list: readonly ChatMessage[]): void {
    for (let i = list.length - 1; i >= 0; i--) {
      const m = list[i]
      if (m.role !== 'assistant' || m.tool_name || m.is_error) continue
      if (examined.get(m) === m.content) continue
      if (revisions >= MENTION_MAX_REVISIONS) break
      examined.set(m, m.content)
      revisions++
      if (m.content.length > MENTION_MESSAGE_CHARS || chars + m.content.length > MENTION_MAX_CHARS) continue
      chars += m.content.length
      for (const input of mentionsOf(m)) {
        if (candidates.size >= MENTION_MAX_CANDIDATES) break
        if (sendable(input)) candidates.add(input)
      }
    }
    publish(compose())
  }

  function nextBatch(): string[] {
    const batch: string[] = []
    for (const input of candidates) {
      if (attempted.has(input)) continue
      if (batch.length >= MENTION_BATCH_ITEMS) break
      if (encoder.encode(JSON.stringify({ paths: [...batch, input] })).length > MENTION_BATCH_BYTES) break
      batch.push(input)
    }
    return batch
  }

  function current(gen: number, key: string | null | undefined): boolean {
    return !stopped && gen === generation && key === contextKey
  }

  function schedule(): void {
    if (scheduled) return
    scheduled = true
    queueMicrotask(() => {
      scheduled = false
      pump()
    })
  }

  function pump(): void {
    if (stopped || busy || !ctxNow || streaming() || posts >= MENTION_MAX_ATTEMPTS) return
    const batch = nextBatch()
    if (batch.length === 0) return
    for (const input of batch) attempted.add(input)
    posts++
    busy = true
    const gen = generation
    const key = contextKey
    api.post<unknown>('/api/chats/' + encodeURIComponent(ctxNow.chatId) + '/file-paths/resolve', { paths: batch })
      .then((body) => {
        if (!current(gen, key)) return
        const rows = parseResults(body, batch)
        if (!rows) return
        batch.forEach((input, i) => answers.set(input, rows[i]))
        publish(compose())
      }, () => {
        // A failed batch stays plain for this generation: no toast, no retry.
      })
      .finally(() => {
        busy = false
        if (!stopped) schedule()
      })
  }

  watch(
    () => [ctxKeyOf(context()), messages(), streaming()] as const,
    ([key, list, isStreaming]) => {
      const contextChanged = key !== contextKey
      const turnEnded = wasStreaming && !isStreaming
      wasStreaming = isStreaming
      if (contextChanged) {
        contextKey = key
        ctxNow = context()
        reset(false)
      } else if (turnEnded) {
        reset(true)
      }
      if (!ctxNow || isStreaming) return
      extract(list)
      schedule()
    },
    { flush: 'post', immediate: true },
  )

  function invalidate(): void {
    if (stopped) return
    reset(true)
    if (!ctxNow || streaming()) return
    extract(messages())
    schedule()
  }

  onScopeDispose(() => {
    stopped = true
    generation++
    kept = new Map()
    answers = new Map()
    candidates = new Set()
    attempted = new Set()
  })

  return { targets, invalidate }
}
