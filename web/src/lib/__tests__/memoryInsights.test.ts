import { describe, expect, it } from 'vitest'
import {
  activeInsightSummary,
  memoryInsights,
  unfinishedInsightSummary,
  type MemoryInsightInput,
} from '../memoryInsights'
import { MEMORY_PASS_KIND } from '../memoryPass'
import type { ChatInfo } from '../types'

function chat(over: Partial<ChatInfo> = {}): ChatInfo {
  return {
    chat_id: 'c1',
    project_id: 'p1',
    title: 'A conversation',
    archived: false,
    created_at: '2026-08-01T10:00:00Z',
    last_activity_at: '2026-08-01T10:00:00Z',
    ...over,
  } as unknown as ChatInfo
}

function source(over: Partial<ChatInfo> = {}): ChatInfo {
  return chat({
    chat_id: 'src',
    title: 'Deck figures',
    archived: true,
    archive_path: 'chats/src.md',
    ...over,
  })
}

function pass(state: string, over: Partial<ChatInfo> = {}): ChatInfo {
  return chat({
    chat_id: 'pass-1',
    project_id: 'p-mem',
    title: `Memory pass · ${state}`,
    helper: {
      kind: MEMORY_PASS_KIND,
      source_chat_id: 'src',
      archive_path: 'chats/src.md',
      doc_path: 'projects/general.md',
      source_title: 'Deck figures',
      source_project: '',
      state,
      archive_policy: 'when_clean',
    } as unknown as NonNullable<ChatInfo['helper']>,
    ...over,
  })
}

function run(chats: ChatInfo[], over: Partial<MemoryInsightInput> = {}) {
  return memoryInsights({
    chats,
    needsInput: () => false,
    pendingQuestion: () => null,
    isArchiving: () => false,
    ...over,
  })
}

describe('memoryInsights', () => {
  it('reports nothing for a settled conversation', () => {
    expect(run([
      source({ postprocess: { state: 'done', steps: { trajectory: { status: 'ok' } } } as never }),
      chat(),
    ])).toEqual([])
  })

  it('names the conversation, not the pass, and opens the pass', () => {
    // The pass's own title is an internal label ("Memory pass · …"); the row is
    // about the conversation, and its click target is the pass.
    const [row] = run([source(), pass('running')])
    expect(row.title).toBe('Deck figures')
    expect(row.sourceChatId).toBe('src')
    expect(row.passChatId).toBe('pass-1')
    expect(row.archivePath).toBe('chats/src.md')
    expect(row.phase).toBe('running')
    expect(row.label).toBe('updating memory…')
    expect(row.active).toBe(true)
    expect(row.blocking).toBe(false)
  })

  it('keeps one row for a conversation whose pipeline and pass both run', () => {
    // The trajectory is saved in seconds and the pass takes minutes, so this
    // overlap is the normal case, not an edge — and it is the case that used
    // to show the same conversation twice.
    const rows = run([
      source({ postprocess: { state: 'running', step: 'trajectory', steps: {} } as never }),
      pass('running'),
    ])
    expect(rows).toHaveLength(1)
    expect(rows[0].phase).toBe('running')
    expect(rows[0].retryable).toBe(false)
  })

  it('follows the pass through queued, blocked and unclean', () => {
    expect(run([source(), pass('queued')])[0]).toMatchObject({
      phase: 'queued',
      label: 'queued for memory',
      active: true,
    })
    expect(run([source(), pass('attention')])[0]).toMatchObject({
      phase: 'attention',
      label: 'needs attention',
      blocking: true,
      active: false,
    })
    const [blocked] = run([source(), pass('running')], {
      needsInput: id => id === 'pass-1',
      pendingQuestion: () => 'Which project is this?',
    })
    expect(blocked.phase).toBe('needsYou')
    expect(blocked.label).toBe('needs you')
    expect(blocked.question).toBe('Which project is this?')
    expect(blocked.blocking).toBe(true)
  })

  it('calls a finished pass updated, not queued, when its archive did not land', () => {
    // A cleanly finished pass is auto-archived, so `done` on an open chat means
    // the archive POST failed. Reporting "queued" would promise work that is
    // never coming.
    expect(run([source(), pass('done')])[0]).toMatchObject({
      phase: 'done',
      label: 'memory updated',
      active: false,
      blocking: false,
    })
  })

  it('names the stage it is saving, and says something for an unknown one', () => {
    expect(run([source({ postprocess: { state: 'running', step: 'trajectory', steps: {} } as never })])[0])
      .toMatchObject({ phase: 'tidying', label: 'saving trajectory…', passChatId: '' })
    expect(run([source({ postprocess: { state: 'running', step: 'memory_pass', steps: {} } as never })])[0])
      .toMatchObject({ phase: 'tidying', label: 'tidying up…' })
  })

  it('falls back to the transcript while there is no pass to open', () => {
    const [row] = run([source({ postprocess: { state: 'running', step: 'trajectory', steps: {} } as never })])
    expect(row.passChatId).toBe('')
    expect(row.archivePath).toBe('chats/src.md')
  })

  it('disables the row when there is nothing at all to open', () => {
    // An archive with no file and no pass is a dead entry; the caller reads the
    // empty pass id and the empty path and disables it.
    const [row] = run([source({ archive_path: '', postprocess: { state: 'running', step: 'trajectory', steps: {} } as never })])
    expect(row.passChatId).toBe('')
    expect(row.archivePath).toBe('')
  })

  it('marks an archive POST in flight as archiving', () => {
    const [row] = run([source({ archive_path: 'chats/src.md' })], { isArchiving: id => id === 'src' })
    expect(row.phase).toBe('archiving')
    expect(row.label).toBe('archiving…')
  })

  it('offers the retry on a pipeline that stopped with a stage left to run', () => {
    const [unfinished] = run([source({
      postprocess: {
        state: 'incomplete',
        job: { job_id: 'j', state: 'incomplete', unfinished: ['trajectory'] },
      } as never,
    })])
    expect(unfinished).toMatchObject({
      phase: 'unfinished',
      label: 'trajectory not finished',
      retryable: true,
    })
    // Neither in flight nor blocked: processing has stopped, and the row is the
    // only thing on Home that offers the fix.
    expect(unfinished.active).toBe(false)
    expect(unfinished.blocking).toBe(false)
  })

  it('names a legacy failure from the step statuses it has', () => {
    expect(run([source({ postprocess: { state: 'incomplete', steps: { trajectory: { status: 'error' } } } as never })])[0].label)
      .toBe('trajectory failed')
  })

  it('never gives a pass a row of its own for its own auto-archive', () => {
    // A cleanly finished pass is archived, which starts a trajectory save of
    // its own. A row keyed on the pass would be a second entry for the same
    // memory work, so only the conversation it was spawned for is listed.
    const rows = run([
      source({ postprocess: { state: 'running', step: 'memory_pass', steps: {} } as never }),
      pass('done', {
        archived: true,
        postprocess: { state: 'running', step: 'trajectory', steps: {} } as never,
      }),
    ])
    expect(rows).toHaveLength(1)
    expect(rows[0].sourceChatId).toBe('src')
  })

  it('keeps a pass whose source conversation is gone, using the title it carries', () => {
    const [row] = run([pass('running')])
    expect(row.sourceChatId).toBe('src')
    expect(row.title).toBe('Deck figures')
    expect(row.passChatId).toBe('pass-1')
    // No source means no transcript, so the pass is the only target.
    expect(row.archivePath).toBe('')
  })

  it('orders newest first, by the conversation rather than by the pass', () => {
    // A pass that finishes now did not change when the conversation happened;
    // dating the row by the pass made a workspace's rows jump around.
    const saving = { state: 'running', step: 'trajectory', steps: {} } as never
    const rows = run([
      source({ postprocess: saving }),
      source({ chat_id: 'older', last_activity_at: '2026-08-01T09:00:00Z', postprocess: saving }),
      source({ chat_id: 'newer', last_activity_at: '2026-08-01T11:00:00Z', postprocess: saving }),
      pass('running', { last_activity_at: '2026-08-02T12:00:00Z' }),
    ])
    expect(rows.map(row => row.sourceChatId)).toEqual(['newer', 'src', 'older'])
  })

  it('breaks a tie towards the live pass, not towards creation order', () => {
    // The backend truncates activity timestamps to whole seconds, so two
    // conversations archived in the same second tie — and a stable sort then
    // falls back to whichever loop created the row first. Live work belongs
    // above a stalled pipeline, and the order must not depend on that accident.
    const rows = run([
      source({ chat_id: 'stalled', postprocess: { state: 'incomplete', job: { job_id: 'j', state: 'incomplete', unfinished: ['trajectory'] } } as never }),
      source({ chat_id: 'src', postprocess: { state: 'running', step: 'memory_pass', steps: {} } as never }),
      pass('running', { last_activity_at: '2026-08-01T12:00:00Z' }),
    ])
    expect(rows.map(row => row.sourceChatId)).toEqual(['src', 'stalled'])
    // The join is in the other direction too: a pipeline row that already has a
    // pass keeps the pass's phase and only gains the retry.
    expect(rows[1]).toMatchObject({ phase: 'unfinished', retryable: true, passChatId: '' })
  })
})

describe('memory insight lane fragments', () => {
  it('says nothing when there is nothing to say', () => {
    expect(activeInsightSummary(0)).toBe('')
    expect(unfinishedInsightSummary(0)).toBe('')
  })

  it('phrases the counts for a lane status sentence', () => {
    expect(activeInsightSummary(1)).toBe('1 updating memory')
    expect(activeInsightSummary(2)).toBe('2 updating memory')
    expect(unfinishedInsightSummary(1)).toBe('1 chat with unfinished steps')
    expect(unfinishedInsightSummary(2)).toBe('2 chats with unfinished steps')
  })
})
