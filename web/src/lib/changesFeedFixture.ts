/**
 * A synthetic Changes feed, shaped like `GET /api/memory/changes`. Shared by the
 * unit and component tests, the Storybook stories and the e2e spec. Synthetic
 * values only: no real paths, chats or vault text.
 */
import type { ChangeEntry, ChangesFeed } from './changesFeed'
import { CARE_WORKSPACE, CARE_WRITTEN_AT } from './careReportFixture'

export const CHANGES_WORKSPACE = CARE_WORKSPACE
const viewer = (path: string) => `memory-vault/${CHANGES_WORKSPACE}/${path}`

function undoEntry(id: string, date: string, over: Partial<ChangeEntry>): ChangeEntry {
  return {
    id, date, kind: 'note-edit', area: 'notes', undo: true, open_path: '', target: '', path: '', region: '', quote: '', why: '',
    by: { kind: 'care', id: 'second-look', text: '' },
    ...over,
  }
}

export const changesFeed: ChangesFeed = {
  workspace: CHANGES_WORKSPACE,
  undo_log: 'ok',
  undo_log_path: viewer('Workspace/Memory-Consolidations.md'),
  groups: [
    {
      id: 'care-2026-11-27', kind: 'care', date: '2026-11-27', at: CARE_WRITTEN_AT, steps: 3,
      entries: [
        undoEntry('u-1', '2026-11-27', {
          kind: 'memory-remove', area: 'memory', target: 'memory', region: 'memory', quote: 'Standup moved to 09:30 for the autumn',
          why: 'expired', by: { kind: 'care', id: 'regions', text: '' },
        }),
        undoEntry('u-2', '2026-11-27', {
          kind: 'merge', target: 'People/Sample-Person.md', path: 'People/Sample-Person.md', open_path: viewer('People/Sample-Person.md'),
          quote: 'Prefers Friday check-ins', why: 'merged into People/Sample Person.md',
        }),
        { id: 's-1', kind: 'care-step', area: 'notes', undo: false, open_path: '', step: 'hygiene', text: 'fixed 10 of 10 findings: orphan links and missing headers' },
        { id: 's-2', kind: 'task-closed', area: 'tasks', undo: false, open_path: '', step: 'closed-tasks', text: 'read 2 tasks, wrote 1 fact (People/User.md)' },
        {
          id: 'p-1', kind: 'skill-suggestion', area: 'skills', undo: false, date: '2026-11-27', target: 'weekly-summary',
          title: 'Shorten the weekly summary', status: 'ready', path: 'Workspace/Proposals/shorten-weekly-summary.md',
          open_path: viewer('Workspace/Proposals/shorten-weekly-summary.md'),
        },
      ],
    },
    {
      id: 'day-2026-11-26', kind: 'day', date: '2026-11-26', at: '',
      entries: [
        undoEntry('u-3', '2026-11-26', {
          target: 'Projects/Sample-Project.md', path: 'Projects/Sample-Project.md', open_path: viewer('Projects/Sample-Project.md'),
          quote: 'Launch planned for May', why: 'superseded: launch moved to March', by: { kind: 'memory_pass', id: 'pass-1', text: '' },
        }),
        {
          id: 'c-1', kind: 'chat-processed', area: 'memory', undo: false, open_path: '', date: '2026-11-26', at: '2026-11-26T09:12:00Z',
          pass_chat_id: 'pass-1', chat_id: 'chat-sample-1', title: 'Sample booking cancellation',
        },
        {
          id: 'c-2', kind: 'chat-processed', area: 'memory', undo: false, open_path: '', date: '2026-11-26', at: '2026-11-26T08:40:00Z',
          pass_chat_id: 'pass-2', chat_id: 'chat-sample-2', title: 'Sample travel plan',
        },
      ],
    },
  ],
  week: { since: '2026-11-21', lines_changed: 3, memory_lines: 1, note_lines: 2, skill_suggestions: 1, chats_processed: 2 },
  today: { date: '2026-11-27', lines_changed: 2, skill_suggestions: 1, chats_processed: 0 },
  care_run: { date: '2026-11-27', at: CARE_WRITTEN_AT, lines_changed: 2, skill_suggestions: 1 },
}

/** A workspace where nothing has been recorded yet. */
export const emptyChangesFeed: ChangesFeed = {
  ...changesFeed,
  groups: [],
  week: { since: '2026-11-21', lines_changed: 0, memory_lines: 0, note_lines: 0, skill_suggestions: 0, chats_processed: 0 },
  today: { date: '2026-11-27', lines_changed: 0, skill_suggestions: 0, chats_processed: 0 },
  care_run: null,
  undo_log: 'missing',
}
