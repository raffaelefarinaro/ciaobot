export type RestartBlocker = { chat_id: string; reasons: string[] }

const CARD_REASONS = ['open_question', 'open_permission']

/** Confirmation sentence for the chats whose open question/approval card a restart will close. */
export function describeInterruptedCards(blockers: RestartBlocker[]): string {
  const n = blockers.filter(b => b.reasons.some(r => CARD_REASONS.includes(r))).length
  if (n === 0) return ''
  if (n === 1) return 'Restarting closes the open question or approval card in 1 chat.'
  return `Restarting closes the open question or approval cards in ${n} chats.`
}
