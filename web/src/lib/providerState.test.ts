import { describe, expect, it } from 'vitest'
import { isMissingInstall, providerStateView } from './providerState'

describe('isMissingInstall', () => {
  it('is true only when the CLI or its adapter is absent', () => {
    expect(isMissingInstall('not_installed')).toBe(true)
    expect(isMissingInstall('adapter_missing')).toBe(true)
    expect(isMissingInstall('ready')).toBe(false)
    expect(isMissingInstall('failed')).toBe(false)
  })
})

describe('providerStateView', () => {
  it('shows a missing CLI as neutral Not installed, never as Not connected', () => {
    expect(providerStateView('claude', 'not_installed')).toEqual({ label: 'Not installed', tone: 'neutral' })
    expect(providerStateView('codex', 'not_installed')).toEqual({ label: 'Not installed', tone: 'neutral' })
    expect(providerStateView('opencode', 'not_installed')).toEqual({ label: 'Not installed', tone: 'neutral' })
  })

  it('keeps an installed CLI without its adapter red, as before', () => {
    expect(providerStateView('claude', 'adapter_missing')).toEqual({ label: 'Not connected', tone: 'error' })
  })

  it('names ready and failed rows per provider', () => {
    expect(providerStateView('claude', 'ready')).toEqual({ label: 'Connected', tone: 'ok' })
    expect(providerStateView('claude', 'failed')).toEqual({ label: 'Not connected', tone: 'error' })
    expect(providerStateView('opencode', 'ready')).toEqual({ label: 'CLI ready', tone: 'ok' })
    expect(providerStateView('opencode', 'failed')).toEqual({ label: 'CLI not ready', tone: 'error' })
  })
})
