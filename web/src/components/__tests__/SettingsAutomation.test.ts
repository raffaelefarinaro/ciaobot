// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount, type VueWrapper } from '@vue/test-utils'
import { nextTick } from 'vue'
import SettingsAutomation from '../settings/SettingsAutomation.vue'
import { useTaskStore } from '../../stores/tasks'
import type { AutomationProcess, ProposalOutcomes, RoutineSettings, Schedule } from '../../lib/types'

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

function schedule(overrides: Partial<Schedule> = {}): Schedule {
  return {
    schedule_id: 'system-memory-curation@personal',
    daily_time_utc: '03:00',
    prompt: 'Tend the workspace.',
    chat_id: 0,
    created_at: '2026-01-01T00:00:00Z',
    timezone_name: 'Europe/Zurich',
    last_triggered_on: '2026-09-27',
    days_of_week: null,
    thread_id: null,
    context_label: 'Workspace care',
    frequency: 'daily',
    interval_minutes: 0,
    day_of_month: null,
    run_at_date: null,
    web_chat_id: null,
    web_project_id: 'proj-1',
    workspace: 'personal',
    model: 'sonnet',
    next_run: '2026-09-29T03:00:00Z',
    last_expected_run: null,
    missed: false,
    enabled: true,
    archive_policy: 'auto',
    ...overrides,
  } as Schedule
}

const baseProps = {
  automationItems: [item()],
  automationLoaded: true,
  automationError: '',
  fetchAutomation: vi.fn(() => Promise.resolve()),
  notifySaved: vi.fn(),
  notifyFailed: vi.fn(),
  routines: null,
  routinesSaving: false,
  saveRoutines: vi.fn(() => Promise.resolve()),
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
  vi.clearAllMocks()
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
    const line = view.find('.automation-proposals-total')
    expect(line.exists()).toBe(true)
    expect(line.text()).toBe('Memory proposals: 3 promoted · 1 dismissed (2 in last 30 days)')
  })

  it('renders the per-workspace breakdown as visible text, not hover-only', () => {
    // Touch and keyboard users cannot reveal a title tooltip; the split must
    // be real content.
    const view = mountPanel({ proposalOutcomes: outcomes() })
    const lines = view.findAll('.automation-proposals-workspace')
    expect(lines.map((l) => l.text())).toEqual([
      'personal: 2 promoted · 1 dismissed',
      'work: 1 promoted · 0 dismissed',
    ])
  })

  it('labels the install-wide bucket instead of an empty workspace name', () => {
    const view = mountPanel({
      proposalOutcomes: outcomes({
        by_workspace: { '': { promoted: 1, dismissed: 0 } },
      }),
    })
    const lines = view.findAll('.automation-proposals-workspace')
    expect(lines.map((l) => l.text())).toEqual(['shared: 1 promoted · 0 dismissed'])
  })

  it('tolerates a payload missing the newer fields', () => {
    // An older server may omit recent_30d; the line still renders.
    const partial = outcomes() as Partial<ProposalOutcomes>
    delete partial.recent_30d
    const view = mountPanel({ proposalOutcomes: partial as ProposalOutcomes })
    expect(view.find('.automation-proposals').text()).toContain('3 promoted')
  })
})

describe('SettingsAutomation legacy schedule mapping', () => {
  it('does not offer a retired skill-evolution schedule while keeping live legacy mapping', async () => {
    const view = mountPanel({
      automationItems: [
        // No `schedule_id`: the row predates the API that reports one.
        item({ job: 'skill_evolution', label: 'Skill reflection' }),
        item({ job: 'memory_proposals', label: 'Memory proposals' }),
      ],
    })

    // An install upgraded from a build that still ran the weekly
    // skill-evolution producer keeps reporting its row, and the persisted
    // `system-skill-evolution@<workspace>` schedule may still be in the store.
    // Neither may put a "Run now" on a row whose schedule no longer ships; the
    // legacy mappings that do resolve keep their action. Read the store after
    // the mount so this is the pinia the panel actually renders from.
    const tasks = useTaskStore()
    tasks.schedules = [
      schedule({ schedule_id: 'system-skill-evolution@personal' }),
      schedule({ schedule_id: 'system-memory-curation@personal' }),
    ]
    await nextTick()

    const row = (label: string) => {
      const found = view.findAll('.automation-row').find(
        r => r.find('.job-title').text().includes(label),
      )
      if (!found) throw new Error(`no "${label}" row rendered`)
      return found
    }

    expect(row('Skill reflection').find('.btn-run').exists()).toBe(false)
    expect(row('Memory proposals').find('.btn-run').text()).toBe('Run now')
    expect(view.findAll('.btn-run')).toHaveLength(1)
  })
})

describe('SettingsAutomation insights privacy toggle', () => {
  it('shows the persisted state and saves the inverse', async () => {
    const saveRoutines = vi.fn(() => Promise.resolve())
    const routines = { insights_enabled: true } as RoutineSettings
    const view = mountPanel({ routines, saveRoutines })
    const toggle = view.find('.insights-toggle')

    expect(toggle.text()).toBe('On')
    expect(toggle.attributes('role')).toBe('switch')
    expect(toggle.attributes('aria-checked')).toBe('true')
    await toggle.trigger('click')

    expect(saveRoutines).toHaveBeenCalledWith({ insights_enabled: false })
  })

  it('shows off with a plain-language privacy explanation', () => {
    const routines = { insights_enabled: false } as RoutineSettings
    const view = mountPanel({ routines })

    expect(view.find('.insights-toggle').text()).toBe('Off')
    expect(view.find('.insights-control').text()).toContain('Off stops the model pass')
  })

  it('shows and saves the trajectory capture state', async () => {
    const saveRoutines = vi.fn(() => Promise.resolve())
    const routines = { trajectories_enabled: true } as RoutineSettings
    const view = mountPanel({ routines, saveRoutines })
    const toggles = view.findAll('.insights-toggle')

    expect(toggles).toHaveLength(2)
    expect(toggles[1].text()).toBe('On')
    await toggles[1].trigger('click')

    expect(saveRoutines).toHaveBeenCalledWith({ trajectories_enabled: false })
  })
})
