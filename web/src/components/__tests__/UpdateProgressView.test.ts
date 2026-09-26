// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import UpdateProgressView from '../UpdateProgressView.vue'

afterEach(() => vi.useRealTimers())

/** The rows in the order the overlay lists them, as [name, status]. */
function rows(wrapper: ReturnType<typeof mount>) {
  return wrapper.findAll('.update-progress-log-row').map(row => [
    row.find('.update-progress-log-name').text(),
    row.find('.update-progress-log-status').text(),
  ])
}

describe('with a real phase', () => {
  it('drives the rows off the record instead of a timer', async () => {
    vi.useFakeTimers()
    const wrapper = mount(UpdateProgressView, { props: { version: '0.20.0', phase: 'swapping' } })
    await nextTick()

    expect(rows(wrapper)).toEqual([
      ['checking the current Ciaobot version', 'ok'],
      ['preparing the local engine', 'ok'],
      ['checking the signed release', 'ok'],
      ['downloading the next hello', 'ok'],
      ['installing the updated runtime', 'ok'],
      ['getting ready to restart', '…'],
    ])
    // A run nobody is performing must not animate itself forward.
    vi.advanceTimersByTime(5000)
    await nextTick()
    expect(wrapper.find('.update-progress-log-row:last-child').classes()).toContain('is-in_progress')
    wrapper.unmount()
  })

  it('says which phase the record is in', () => {
    const wrapper = mount(UpdateProgressView, { props: { phase: 'verifying_start' } })
    expect(wrapper.find('.update-progress-phase').text()).toBe('Checking the updated engine is answering')
    wrapper.unmount()
  })

  it('marks everything done and ready on applied', () => {
    const wrapper = mount(UpdateProgressView, { props: { version: '0.20.0', phase: 'applied' } })
    expect(rows(wrapper).map(r => r[1])).toEqual(['ok', 'ok', 'ok', 'ok', 'ok', 'ok'])
    expect(wrapper.find('.update-progress-ready').text()).toContain('ciaobot is up to date')
    expect(wrapper.text()).not.toContain('[failed]')
    wrapper.unmount()
  })

  it('renders a failure with the record\'s own reason, and no success line', () => {
    const wrapper = mount(UpdateProgressView, {
      props: { phase: 'rolled_back', error: 'new engine never answered' },
    })
    expect(wrapper.find('.update-progress-failed').text()).toBe('[failed] new engine never answered')
    expect(wrapper.text()).not.toContain('ciaobot is up to date')
    // The word is on the row too: the red is a second signal, not the only one.
    expect(wrapper.find('.update-progress-log-row:last-child').classes()).toContain('is-error')
    expect(wrapper.find('.update-progress-log-row:last-child').text()).toContain('failed')
    wrapper.unmount()
  })

  it('falls back to a sentence when the record names no reason', () => {
    const wrapper = mount(UpdateProgressView, { props: { phase: 'failed' } })
    expect(wrapper.find('.update-progress-failed').text().length).toBeGreaterThan('[failed] '.length)
    expect(wrapper.text()).not.toContain('ciaobot is up to date')
    wrapper.unmount()
  })
})

describe('without a phase (the boot animation)', () => {
  it('advances on its own timer, exactly as it always did', async () => {
    vi.useFakeTimers()
    const wrapper = mount(UpdateProgressView, { props: { version: '0.20.0' } })
    await nextTick()
    // onMounted advances once, so the first row is done and the second is live.
    expect(rows(wrapper).map(r => r[1])).toEqual(['ok', '…', 'wait', 'wait', 'wait', 'wait'])

    vi.advanceTimersByTime(900)
    await nextTick()
    expect(rows(wrapper).map(r => r[1])).toEqual(['ok', 'ok', '…', 'wait', 'wait', 'wait'])

    vi.advanceTimersByTime(900 * 5)
    await nextTick()
    expect(rows(wrapper).map(r => r[1])).toEqual(['ok', 'ok', 'ok', 'ok', 'ok', 'ok'])
    expect(wrapper.find('.update-progress-foot').text()).toContain('updating')
    wrapper.unmount()
  })

  it('has no phase line and no failure to show', () => {
    const wrapper = mount(UpdateProgressView, { props: { finishing: true } })
    expect(wrapper.find('.update-progress-phase').exists()).toBe(false)
    expect(wrapper.find('.update-progress-failed').exists()).toBe(false)
    expect(wrapper.find('.update-progress-ready').text()).toContain('ciaobot is up to date')
    wrapper.unmount()
  })

  it('stops the timer on unmount', async () => {
    vi.useFakeTimers()
    const wrapper = mount(UpdateProgressView)
    await nextTick()
    const pending = vi.getTimerCount()
    expect(pending).toBeGreaterThan(0)
    wrapper.unmount()
    expect(vi.getTimerCount()).toBe(0)
  })
})
