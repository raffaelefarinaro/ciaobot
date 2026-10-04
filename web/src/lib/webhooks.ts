import type { WebhookMode, WebhookTrigger } from './types'

/**
 * Pure helpers for the webhook triggers section.
 *
 * Vue-free so the copy a sender copies is testable without mounting anything —
 * the recipe is the one place in the PWA where a machine-facing contract is
 * spelled out, and a wrong verb or header there is a sender that cannot work.
 */

/**
 * The trigger record a write answered with, or `null` when the body is not one.
 *
 * Every write's record is drawn on and written back from, so it is read through
 * a shape rather than trusted: a 200 whose body is not the record is not a
 * record to adopt. Only the fields a later write depends on are checked — the
 * id to find the row and the revision to present — because a narrower check
 * would reject a record the engine did send.
 */
export function webhookTriggerFrom(payload: unknown): WebhookTrigger | null {
  if (!payload || typeof payload !== 'object') return null
  const row = payload as Partial<WebhookTrigger>
  if (typeof row.trigger_id !== 'string' || !row.trigger_id) return null
  if (typeof row.revision !== 'number') return null
  return row as WebhookTrigger
}

/**
 * The `POST /hooks/v1/{trigger_id}` receiver recipe, with this trigger's real id.
 *
 * `triggerId` is the path segment the engine registered, so the text is what a
 * sender needs rather than a shape to fill in. The `<secret>` placeholder is
 * deliberately *not* this trigger's secret: the secret is shown once, in the
 * dialog that mints it, and a recipe that carried it would be a second copy of a
 * credential in a surface that outlives the copy button.
 *
 * The body is exactly `{"text": …}` because `event_text` is the only input
 * policy the store accepts — any other key is a 400. `Idempotency-Key` is not
 * optional in practice: reusing one key with a different body is a 409, so a
 * sender that omits it has no way to retry safely.
 */
export function webhookRecipe(triggerId: string): string {
  return [
    `POST /hooks/v1/${triggerId}`,
    `Authorization: Bearer <secret>`,
    `Idempotency-Key: <a fresh uuid per event>`,
    '',
    `{"text": "the event text"}`,
    '',
    '# 202',
    `{`,
    `  "receipt_id": "...",`,
    `  "trigger_id": "${triggerId}",`,
    `  "status": "accepted"`,
    `}`,
  ].join('\n')
}

/**
 * What a trigger's mode does, in words.
 *
 * The mode is create-only, so this label is the only explanation of it a user
 * ever gets — a row that said `auto` would leave the choice that decides whether
 * a turn asks for approval unexplained.
 */
export function webhookModeLabel(mode: WebhookMode): string {
  if (mode === 'plan') return 'Plan'
  if (mode === 'normal') return 'Normal'
  return 'Auto'
}

/** One sentence on what the mode means, for the create form. */
export function webhookModeHint(mode: WebhookMode): string {
  if (mode === 'plan') return 'Reads the event and writes a plan. It does not act on it.'
  if (mode === 'normal') return 'Answers the event as an ordinary turn.'
  return 'Works the event itself, asking for approval when it needs it.'
}