import { describe, expect, it } from 'vitest'
import {
  actionNoticeKey,
  notificationAccessibleName,
  notificationBadgeText,
  notificationPath,
  projectNotificationInbox,
  operatorInScope,
  parseHomeNoticeKey,
  scheduleDisplayName,
  type ChatCandidate,
  type MemoryInsightCandidate,
  type TaskReviewCandidate,
  type UpdateTaskCandidate,
  type OperatorCandidate,
  type ScheduleCandidate,
  type SourceSnapshot,
  updateNoticeKey,
} from './notificationInbox'

function chat(o: Partial<ChatCandidate> & { chatId: string }): ChatCandidate {
  return {
    title: '',
    archived: false,
    quietHelper: false,
    needsYou: false,
    needsInput: false,
    failed: false,
    unread: false,
    activityAt: '',
    pendingQuestion: '',
    snippet: '',
    ...o,
  }
}

const READY: SourceSnapshot[] = [
  { source: 'chats', status: 'ready', lastSuccessAt: '2026-10-01T00:00:00+00:00' },
  { source: 'operator', status: 'ready', lastSuccessAt: '2026-10-01T00:00:00+00:00' },
  { source: 'automations', status: 'ready', lastSuccessAt: '2026-10-01T00:00:00+00:00' },
  { source: 'updateTasks', status: 'ready', lastSuccessAt: '2026-10-01T00:00:00+00:00' },
]

function project(chats: ChatCandidate[], sources: SourceSnapshot[] = READY) {
  return projectNotificationInbox({ chats, sources })
}

function keys(inbox: ReturnType<typeof project>, section: string): string[] {
  return inbox.sections.find((s) => s.section === section)!.rows.map((r) => r.key)
}

describe('notificationInbox', () => {
  it('needs_you_beats_unread', () => {
    const inbox = project([
      chat({ chatId: 'a', title: 'Alpha', needsInput: true, needsYou: true, unread: true, pendingQuestion: 'Which branch?', activityAt: '2026-10-01T10:00:00+00:00' }),
      chat({ chatId: 'b', title: 'Beta', unread: true, snippet: 'Done: 3 files', activityAt: '2026-10-01T11:00:00+00:00' }),
    ])
    expect(keys(inbox, 'needsYou')).toEqual(['chat:a'])
    const a = inbox.sections[0].rows[0]
    expect(a.unread).toBe(true)
    expect(a.reason).toBe('Which branch?')
    expect(a.urgency).toBe('input')
    expect(keys(inbox, 'newReplies')).toEqual(['chat:b'])
    expect(inbox.sections[2].rows[0].reason).toBe('Done: 3 files')
    expect(inbox.count).toBe(2)
  })

  it('active_read_chat_keeps_permission', () => {
    const inbox = project([chat({ chatId: 'p', needsInput: true, needsYou: true, unread: false })])
    expect(inbox.sections[0].rows).toHaveLength(1)
    const row = inbox.sections[0].rows[0]
    expect(row.unread).toBe(false)
    expect(row.reason).toBe('Waiting for your answer or approval')
    expect(inbox.count).toBe(1)
  })

  it('archived_and_quiet_helpers_excluded', () => {
    const inbox = project([
      chat({ chatId: 'arch', archived: true, unread: true, needsYou: true }),
      chat({ chatId: 'quiet', quietHelper: true, unread: true }),
      chat({ chatId: 'plain' }),
      chat({ chatId: 'ok', unread: true }),
    ])
    expect(inbox.rows.map((r) => r.key)).toEqual(['chat:ok'])
    expect(inbox.count).toBe(1)
  })

  it('same_titles_order_by_key', () => {
    const input = [
      chat({ chatId: 'c3', title: 'Same', failed: true }),
      chat({ chatId: 'c1', title: 'Same', failed: true }),
      chat({ chatId: 'c2', title: 'Same', failed: true }),
      chat({ chatId: 'z', title: 'Zed', needsInput: true }),
      chat({ chatId: 'r2', title: 'Same', unread: true, activityAt: '2026-10-01T10:00:00+00:00' }),
      chat({ chatId: 'r1', title: 'Same', unread: true, activityAt: '2026-10-01T10:00:00+00:00' }),
    ]
    const before = input.map((c) => c.chatId)
    const inbox = project(input)
    expect(keys(inbox, 'needsYou')).toEqual(['chat:z', 'chat:c1', 'chat:c2', 'chat:c3'])
    expect(keys(inbox, 'newReplies')).toEqual(['chat:r1', 'chat:r2'])
    expect(input.map((c) => c.chatId)).toEqual(before)
  })

  it('badge_counts_rows_not_messages', () => {
    const x = chat({ chatId: 'x', needsYou: true, unread: true })
    const inbox = project([x, x, chat({ chatId: 'y', unread: true })])
    expect(inbox.count).toBe(2)
    expect(notificationBadgeText(99)).toBe('99')
    expect(notificationBadgeText(100)).toBe('99+')
    expect(notificationBadgeText(0)).toBe('0')
    expect(notificationAccessibleName(100, 'ready')).toBe('Notifications, 100 items')
    expect(notificationAccessibleName(1, 'ready')).toBe('Notifications, 1 item')
    expect(notificationAccessibleName(0, 'ready')).toBe('Notifications, none')
  })

  it('zero_with_unavailable_source_is_not_all_clear', () => {
    const unavailable = project([], [{ source: 'chats', status: 'unavailable', lastSuccessAt: '' }])
    expect(unavailable.count).toBe(0)
    expect(unavailable.overall).toBe('unavailable')
    expect(unavailable.allClear).toBe(false)

    const checking = project([], [{ source: 'chats', status: 'checking', lastSuccessAt: '' }])
    expect(checking.overall).toBe('checking')
    expect(checking.allClear).toBe(false)

    const missing = project([], [])
    expect(missing.overall).toBe('checking')
    expect(missing.sources[0]).toMatchObject({ source: 'chats', status: 'checking', lastSuccessAt: '', label: 'Chats' })
    expect(missing.allClear).toBe(false)

    expect(project([]).allClear).toBe(true)

    const stale = project([chat({ chatId: 'u', unread: true })], [{ source: 'chats', status: 'stale', lastSuccessAt: 'x' }])
    expect(stale.rows).toHaveLength(1)
    expect(stale.rows[0].freshness).toBe('stale')
    expect(stale.allClear).toBe(false)

    const name = notificationAccessibleName(0, 'unavailable')
    expect(name).toContain('could not be fully checked')
    expect(name.toLowerCase()).not.toContain('all clear')
  })

  it('unknown_timestamps_are_not_invented', () => {
    const inbox = project([
      chat({ chatId: 'u1', unread: true, activityAt: '' }),
      chat({ chatId: 'u2', unread: true, activityAt: '2026-10-01T09:00:00+00:00' }),
      chat({ chatId: 'u3', unread: true, activityAt: '   ' }),
      chat({ chatId: 'n', needsYou: true }),
    ])
    const by = (k: string) => inbox.rows.find((r) => r.key === k)!
    expect('timestamp' in by('chat:u1')).toBe(false)
    expect('timestamp' in by('chat:u3')).toBe(false)
    expect('timestamp' in by('chat:n')).toBe(false)
    expect(keys(inbox, 'newReplies')).toEqual(['chat:u2', 'chat:u1', 'chat:u3'])
    expect(by('chat:u2').timestamp).toBe('2026-10-01T09:00:00+00:00')
  })

  it('orders replies newest first', () => {
    const inbox = project([
      chat({ chatId: 'a', unread: true, activityAt: '2026-10-01T08:00:00+00:00' }),
      chat({ chatId: 'b', unread: true, activityAt: '2026-10-01T12:00:00+00:00' }),
      chat({ chatId: 'c', unread: true, activityAt: '2026-10-01T10:00:00+00:00' }),
    ])
    expect(keys(inbox, 'newReplies')).toEqual(['chat:b', 'chat:c', 'chat:a'])
  })

  it('builds an encoded chat path', () => {
    expect(notificationPath({ kind: 'chat', chatId: 'a/b c' })).toBe('/chat/a%2Fb%20c')
  })

  it('collapses and truncates reasons', () => {
    const long = 'line one\n\n' + 'x'.repeat(300)
    const inbox = project([chat({ chatId: 'u', unread: true, snippet: long })])
    const reason = inbox.rows[0].reason
    expect(reason).not.toMatch(/\s{2,}|\n/)
    expect(reason.length).toBeLessThanOrEqual(160)
    expect(reason.endsWith('…')).toBe(true)
  })

  it('failed chat without input is a failure', () => {
    const inbox = project([chat({ chatId: 'f', failed: true })])
    expect(inbox.rows[0].urgency).toBe('failure')
    expect(inbox.rows[0].reason).toBe('The last turn failed')
    expect(inbox.rows[0].title).toBe('Untitled chat')
  })
})

const READY3: SourceSnapshot[] = [
  { source: 'chats', status: 'ready', lastSuccessAt: 'x' },
  { source: 'operator', status: 'ready', lastSuccessAt: 'x' },
  { source: 'automations', status: 'ready', lastSuccessAt: 'x' },
  { source: 'updateTasks', status: 'ready', lastSuccessAt: 'x' },
]

function op(o: Partial<OperatorCandidate> & { id: string }): OperatorCandidate {
  return { kind: 'skill-triage-pending', workspace: '', title: 'T', detail: '', blocking: false, viewRoute: '', ...o }
}

function sched(o: Partial<ScheduleCandidate> & { scheduleId: string }): ScheduleCandidate {
  return {
    title: '', prompt: '', frequency: 'daily', enabled: true, missed: false,
    lastStatus: '', needsAttention: true, ...o,
  }
}

function project3(
  o: { chats?: ChatCandidate[]; operators?: OperatorCandidate[]; schedules?: ScheduleCandidate[]; sources?: SourceSnapshot[] },
) {
  return projectNotificationInbox({
    chats: o.chats ?? [],
    operators: o.operators,
    schedules: o.schedules,
    sources: o.sources ?? READY3,
  })
}

describe('notificationInbox operator and automation sources', () => {
  it('operator_blocking_is_needs_you_nonblocking_is_to_review', () => {
    const inbox = project3({
      operators: [op({ id: 'a', blocking: true, title: 'Blocker' }), op({ id: 'b', blocking: false, title: 'Soft' })],
    })
    const a = inbox.rows.find((r) => r.key === 'operator:shared:a')!
    const b = inbox.rows.find((r) => r.key === 'operator:shared:b')!
    expect(a).toMatchObject({ section: 'needsYou', urgency: 'blocking', kind: 'operator-action', source: 'operator', title: 'Blocker' })
    expect(b).toMatchObject({ section: 'toReview', urgency: 'review' })
  })

  it('blocking_operator_sorts_between_input_chat_and_failed_chat', () => {
    const inbox = project3({
      chats: [
        chat({ chatId: 'f', title: 'A', failed: true }),
        chat({ chatId: 'i', title: 'Z', needsInput: true }),
      ],
      operators: [op({ id: 'b', blocking: true, title: 'M' })],
    })
    expect(keys(inbox, 'needsYou')).toEqual(['chat:i', 'operator:shared:b', 'chat:f'])
  })

  it('github_star_excluded', () => {
    const inbox = project3({
      operators: [op({ id: 'g', kind: 'github-star' }), op({ id: 'l', kind: 'legacy-env-ignored' })],
    })
    expect(inbox.rows.map((r) => r.key)).toEqual(['operator:shared:l'])
  })

  it('operator_scope_helper', () => {
    expect(operatorInScope('', 'personal')).toBe(true)
    expect(operatorInScope('personal', 'personal')).toBe(true)
    expect(operatorInScope('work', 'personal')).toBe(false)
    expect(operatorInScope('work', '')).toBe(true)
  })

  it.each([
    ['package-update', '', '/settings'],
    ['provider-update', '', '/settings/providers'],
    ['other', '/settings', '/settings'],
    ['other', '/settings/providers', '/settings/providers'],
    ['other', '', '/?notice=action%3Ashared%3Ax'],
    ['other', '/schedules/x', '/?notice=action%3Ashared%3Ax'],
    ['other', 'https://evil', '/?notice=action%3Ashared%3Ax'],
    ['other', '//x', '/?notice=action%3Ashared%3Ax'],
    ['other', '/api/x', '/?notice=action%3Ashared%3Ax'],
  ])('operator_destinations %s %s', (kind, viewRoute, path) => {
    const inbox = project3({ operators: [op({ id: 'x', kind, viewRoute })] })
    expect(notificationPath(inbox.rows[0].destination)).toBe(path)
    if (path.startsWith('/?notice=')) {
      expect(inbox.rows[0].destination).toEqual({ kind: 'home-notice', noticeKey: 'action:shared:x' })
    }
  })

  it('operator_home_notice_uses_the_workspace', () => {
    const inbox = project3({ operators: [op({ id: 'x', kind: 'other', workspace: 'personal' })] })
    expect(inbox.rows[0].destination).toEqual({ kind: 'home-notice', noticeKey: 'action:personal:x' })
  })

  it('notice_key_builders_and_parser', () => {
    expect(actionNoticeKey('', 'x')).toBe('action:shared:x')
    expect(actionNoticeKey('personal', 'x')).toBe('action:personal:x')
    expect(updateNoticeKey('', 't', 2)).toBe('update:shared:t@2')
    expect(parseHomeNoticeKey(actionNoticeKey('personal', 'x'))).toEqual({ kind: 'action', workspace: 'personal', id: 'x' })
    expect(parseHomeNoticeKey(updateNoticeKey('work', 't', 2))).toEqual({ kind: 'update', workspace: 'work', id: 't@2' })
    for (const bad of ['', 'foo:a:b', 'action::x', 'action:ws']) expect(parseHomeNoticeKey(bad)).toBeNull()
  })

  it('home_notice_path_encodes', () => {
    expect(notificationPath({ kind: 'home-notice', noticeKey: 'action:shared:a&b#c/d' })).toBe(
      '/?notice=action%3Ashared%3Aa%26b%23c%2Fd',
    )
  })

  it('operator_reason_strips_markdown', () => {
    const inbox = project3({
      operators: [
        op({ id: 'a', detail: '- **x** [name](/schedules/a) `code`' }),
        op({ id: 'b', detail: '# Head\n' + 'y'.repeat(300) }),
      ],
    })
    const by = (k: string) => inbox.rows.find((r) => r.key === k)!
    expect(by('operator:shared:a').reason).toBe('x name code')
    const long = by('operator:shared:b').reason
    expect(long.length).toBeLessThanOrEqual(160)
    expect(long.endsWith('…')).toBe(true)
    expect(long.startsWith('Head ')).toBe(true)
  })

  it('missed_aggregate_labelled_all_workspaces', () => {
    const inbox = project3({
      operators: [op({ id: 'missed-schedules', kind: 'missed-schedules', title: '2 reminders missed', detail: '[a](/schedules/a)' })],
    })
    expect(inbox.rows).toHaveLength(1)
    expect(inbox.rows[0]).toMatchObject({
      key: 'operator:shared:missed-schedules',
      kind: 'missed-schedules',
      section: 'needsYou',
      urgency: 'failure',
      source: 'operator',
      title: 'All workspaces: 2 reminders missed',
      reason: 'All workspaces. Open Home to fire or dismiss them.',
      destination: { kind: 'home-notice', noticeKey: 'action:shared:missed-schedules' },
    })
  })

  const agg = op({ id: 'missed-schedules', kind: 'missed-schedules', title: 'Missed' })
  const onceMissed = (o: Partial<ScheduleCandidate> = {}) =>
    sched({ scheduleId: 's', title: 'Remind', frequency: 'once', enabled: true, missed: true, lastStatus: '', ...o })

  it('missed_aggregate_suppresses_enabled_once_without_error', () => {
    const withAgg = project3({ operators: [agg], schedules: [onceMissed()] })
    expect(withAgg.rows.map((r) => r.key)).toEqual(['operator:shared:missed-schedules'])
    const without = project3({ schedules: [onceMissed()] })
    expect(without.rows.map((r) => r.key)).toEqual(['schedule:s'])
  })

  it.each([
    ['error', { lastStatus: 'error' as const }],
    ['missing-chat', { lastStatus: 'missing-chat' as const }],
    ['disabled once', { enabled: false, lastStatus: 'missing-chat' as const }],
  ])('missed_aggregate_keeps_%s', (_n, o) => {
    const inbox = project3({ operators: [agg], schedules: [onceMissed(o)] })
    expect(inbox.rows.map((r) => r.key).sort()).toEqual(['operator:shared:missed-schedules', 'schedule:s'])
  })

  it('automation_rows', () => {
    const inbox = project3({
      schedules: [
        sched({ scheduleId: 'e', title: 'Err', lastStatus: 'error' }),
        sched({ scheduleId: 'm', title: 'Gone', lastStatus: 'missing-chat', enabled: false }),
        sched({ scheduleId: 'x', prompt: 'Run the thing\nmore', missed: true }),
        sched({ scheduleId: 'h', title: 'Healthy', enabled: false, needsAttention: false }),
      ],
    })
    const by = (k: string) => inbox.rows.find((r) => r.key === k)!
    expect(inbox.rows).toHaveLength(3)
    expect(by('schedule:e')).toMatchObject({
      kind: 'automation-attention', section: 'needsYou', urgency: 'failure', source: 'automations',
      title: 'Err', reason: 'The last run failed.', destination: { kind: 'schedule', scheduleId: 'e' },
    })
    expect(by('schedule:m').reason).toBe('Its chat was deleted, so it is turned off.')
    expect(by('schedule:x').reason).toBe('It did not run when expected.')
    expect(by('schedule:x').title).toBe('Run the thing')
    expect(notificationPath(by('schedule:e').destination)).toBe('/schedules/e')
    expect(keys(inbox, 'needsYou')).toEqual(['schedule:e', 'schedule:m', 'schedule:x'])
  })

  it('schedule_display_name', () => {
    expect(scheduleDisplayName({ title: '  T  ', prompt: 'p', scheduleId: 'i' })).toBe('T')
    expect(scheduleDisplayName({ title: '', prompt: '\n first \nsecond', scheduleId: 'i' })).toBe('first')
    expect(scheduleDisplayName({ title: '', prompt: 'a'.repeat(70), scheduleId: 'i' })).toBe(`${'a'.repeat(57)}…`)
    expect(scheduleDisplayName({ title: '', prompt: '  ', scheduleId: 'i' })).toBe('i')
  })

  it('automation_suppresses_linked_failed_and_unread_chat', () => {
    const inbox = project3({
      schedules: [sched({ scheduleId: 'S', title: 'Brief', lastStatus: 'error' })],
      chats: [
        chat({ chatId: 'f', failed: true, scheduleId: 'S' }),
        chat({ chatId: 'u', unread: true, scheduleId: 'S' }),
      ],
    })
    expect(inbox.rows.map((r) => r.key)).toEqual(['schedule:S'])
  })

  it('automation_keeps_live_question_chat', () => {
    const inbox = project3({
      schedules: [sched({ scheduleId: 'S', title: 'Brief', lastStatus: 'error' })],
      chats: [
        chat({ chatId: 'q', needsInput: true, needsYou: true, scheduleId: 'S' }),
        chat({ chatId: 'q2', needsInput: true, failed: true, scheduleId: 'S' }),
        chat({ chatId: 'd', needsYou: true, scheduleId: 'S' }),
      ],
    })
    expect(inbox.rows.map((r) => r.key).sort()).toEqual(['chat:d', 'chat:q', 'chat:q2', 'schedule:S'])
  })

  it('automation_does_not_suppress_unlinked_chat', () => {
    const inbox = project3({
      schedules: [sched({ scheduleId: 'S', lastStatus: 'error' })],
      chats: [
        chat({ chatId: 'a', failed: true }),
        chat({ chatId: 'b', failed: true, scheduleId: 'OTHER' }),
        chat({ chatId: 'c', unread: true, scheduleId: '' }),
      ],
    })
    expect(inbox.rows.map((r) => r.key).sort()).toEqual(['chat:a', 'chat:b', 'chat:c', 'schedule:S'])
  })

  it('suppressed_automation_does_not_suppress_chat', () => {
    const inbox = project3({
      operators: [agg],
      schedules: [onceMissed()],
      chats: [chat({ chatId: 'f', failed: true, scheduleId: 's' })],
    })
    expect(inbox.rows.map((r) => r.key).sort()).toEqual(['chat:f', 'operator:shared:missed-schedules'])
  })

  it('count_is_distinct_rows_across_sources', () => {
    const inbox = project3({
      chats: [chat({ chatId: 'c', unread: true })],
      operators: [op({ id: 'o' }), op({ id: 'o' })],
      schedules: [sched({ scheduleId: 's', lastStatus: 'error' })],
    })
    expect(inbox.count).toBe(3)
  })

  it('freshness_follows_own_source', () => {
    const inbox = project3({
      chats: [chat({ chatId: 'c', unread: true })],
      operators: [op({ id: 'o' })],
      schedules: [sched({ scheduleId: 's', lastStatus: 'error' })],
      sources: [
        { source: 'chats', status: 'stale', lastSuccessAt: 'x' },
        { source: 'operator', status: 'ready', lastSuccessAt: 'x' },
        { source: 'automations', status: 'unavailable', lastSuccessAt: '' },
      ],
    })
    const f = (k: string) => inbox.rows.find((r) => r.key === k)!.freshness
    expect(f('chat:c')).toBe('stale')
    expect(f('operator:shared:o')).toBe('fresh')
    expect(f('schedule:s')).toBe('stale')
  })

  it('notification_path_new_kinds', () => {
    expect(notificationPath({ kind: 'schedule', scheduleId: 'a b' })).toBe('/schedules/a%20b')
    expect(notificationPath({ kind: 'settings', tab: '' })).toBe('/settings')
    expect(notificationPath({ kind: 'settings', tab: 'providers' })).toBe('/settings/providers')
    expect(notificationPath({ kind: 'home-notice', noticeKey: 'action:shared:x' })).toBe('/?notice=action%3Ashared%3Ax')
  })

  it('sources_four_keys_in_order', () => {
    const missing = project3({ sources: [{ source: 'chats', status: 'ready', lastSuccessAt: 'x' }] })
    expect(missing.sources.map((s) => [s.source, s.status, s.label])).toEqual([
      ['chats', 'ready', 'Chats'],
      ['operator', 'checking', 'Settings'],
      ['automations', 'checking', 'Automations'],
      ['updateTasks', 'checking', 'Update tasks'],
    ])
    expect(missing.allClear).toBe(false)
    expect(project3({}).allClear).toBe(true)
  })
})

function inboxWith(o: {
  chats?: ChatCandidate[]
  reviewTasks?: TaskReviewCandidate[]
  memoryInsights?: MemoryInsightCandidate[]
  sources?: SourceSnapshot[]
  workspace?: string
}) {
  return projectNotificationInbox({
    workspace: o.workspace ?? 'personal',
    chats: o.chats ?? [],
    reviewTasks: o.reviewTasks,
    memoryInsights: o.memoryInsights,
    sources: o.sources ?? READY,
  })
}

function insight(o: Partial<MemoryInsightCandidate> & { sourceChatId: string }): MemoryInsightCandidate {
  return {
    passChatId: `pass-${o.sourceChatId}`, title: 'Source', phase: 'needsYou', question: '',
    timestamp: '', ...o,
  }
}

describe('notificationInbox review, queue and memory rows', () => {
  it('task_review_row', () => {
    const inbox = inboxWith({ reviewTasks: [{ taskId: 'a b', title: 'Ship it' }] })
    expect(inbox.rows).toHaveLength(1)
    const r = inbox.rows[0]
    expect(r.key).toBe('task:personal:a b')
    expect(r.kind).toBe('task-review')
    expect(r.title).toBe('Ship it')
    expect(r.reason).toBe('The agent says it is done. Approve or send it back.')
    expect(r.section).toBe('toReview')
    expect(r.source).toBe('chats')
    expect(r.destination).toEqual({ kind: 'task', taskId: 'a b' })
    expect(notificationPath(r.destination)).toBe('/tasks/a%20b')
    expect(r.timestamp).toBeUndefined()
  })

  it('task_review_suppresses_only_unread_result_chat', () => {
    const reviewTasks = [{ taskId: 't1', title: 'T' }]
    const base = { title: 'Result', unread: true, reviewTaskId: 't1' }
    const chatKeys = (c: ChatCandidate) =>
      inboxWith({ reviewTasks, chats: [c] }).rows.map((r) => r.key).filter((k) => k.startsWith('chat:'))
    expect(chatKeys(chat({ chatId: 'x', ...base }))).toEqual([])
    expect(chatKeys(chat({ chatId: 'x', ...base, needsInput: true, needsYou: true }))).toEqual(['chat:x'])
    expect(chatKeys(chat({ chatId: 'x', ...base, failed: true }))).toEqual(['chat:x'])
    expect(chatKeys(chat({ chatId: 'x', ...base, needsYou: true }))).toEqual(['chat:x'])
    expect(chatKeys(chat({ chatId: 'x', ...base, reviewTaskId: 'other' }))).toEqual(['chat:x'])
  })

  it('memory_insight_rows', () => {
    const inbox = inboxWith({
      memoryInsights: [
        insight({ sourceChatId: 's1', title: 'Plan', question: 'Keep this?', timestamp: '2026-10-01T09:00:00+00:00' }),
        insight({ sourceChatId: 's2', title: 'Notes', phase: 'attention' }),
        insight({ sourceChatId: 's3', title: 'Bare' }),
      ],
      chats: [chat({ chatId: 'pass-s1', needsInput: true, needsYou: true })],
    })
    const by = (k: string) => inbox.rows.find((r) => r.key === k)!
    expect(by('memory:s1').urgency).toBe('input')
    expect(by('memory:s1').title).toBe('Memory update: Plan')
    expect(by('memory:s1').reason).toBe('Keep this?')
    expect(by('memory:s1').timestamp).toBe('2026-10-01T09:00:00+00:00')
    expect(by('memory:s1').destination).toEqual({ kind: 'chat', chatId: 'pass-s1' })
    expect(by('memory:s2').urgency).toBe('failure')
    expect(by('memory:s2').reason).toBe('The memory update needs attention')
    expect(by('memory:s3').reason).toBe('The memory update is waiting for your answer or approval')
    expect(inbox.sections[0].rows.map((r) => r.key)).toEqual(['memory:s3', 'memory:s1', 'memory:s2'])
    expect(inbox.rows.some((r) => r.key === 'chat:pass-s1')).toBe(false)
  })

  it('count_is_distinct_rows_with_new_sources', () => {
    const inbox = inboxWith({
      reviewTasks: [{ taskId: 't', title: 'T' }],
      memoryInsights: [insight({ sourceChatId: 's' })],
      chats: [chat({ chatId: 'u', unread: true })],
    })
    expect(inbox.count).toBe(3)
    expect(inbox.rows).toHaveLength(3)
  })

  it('keys_use_shared_for_empty_workspace', () => {
    const inbox = inboxWith({ workspace: '', reviewTasks: [{ taskId: 't', title: 'T' }] })
    expect(inbox.rows.map((r) => r.key).sort()).toEqual(['task:shared:t'])
  })
})

function ut(o: Partial<UpdateTaskCandidate> = {}): UpdateTaskCandidate {
  return {
    id: 't1', revision: 1, workspace: 'personal', title: 'Task', status: 'offered',
    applicability: 'applicable', chatId: '', chatLive: false, chatNeedsInput: false, chatFailed: false,
    ...o,
  }
}

function projectUt(updateTasks: UpdateTaskCandidate[], chats: ChatCandidate[] = []) {
  return projectNotificationInbox({ workspace: 'personal', chats, updateTasks, sources: READY })
}

describe('update-task rows', () => {
  const STATUSES = ['offered', 'in_progress', 'waiting_review', 'failed', 'completed', 'dismissed']
  const APPLIC = ['applicable', 'not_applicable', 'unknown']
  const expected = (status: string, applicability: string): string | null => {
    if (status === 'offered') return applicability === 'not_applicable' ? null : 'toReview'
    if (status === 'waiting_review') return 'toReview'
    if (status === 'failed') return 'needsYou'
    return null
  }
  const cases = STATUSES.flatMap((s) => APPLIC.map((a) => [s, a, expected(s, a)] as const))

  it.each(cases)('update_task_matrix %s x %s', (status, applicability, section) => {
    const inbox = projectUt([ut({ status, applicability })])
    if (section === null) {
      expect(inbox.rows).toHaveLength(0)
      return
    }
    expect(inbox.rows).toHaveLength(1)
    expect(inbox.rows[0].section).toBe(section)
    if (status === 'offered' && applicability === 'applicable') expect(inbox.rows[0].reason).toBe('New since this update.')
    if (status === 'offered' && applicability === 'unknown') expect(inbox.rows[0].title).toBe('Check applicability: Task')
    else expect(inbox.rows[0].title).toBe('Task')
  })

  it('failed_live_task_survives_changed_applicability', () => {
    expect(projectUt([ut({ status: 'failed', applicability: 'not_applicable' })]).rows).toHaveLength(1)
    expect(projectUt([ut({ status: 'offered', applicability: 'not_applicable' })]).rows).toHaveLength(0)
  })

  it('in_progress_contributes_only_for_failed_chat', () => {
    const failed = projectUt([ut({ status: 'in_progress', chatId: 'c', chatLive: true, chatFailed: true })])
    expect(failed.rows).toHaveLength(1)
    expect(failed.rows[0].section).toBe('needsYou')
    expect(failed.rows[0].urgency).toBe('failure')
    expect(projectUt([ut({ status: 'in_progress', chatId: 'c', chatLive: true, chatNeedsInput: true })]).rows).toHaveLength(0)
    expect(projectUt([ut({ status: 'in_progress', chatId: 'c', chatLive: true })]).rows).toHaveLength(0)
  })

  it('key_carries_workspace_id_and_revision', () => {
    const inbox = projectUt([ut({ revision: 1 }), ut({ revision: 2 })])
    expect(inbox.rows.map((r) => r.key).sort()).toEqual(['update-task:personal:t1@1', 'update-task:personal:t1@2'])
    const paths = inbox.rows.map((r) => notificationPath(r.destination)).sort()
    expect(paths).toEqual(['/?notice=update%3Apersonal%3At1%401', '/?notice=update%3Apersonal%3At1%402'])
    expect(projectUt([ut({ workspace: '' })]).rows[0].key).toBe('update-task:shared:t1@1')
  })

  it('destination_is_always_home_notice', () => {
    const inbox = projectUt([ut({ status: 'waiting_review', chatId: '', chatLive: false })])
    expect(notificationPath(inbox.rows[0].destination)).toBe('/?notice=update%3Apersonal%3At1%401')
    const live = projectUt([ut({ status: 'waiting_review', chatId: 'c1', chatLive: true })])
    expect(notificationPath(live.rows[0].destination)).toBe('/?notice=update%3Apersonal%3At1%401')
  })

  it('question_chat_preferred_over_task_row', () => {
    const inbox = projectUt(
      [ut({ status: 'in_progress', chatId: 'c', chatLive: true, chatNeedsInput: true })],
      [chat({ chatId: 'c', needsInput: true, needsYou: true })],
    )
    expect(inbox.rows.map((r) => r.key)).toEqual(['chat:c'])
  })

  it('failed_chat_and_unread_twin_suppressed_by_task_row', () => {
    const task = ut({ status: 'in_progress', chatId: 'c', chatLive: true, chatFailed: true })
    const failedChat = projectUt([task], [chat({ chatId: 'c', failed: true, needsYou: true })])
    expect(failedChat.rows.map((r) => r.key)).toEqual(['update-task:personal:t1@1'])
    const twin = projectUt([ut({ status: 'waiting_review', chatId: 'c', chatLive: true })], [chat({ chatId: 'c', unread: true })])
    expect(twin.rows.map((r) => r.key)).toEqual(['update-task:personal:t1@1'])
  })

  it('unlinked_chat_untouched', () => {
    const inbox = projectUt([ut({ status: 'failed' })], [chat({ chatId: 'other', unread: true })])
    expect(inbox.rows.map((r) => r.key).sort()).toEqual(['chat:other', 'update-task:personal:t1@1'])
  })

  it('in_progress_unread_chat_keeps_chat_row', () => {
    const inbox = projectUt(
      [ut({ status: 'in_progress', chatId: 'c', chatLive: true })],
      [chat({ chatId: 'c', unread: true })],
    )
    expect(inbox.rows.map((r) => r.key)).toEqual(['chat:c'])
  })

  it('wording', () => {
    const reason = (o: Partial<UpdateTaskCandidate>) => projectUt([ut(o)]).rows[0].reason
    expect(reason({ status: 'failed' })).toBe('The last attempt did not get going.')
    expect(reason({ status: 'in_progress', chatFailed: true, chatId: 'c', chatLive: true })).toBe('Its chat failed.')
    expect(reason({ status: 'waiting_review', chatLive: true, chatId: 'c' }))
      .toBe('Its chat is open and waiting for you to decide what to do with it.')
    expect(reason({ status: 'waiting_review' }))
      .toBe('Its chat was archived or deleted. Open the notice to start a new one.')
    expect(reason({ applicability: 'unknown' })).toBe('Not yet known whether this applies here. Open it to check again.')
    expect(reason({})).toBe('New since this update.')
    expect(projectUt([ut({ status: 'waiting_review', applicability: 'unknown' })]).rows[0].title).toBe('Task')
  })
})
