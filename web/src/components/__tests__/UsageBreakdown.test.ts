// @vitest-environment jsdom

import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import UsageBreakdown from '../UsageBreakdown.vue'
import { normalizeUsageStats } from '../../lib/usageStats'

const DAYS = Array.from({ length: 182 }, (_, i) => {
  const d = new Date(Date.UTC(2026, 10, 9 - (181 - i)))
  return { date: d.toISOString().slice(0, 10), count: i % 5 }
})

const SAMPLE = {
  range_days: 30,
  daily_messages: DAYS,
  projects: [
    { project_id: 'alpha-general', name: 'General', messages: 412 },
    { project_id: 'alpha-notes', name: 'alpha-notes-a-very-long-unbreakable-project-identifier', messages: 260 },
    { project_id: 'beta-general', name: 'General', messages: 198 },
  ],
  memory_pipeline: { proposed: 179, accepted: 112, notes_updated: 62 },
  hours: [2, 1, 0, 0, 0, 0, 1, 4, 9, 14, 16, 12, 7, 11, 15, 13, 10, 8, 6, 9, 14, 17, 12, 6],
  models: [{ model: 'claude-opus-5-5', turns: 680 }, { model: 'claude-sonnet-5-5', turns: 220 }, { model: 'openai/gpt-5.6-luna', turns: 100 }],
}

const DAY_CELLS = '.usage-heat-cell:not(.usage-heat-cell--pad):not(.usage-legend-swatch)'

function mountBreakdown(stats: ReturnType<typeof normalizeUsageStats> | null, rangeDays: number) {
  return mount(UsageBreakdown, { props: { stats, rangeDays } })
}

describe('UsageBreakdown', () => {
  it('draws one labelled cell per day', () => {
    const wrapper = mountBreakdown(normalizeUsageStats(SAMPLE, 30), 30)

    const heat = wrapper.find('.usage-heat')
    expect(heat.attributes('role')).toBe('list')
    expect(heat.attributes('tabindex')).toBe('0')
    expect(heat.attributes('aria-label')).toBe('Messages per day, last 26 weeks')

    const days = wrapper.findAll(DAY_CELLS)
    expect(days).toHaveLength(182)
    const last = days[days.length - 1]
    expect(last.attributes('aria-label')).toBe('Mon 9 Nov 2026: 1 message')
    expect(last.attributes('title')).toBe('Mon 9 Nov 2026: 1 message')

    const pads = wrapper.findAll('.usage-heat-cell--pad')
    expect(pads.length).toBeGreaterThan(0)
    expect(pads.every(p => p.attributes('aria-hidden') === 'true')).toBe(true)

    expect(wrapper.findAll('.usage-heat-cell').every(c => ['0', '1', '2', '3', '4'].includes(c.attributes('data-level') ?? ''))).toBe(true)
  })

  it('shows a five-step legend in words', () => {
    const wrapper = mountBreakdown(normalizeUsageStats(SAMPLE, 30), 30)

    expect(wrapper.findAll('.usage-legend .usage-legend-swatch')).toHaveLength(5)
    const text = wrapper.find('.usage-legend').text()
    expect(text.startsWith('Less')).toBe(true)
    expect(text).toContain('More')
  })

  it('lists projects by messages', () => {
    const wrapper = mountBreakdown(normalizeUsageStats(SAMPLE, 30), 30)

    const names = wrapper.findAll('.usage-bar-name').map(n => n.text())
    expect(names.slice(0, 3)).toEqual(['General', 'alpha-notes-a-very-long-unbreakable-project-identifier', 'General'])
    expect((wrapper.find('.usage-bar-fill').element as HTMLElement).style.width).toBe('100%')
    expect(wrapper.text()).toContain('Messages sent, last 30 days.')
  })

  it('walks the memory pipeline in order', () => {
    const wrapper = mountBreakdown(normalizeUsageStats(SAMPLE, 30), 30)

    expect(wrapper.findAll('.usage-pipeline-step').map(s => s.text())).toEqual(['Proposed179', 'Accepted112', 'Vault notes updated62'])
  })

  it('plots 24 hours and names the busiest', () => {
    const wrapper = mountBreakdown(normalizeUsageStats(SAMPLE, 30), 30)

    expect(wrapper.findAll('.usage-hour')).toHaveLength(24)
    expect(wrapper.find('.usage-hours').attributes('role')).toBe('img')
    expect(wrapper.find('.usage-hours').attributes('aria-label')).toBe('Messages by hour of day, busiest at 21:00')
    expect(wrapper.findAll('.usage-hour')[9].attributes('title')).toBe('09:00 · 14 messages')
  })

  it('shares models by turns', () => {
    const wrapper = mountBreakdown(normalizeUsageStats(SAMPLE, 30), 30)

    const models = wrapper.find('[aria-labelledby="usage-models-title"]')
    expect(models.findAll('.usage-bar-value').map(v => v.text())).toEqual(['68%', '22%', '10%'])
    expect(models.find('.usage-bar-name').attributes('title')).toBe('claude-opus-5-5')
  })

  it('says so when there is nothing to show', () => {
    const wrapper = mountBreakdown(null, 7)

    expect(wrapper.find('.usage-heat').exists()).toBe(false)
    expect(wrapper.text()).toContain('No activity recorded yet.')
    expect(wrapper.text()).toContain('No project activity in this period.')
    expect(wrapper.text()).toContain('No model turns in this period.')
    expect(wrapper.findAll('.usage-pipeline-step strong').map(s => s.text())).toEqual(['—', '—', '—'])
    expect(wrapper.find('.usage-hours').attributes('aria-label')).toBe('Messages by hour of day, none in this period')
    expect(wrapper.text()).toContain('Messages sent, last 7 days.')
  })
})
