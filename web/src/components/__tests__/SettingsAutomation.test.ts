// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount, type VueWrapper } from '@vue/test-utils'
import SettingsAutomation from '../settings/SettingsAutomation.vue'
import type { AutomationProcess, ProposalOutcomes } from '../../lib/types'

function item(overrides: Partial<AutomationProcess> = {}): AutomationProcess {
  return {
    job: 'insights',
    label: 'Session insights',
    category: 'content',
    description: 'Extracts durable insights from an archived session transcript.',
    last_run: null,
    recent: [],
    stats: { total_runs: 0, success_rate: null, avg_duration_ms: 0, last_error: null },
    ...overrides,
  }
}

function outcomes(overrides: Partial<ProposalOutcomes> = {}): ProposalOutcomes {
  return {
    promoted: 3,
    dismissed: 1,
    by_workspace: { personal: { promoted: 2, dismissed: 1 }, work: { promoted: 1, dismissed: 0 } },
    recent_30d: { promoted: 1, dismissed: 1 },
    ...overrides,
  }
}

const baseProps = {
  automationItems: [item()],
  automationLoaded: true,
  automationError: '',
  fetchAutomation: vi.fn(() => Promise.resolve()),
  notifySaved: vi.fn(),
  notifyFailed: vi.fn(),
  routines: null,
  providerModels: undefined,
  providerLabels: {},
}

let wrapper: VueWrapper | null = null

function mountPanel(props: Record<string, unknown> = {}) {
  wrapper = mount(SettingsAutomation, {
    props: { ...baseProps, ...props },
    global: { plugins: [createPinia()] },
  })
  return wrapper
}

beforeEach(() => {
  setActivePinia(createPinia())
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
})

describe('SettingsAutomation proposal outcomes line', () => {
  it('renders nothing when the server does not serve outcomes', () => {
    const view = mountPanel({ proposalOutcomes: null })
    expect(view.find('.automation-proposals').exists()).toBe(false)
  })

  it('stays hidden while no proposal has ever been resolved', () => {
    // "0 promoted · 0 dismissed" on a fresh install reads as breakage, not as
    // an empty ledger.
    const view = mountPanel({
      proposalOutcomes: outcomes({
        promoted: 0,
        dismissed: 0,
        by_workspace: {},
        recent_30d: { promoted: 0, dismissed: 0 },
      }),
    })
    expect(view.find('.automation-proposals').exists()).toBe(false)
  })

  it('renders one compact promoted/dismissed line with the 30-day count', () => {
    const view = mountPanel({ proposalOutcomes: outcomes() })
    const line = view.find('.automation-proposals')
    expect(line.exists()).toBe(true)
    expect(line.text()).toBe('Memory proposals: 3 promoted · 1 dismissed (2 in last 30 days)')
  })

  it('carries the per-workspace breakdown on the title attribute', () => {
    // Real data without a second row of UI to maintain.
    const view = mountPanel({ proposalOutcomes: outcomes() })
    expect(view.find('.automation-proposals').attributes('title')).toBe(
      'personal: 2 promoted · 1 dismissed\nwork: 1 promoted · 0 dismissed',
    )
  })

  it('labels the install-wide bucket instead of an empty workspace name', () => {
    const view = mountPanel({
      proposalOutcomes: outcomes({
        by_workspace: { '': { promoted: 1, dismissed: 0 } },
      }),
    })
    expect(view.find('.automation-proposals').attributes('title')).toBe(
      'shared: 1 promoted · 0 dismissed',
    )
  })

  it('tolerates a payload missing the newer fields', () => {
    // An older server may omit recent_30d; the line still renders.
    const partial = outcomes() as Partial<ProposalOutcomes>
    delete partial.recent_30d
    const view = mountPanel({ proposalOutcomes: partial as ProposalOutcomes })
    expect(view.find('.automation-proposals').text()).toContain('3 promoted')
  })
})
