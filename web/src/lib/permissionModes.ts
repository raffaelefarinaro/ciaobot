/**
 * The permission modes the engine runs a chat in, with the words the PWA uses
 * for them. Pure: no Vue imports. Settings → Providers and the needs-you card
 * both read these, so the two cannot drift apart.
 */

/** The engine's mode names (`BridgeMode`). The PWA's "manual" is `normal`. */
export type BridgeMode = 'plan' | 'normal' | 'auto' | 'bypass'

export const PERMISSION_MODE_COPY: Record<BridgeMode, { name: string; allows: string }> = {
  plan: { name: 'Plan', allows: 'read and propose only, change nothing' },
  normal: { name: 'Manual', allows: 'ask for every action' },
  auto: { name: 'Auto', allows: 'allow safe work, ask on risky' },
  bypass: { name: 'Bypass', allows: 'allow everything' },
}

/** The mode with what it allows, e.g. `Manual — ask for every action`. */
export function modeLabel(mode: BridgeMode): string {
  const { name, allows } = PERMISSION_MODE_COPY[mode]
  return `${name} — ${allows}`
}

export function isBridgeMode(value: unknown): value is BridgeMode {
  return typeof value === 'string' && Object.prototype.hasOwnProperty.call(PERMISSION_MODE_COPY, value)
}
