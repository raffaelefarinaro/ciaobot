import type { ProviderState } from './types'

/** Whether the CLI itself is missing or lacks its adapter, so chats cannot run yet. */
export function isMissingInstall(state: ProviderState): boolean {
  return state === 'not_installed' || state === 'adapter_missing'
}

export interface ProviderStateView {
  /** Visible words for the state. Colour is never the only signal. */
  label: string
  /** `neutral` is a choice (not installed), not a fault: no red. */
  tone: 'ok' | 'error' | 'neutral'
}

/**
 * The card and rail wording for one provider row. opencode says "CLI ready"
 * because its credentials are optional; the others say "Connected".
 */
export function providerStateView(connKey: string, state: ProviderState): ProviderStateView {
  const isCli = connKey === 'opencode'
  switch (state) {
    case 'ready':
      return { label: isCli ? 'CLI ready' : 'Connected', tone: 'ok' }
    case 'not_installed':
      return { label: 'Not installed', tone: 'neutral' }
    default:
      // adapter_missing and failed, and any row without a known state: not
      // connected, in the error tone, so nothing reads as ready by accident.
      return { label: isCli ? 'CLI not ready' : 'Not connected', tone: 'error' }
  }
}
