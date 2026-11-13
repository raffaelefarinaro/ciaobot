import type { ChatMessage, TurnVerification } from './types'

/** The footer badge for a turn's vault check, or null when the answer did not
 *  depend on the vault (only general-knowledge claims). */
export function verificationBadge(
  v: TurnVerification | undefined | null,
): { label: string; tone: 'ok' | 'warn'; ariaLabel: string } | null {
  if (!v) return null
  const checked = v.supported + v.unsupported
  if (checked === 0) return null
  const aria = `Vault check: ${v.supported} of ${checked} claims supported by your notes`
  return {
    label: `Vault ${v.supported}/${checked}`,
    tone: v.unsupported > 0 ? 'warn' : 'ok',
    ariaLabel: v.unsupported > 0 ? `${aria}, ${v.unsupported} not supported` : aria,
  }
}

/** Attach a live verification to the turn's last assistant row, in place.
 *  Returns false when the turn or its assistant row is not in the rows. */
export function applyTurnVerification(
  rows: ChatMessage[],
  turnIndex: number,
  v: TurnVerification,
): boolean {
  const start = rows.findIndex(r => r.role === 'user' && r.turn_index === turnIndex)
  if (start < 0) return false
  let target = -1
  for (let i = start + 1; i < rows.length && rows[i].role !== 'user'; i++) {
    if (rows[i].role === 'assistant') target = i
  }
  if (target < 0) return false
  rows[target].verification = v
  return true
}
