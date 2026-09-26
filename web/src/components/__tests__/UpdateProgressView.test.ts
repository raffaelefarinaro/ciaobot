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

function percent(wrapper: ReturnType<typeof mount>): number {
  return parseInt(wrapper.find('.update-progress-pct').text(), 10)
}

/** One row by name, so an ordering test names the phase it is about. */
function rowFor(list: string[][], name: string): string[] | undefined {
  return list.find(row => row[0] === name)
}

/** The rows the log calls live, by name. */
function liveRows(wrapper: ReturnType<typeof mount>): string[] {
  return wrapper.findAll('.update-progress-log-row.is-in_progress')
    .map(row => row.find('.update-progress-log-name').text())
}

/**
 * `ciao/engine_update.py:PHASES` in the order the coordinator runs them, split
 * the way the overlay lists them. Listed literally so a phase added on the
 * Python side has to be placed here, where leaving it out fails the
 * monotonicity test below rather than showing up as a row that jumps backwards.
 */
const FORWARD_TRACK = [
  'resolving',
  'downloading',
  'verifying',
  'staging',
  'staged',
  'draining',
  'applying',
  'stopping',
  'swapping',
  'starting',
  'verifying_start',
  'applied',
]
const ROLLBACK_TRACK = ['rolling_back', 'rolled_back', 'rollback_failed']
const TRACK = [...FORWARD_TRACK, ...ROLLBACK_TRACK]

describe('with a real phase', () => {
  it('drives the rows off the record instead of a timer', async () => {
    vi.useFakeTimers()
    const wrapper = mount(UpdateProgressView, { props: { version: '0.20.0', phase: 'swapping' } })
    await nextTick()

    // One row per phase, in the order the engine runs them: the swap is the
    // ninth, so the eight before it are done, it is live, and the three after it
    // have not started.
    expect(rows(wrapper)).toEqual([
      ['looking for the latest release', 'ok'],
      ['downloading the release', 'ok'],
      ['checking the signed release', 'ok'],
      ['preparing the update', 'ok'],
      ['ready to apply', 'ok'],
      ['waiting for active chats to finish', 'ok'],
      ['applying the update', 'ok'],
      ['stopping the engine', 'ok'],
      ['swapping in the new version', '…'],
      ['starting the updated engine', 'wait'],
      ['checking the updated engine is answering', 'wait'],
      ['update applied', 'wait'],
    ])
    // A run nobody is performing must not animate itself forward.
    vi.advanceTimersByTime(5000)
    await nextTick()
    expect(liveRows(wrapper)).toEqual(['swapping in the new version'])
    wrapper.unmount()
  })

  it('never moves the bar backwards as the record advances', async () => {
    const wrapper = mount(UpdateProgressView, { props: { version: '0.20.0', phase: 'resolving' } })
    const seen: number[] = []
    for (const phase of TRACK) {
      await wrapper.setProps({ phase })
      seen.push(percent(wrapper))
    }
    // `downloading` then `verifying` is the ordinary transition that used to
    // drop 50% → 33%: every phase is at least as far along as the one before it.
    expect(seen).toEqual([...seen].sort((a, b) => a - b))
    expect(seen[0]).toBe(0)
    expect(seen[seen.length - 1]).toBe(100)
    wrapper.unmount()
  })

  it('keeps a finished download done when the record moves to verifying', async () => {
    const wrapper = mount(UpdateProgressView, { props: { version: '0.20.0', phase: 'downloading' } })
    const downloading = percent(wrapper)
    await wrapper.setProps({ phase: 'verifying' })

    const list = rows(wrapper)
    expect(rowFor(list, 'downloading the release')).toEqual(['downloading the release', 'ok'])
    expect(rowFor(list, 'checking the signed release')).toEqual(['checking the signed release', '…'])
    expect(percent(wrapper)).toBeGreaterThan(downloading)
    wrapper.unmount()
  })

  it('names a rollback as a rollback, not as a row it happens to land on', () => {
    const wrapper = mount(UpdateProgressView, { props: { version: '0.20.0', phase: 'rolling_back' } })
    const list = rows(wrapper)
    // Live, and named for what it is. `rolling_back` is in flight in the
    // coordinator, so it gets a row of its own rather than row zero and a
    // version check it is not doing.
    expect(rowFor(list, 'rolling back to the previous version'))
      .toEqual(['rolling back to the previous version', '…'])
    expect(wrapper.find('.update-progress-phase').text()).toBe('Rolling back to the previous version')
    expect(wrapper.text()).not.toContain('checking the current Ciaobot version')
    wrapper.unmount()
  })

  it('lists the rollback track only once the record names a rollback', () => {
    const forward = mount(UpdateProgressView, { props: { phase: 'swapping' } })
    expect(forward.text()).not.toContain('rolling back')
    forward.unmount()

    const rollback = mount(UpdateProgressView, { props: { phase: 'rolling_back' } })
    expect(rollback.text()).toContain('rolling back to the previous version')
    rollback.unmount()
  })

  it('says which phase the record is in', () => {
    const wrapper = mount(UpdateProgressView, { props: { phase: 'verifying_start' } })
    expect(wrapper.find('.update-progress-phase').text()).toBe('Checking the updated engine is answering')
    wrapper.unmount()
  })

  it('is a live status region a caller can put focus in, because a record is behind it', () => {
    const wrapper = mount(UpdateProgressView, { props: { phase: 'swapping' } })
    const content = wrapper.get('.update-progress-content')
    expect(content.attributes('role')).toBe('status')
    expect(content.attributes('aria-live')).toBe('polite')
    // Not in the tab order: nothing in a log is actionable, so Tab belongs to
    // whatever holds focus while the overlay is up.
    expect(content.attributes('tabindex')).toBe('-1')
    // A real job lists more rows than the boot screen and must be able to scroll.
    expect(content.classes()).toContain('update-progress-content--job')
    wrapper.unmount()
  })

  it('still counts as a job for a phase this build has no name for', () => {
    const wrapper = mount(UpdateProgressView, { props: { phase: 'rewarming' } })
    // An unknown phase shows no phase line, but the record exists and its
    // progress is what is on screen, so the job affordances still apply.
    expect(wrapper.find('.update-progress-phase').exists()).toBe(false)
    expect(wrapper.get('.update-progress-content').attributes('role')).toBe('status')
    wrapper.unmount()
  })

  it('marks everything done and ready on applied', () => {
    const wrapper = mount(UpdateProgressView, { props: { version: '0.20.0', phase: 'applied' } })
    // `applied` is the one phase the run is not in any more: every row is behind it.
    expect(rows(wrapper).map(r => r[1])).toEqual(Array(FORWARD_TRACK.length).fill('ok'))
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
    // It is the rollback's own row, not the last one in the list.
    const failedRow = wrapper.findAll('.update-progress-log-row')
      .find(row => row.classes().includes('is-error'))
    expect(failedRow?.find('.update-progress-log-name').text()).toBe('rolled back to the previous version')
    expect(failedRow?.find('.update-progress-log-status').text()).toBe('failed')
    wrapper.unmount()
  })

  it('falls back to a sentence when the record names no reason', () => {
    const wrapper = mount(UpdateProgressView, { props: { phase: 'failed' } })
    expect(wrapper.find('.update-progress-failed').text().length).toBeGreaterThan('[failed] '.length)
    expect(wrapper.text()).not.toContain('ciaobot is up to date')
    wrapper.unmount()
  })

  it('claims no completed row for a record that says only that it failed', () => {
    const wrapper = mount(UpdateProgressView, { props: { phase: 'failed' } })
    // `failed` names no phase it broke at, so nothing behind it may be shown as
    // done and the bar stays where the run started.
    expect(rows(wrapper)).toEqual([['the update failed', 'failed']])
    expect(percent(wrapper)).toBe(0)
    wrapper.unmount()
  })

  it('names the failure in the bar, rather than calling it progress', () => {
    const wrapper = mount(UpdateProgressView, {
      props: { phase: 'rolled_back', error: 'the new engine never answered' },
    })
    // A bar labelled "Updating 100 percent" beside a `[failed]` footer is a
    // progress indicator claiming work in flight for a run that has stopped.
    const label = wrapper.get('.update-progress').attributes('aria-label') ?? ''
    expect(label).toContain('the new engine never answered')
    expect(label).toContain('Rolled back to the previous version')
    expect(label).not.toMatch(/^Updating \d+ percent$/)
    wrapper.unmount()
  })

  it('still labels a running bar with its percentage', () => {
    const wrapper = mount(UpdateProgressView, { props: { version: '0.20.0', phase: 'swapping' } })
    expect(wrapper.get('.update-progress').attributes('aria-label')).toBe(`Updating ${percent(wrapper)} percent`)
    wrapper.unmount()
  })
})

describe('inline (a run that is not a takeover)', () => {
  it('drops the full-window layer and keeps the record', () => {
    const wrapper = mount(UpdateProgressView, {
      props: { version: '0.20.0', phase: 'downloading', inline: true },
    })
    // The class is the whole difference: the rows, the bar, the phase line and
    // the footer are the overlay's, so a staged run reads the same either way.
    expect(wrapper.get('.update-progress-overlay').classes()).toContain('update-progress-overlay--inline')
    expect(wrapper.find('.update-progress-phase').text()).toBe('Downloading the release')
    expect(rowFor(rows(wrapper), 'downloading the release'))
      .toEqual(['downloading the release', '…'])
    expect(wrapper.get('.update-progress-content').attributes('role')).toBe('status')
    wrapper.unmount()
  })

  it('is not a takeover: the card holds it, not the window', () => {
    const wrapper = mount(UpdateProgressView, { props: { phase: 'downloading', inline: true } })
    // The one thing an inline run must not be is a claim on the window: this is
    // a child element the caller places, so there is no modal layer, no inert
    // boundary and nothing in the document for one to be applied to.
    const overlay = wrapper.get('.update-progress-overlay').element as HTMLElement
    expect(overlay.parentElement?.tagName).not.toBe('BODY')
    expect(overlay.closest('.engine-update-overlay')).toBeNull()
    expect(Array.from(document.body.children).every(node => !(node instanceof HTMLElement) || !node.inert)).toBe(true)
    wrapper.unmount()
  })

  it('stops the boot timer when a record arrives after the mount', async () => {
    vi.useFakeTimers()
    // The boot path passes no phase, so the animation starts; a record can still
    // land afterwards, and then the rows belong to it. A timer left running under
    // a record-driven row set would move rows the record is the only witness to.
    const wrapper = mount(UpdateProgressView, { props: { version: '0.20.0' } })
    await nextTick()
    expect(vi.getTimerCount()).toBeGreaterThan(0)

    await wrapper.setProps({ phase: 'downloading' })
    expect(vi.getTimerCount()).toBe(0)

    vi.advanceTimersByTime(900 * 3)
    await nextTick()
    expect(liveRows(wrapper)).toEqual(['downloading the release'])
    wrapper.unmount()
  })

  it('leaves the boot screen exactly as it was', () => {
    const wrapper = mount(UpdateProgressView, { props: { version: '0.20.0' } })
    // `inline` is the caller's choice, and absent must mean the boot screen is
    // what it has always been: the full-window layer, with no inline class.
    expect(wrapper.get('.update-progress-overlay').classes()).toEqual(['update-progress-overlay'])
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

  it('keeps the six boot stages and the real job apart', () => {
    const wrapper = mount(UpdateProgressView, { props: { version: '0.20.0' } })
    expect(rows(wrapper).map(r => r[0])).toEqual([
      'checking the current Ciaobot version',
      'preparing the local engine',
      'checking the signed release',
      'downloading the next hello',
      'installing the updated runtime',
      'getting ready to restart',
    ])
    wrapper.unmount()
  })

  it('has no phase line and no failure to show', () => {
    const wrapper = mount(UpdateProgressView, { props: { finishing: true } })
    expect(wrapper.find('.update-progress-phase').exists()).toBe(false)
    expect(wrapper.find('.update-progress-failed').exists()).toBe(false)
    expect(wrapper.find('.update-progress-ready').text()).toContain('ciaobot is up to date')
    wrapper.unmount()
  })

  it('is the same screen it always was, with no job to announce', () => {
    const wrapper = mount(UpdateProgressView, { props: { version: '0.20.0' } })
    const content = wrapper.get('.update-progress-content')
    // The job path's status region is a live region, a focus target and a
    // scrolling list. None of that belongs to an animation with no record
    // behind it, so the boot screen carries none of it.
    expect(content.attributes('role')).toBeUndefined()
    expect(content.attributes('aria-live')).toBeUndefined()
    expect(content.attributes('tabindex')).toBeUndefined()
    expect(content.classes()).not.toContain('update-progress-content--job')
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
