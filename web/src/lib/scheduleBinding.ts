/**
 * "Does this schedule run into one existing chat?" — the single rule behind
 * two distinct consequences, which is why it had started to be written out
 * separately at each of them.
 *
 * An interval entry with a `web_chat_id` and no `web_project_id` posts every
 * run into that one conversation. That means:
 *
 *  - it inherits the chat's own model and mode (`prepare_schedule_chat`
 *    deliberately skips the override), so a model picker on the schedule
 *    reports a setting that never takes effect; and
 *  - it cannot auto-archive, because `_rehome_interval_chat` forks a
 *    replacement whenever it finds the target archived, so archiving after a
 *    clean run makes the next run fork, run, and archive again, forever.
 *
 * A project-bound interval entry opens a fresh chat per run and has neither
 * consequence. Mirrors `ciao/schedules.py::supports_auto_archive`, which is
 * what the store enforces on every write.
 */
import type { Schedule } from './types'

/** The rule, on the two fields a stored schedule carries. */
export function bindsFixedChat(
  frequency: string,
  webChatId?: string | null,
  webProjectId?: string | null,
): boolean {
  return frequency === 'interval' && !!webChatId && !webProjectId
}

/** The rule, on the single `contextKey` the create/edit forms bind to. */
export function contextBindsFixedChat(frequency: string, contextKey: string): boolean {
  return frequency === 'interval' && contextKey.startsWith('web:')
}

/** False where the dispatcher refuses to honour an `auto` archive policy. */
export function scheduleSupportsAutoArchive(
  s: Pick<Schedule, 'frequency' | 'web_chat_id' | 'web_project_id'>,
): boolean {
  return !bindsFixedChat(s.frequency, s.web_chat_id, s.web_project_id)
}
