// @vitest-environment jsdom
import { describe, it, expect, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { api } from '../../lib/api'
import SettingsTerminalPath from '../settings/SettingsTerminalPath.vue'

vi.mock('../../lib/api', () => ({ api: { post: vi.fn() } }))
const ready = { status: 'ready', detail: 'Ready', executable: '/bin/ciao', directory: '/bin', destination: '/home/me/.profile', revision: 'a'.repeat(64) }

describe('SettingsTerminalPath', () => {
  it('previews without mutation and applies exactly once on explicit click', async () => {
    vi.mocked(api.post).mockReset().mockResolvedValueOnce(ready).mockResolvedValueOnce({ status: 'applied', detail: 'Verified', mutation: 'written', warning: '' })
    const wrapper = mount(SettingsTerminalPath, { props: { target: 'ciao' } })
    await flushPromises()
    expect(api.post).toHaveBeenCalledTimes(1)
    expect(wrapper.text()).toContain('Terminal PATH · ciao')
    expect(wrapper.get('[role="status"]').attributes('aria-label')).toBe('ciao terminal PATH status')
    expect(wrapper.text()).toContain(`Executable: ${ready.executable}`)
    expect(wrapper.text()).toContain(`Directory: ${ready.directory}`)
    expect(wrapper.text()).toContain(`Destination: ${ready.destination}`)
    expect(wrapper.text()).toContain('Append this directory to your terminal PATH')
    expect(wrapper.text()).toContain('Open a new terminal')
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenLastCalledWith('/api/settings/terminal-path/apply', { target: 'ciao', revision: ready.revision })
    expect(wrapper.text()).toContain('Verified')
    wrapper.unmount()
  })

  it('shows already configured and refuses unsafe or remote targets', async () => {
    for (const response of [{ ...ready, status: 'already_configured', detail: 'Already configured' }, { ...ready, status: 'refused', detail: 'Collision refused' }]) {
      vi.mocked(api.post).mockReset().mockResolvedValueOnce(response)
      const wrapper = mount(SettingsTerminalPath, { props: { target: 'claude' } })
      await flushPromises()
      expect(wrapper.text()).toContain(response.detail)
      expect(wrapper.text()).not.toContain('Add to terminal PATH')
      wrapper.unmount()
    }
    vi.mocked(api.post).mockReset().mockRejectedValueOnce({ status: 403, payload: { error: 'not_on_host' } })
    const wrapper = mount(SettingsTerminalPath, { props: { target: 'ciao' } })
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('computer running Ciaobot')
    wrapper.unmount()
  })

  it('requires a fresh click after stale preview', async () => {
    vi.mocked(api.post).mockReset().mockResolvedValueOnce(ready).mockRejectedValueOnce({ status: 409, payload: { status: 'stale', detail: 'Stale', mutation: 'none', warning: '' } }).mockResolvedValueOnce({ ...ready, revision: 'b'.repeat(64) })
    const wrapper = mount(SettingsTerminalPath, { props: { target: 'ciao' } })
    await flushPromises()
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Stale')
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledTimes(3)
    expect(wrapper.text()).toContain('Add to terminal PATH')
    wrapper.unmount()
  })

  it('reports a written but unverified change honestly', async () => {
    vi.mocked(api.post).mockReset().mockResolvedValueOnce(ready).mockRejectedValueOnce({ status: 500, payload: { status: 'verification_failed', detail: 'Verification failed', mutation: 'written', warning: '' } })
    const wrapper = mount(SettingsTerminalPath, { props: { target: 'ciao' } })
    await flushPromises()
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('written but could not be verified')
    expect(wrapper.text()).not.toContain('Add to terminal PATH')
    wrapper.unmount()
  })

  it('clears_revision_before_apply_and_never_reuses_it_after_partial_unknown_or_network_failure', async () => {
    for (const failure of [{ status: 500, payload: { status: 'write_failed', detail: 'Failed', mutation: 'partial', warning: '' } }, { status: 500, payload: { status: 'write_failed', detail: 'Failed', mutation: 'unknown', warning: '' } }, new Error('network')]) {
      vi.mocked(api.post).mockReset().mockResolvedValueOnce(ready).mockRejectedValueOnce(failure).mockResolvedValueOnce({ ...ready, revision: 'b'.repeat(64) }).mockResolvedValueOnce({ status: 'applied', detail: 'Verified', mutation: 'written', warning: '' })
      const wrapper = mount(SettingsTerminalPath, { props: { target: 'ciao' } })
      await flushPromises()
      await wrapper.get('button').trigger('click')
      await flushPromises()
      expect(wrapper.get('[role="alert"]').text()).toContain('may have changed')
      await wrapper.get('button').trigger('click')
      await flushPromises()
      expect(api.post).toHaveBeenCalledTimes(3)
      await wrapper.get('button').trigger('click')
      expect(api.post).toHaveBeenLastCalledWith('/api/settings/terminal-path/apply', { target: 'ciao', revision: 'b'.repeat(64) })
      wrapper.unmount()
    }
  })

  it('stale_refresh_failure_never_restores_approval_and_announces_error', async () => {
    vi.mocked(api.post).mockReset().mockResolvedValueOnce(ready).mockRejectedValueOnce({ payload: { status: 'stale', detail: 'Stale', mutation: 'none', warning: '' } }).mockRejectedValueOnce(new Error('offline'))
    const wrapper = mount(SettingsTerminalPath, { props: { target: 'ciao' }, attachTo: document.body })
    await flushPromises()
    ;(wrapper.get('button').element as HTMLButtonElement).focus()
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(document.activeElement?.getAttribute('tabindex')).toBe('-1')
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('Could not check')
    expect(wrapper.get('[role="alert"]').text()).toContain('Stale')
    expect(wrapper.text()).not.toContain('Add to terminal PATH')
    wrapper.unmount()
  })

  it('preserves partial or unknown write warnings when a read-only refresh fails', async () => {
    for (const mutation of ['partial', 'unknown']) {
      vi.mocked(api.post).mockReset().mockResolvedValueOnce(ready)
        .mockRejectedValueOnce({ status: 500, payload: { status: 'write_failed', detail: 'Write failed', mutation, warning: 'Inspect the profile' } })
        .mockRejectedValueOnce(new Error('offline'))
      const wrapper = mount(SettingsTerminalPath, { props: { target: 'ciao' } })
      await flushPromises()
      await wrapper.get('button').trigger('click')
      await flushPromises()
      await wrapper.get('button').trigger('click')
      await flushPromises()
      const alert = wrapper.get('[role="alert"]').text()
      expect(alert).toContain('Write failed')
      expect(alert).toContain('may have changed')
      expect(alert).toContain('Could not check')
      expect(wrapper.text()).toContain('Inspect the profile')
      expect(wrapper.text()).not.toContain(ready.executable)
      expect(wrapper.text()).not.toContain('Add to terminal PATH')
      expect(api.post).toHaveBeenCalledTimes(3)
      expect(api.post).toHaveBeenLastCalledWith('/api/settings/terminal-path/preview', { target: 'ciao' })
      wrapper.unmount()
    }
  })

  it('renders_every_closed_http_failure_shape', async () => {
    for (const status of [400, 401, 403, 500]) {
      vi.mocked(api.post).mockReset().mockRejectedValueOnce({ status, payload: { error: status === 403 ? 'forbidden' : 'error' } })
      const wrapper = mount(SettingsTerminalPath, { props: { target: 'ciao' } })
      await flushPromises()
      expect(wrapper.find('[role="alert"]').exists()).toBe(true)
      expect(wrapper.text()).not.toContain('Add to terminal PATH')
      wrapper.unmount()
    }
    for (const mutation of ['none', 'partial', 'unknown']) {
      vi.mocked(api.post).mockReset().mockResolvedValueOnce(ready).mockRejectedValueOnce({ status: 500, payload: { status: 'write_failed', detail: 'Write failed', mutation, warning: '' } })
      const wrapper = mount(SettingsTerminalPath, { props: { target: 'ciao' } })
      await flushPromises()
      await wrapper.get('button').trigger('click')
      await flushPromises()
      expect(wrapper.get('[role="alert"]').text()).toContain(mutation === 'none' ? 'No change was written' : 'may have changed')
      wrapper.unmount()
    }
    vi.mocked(api.post).mockReset().mockResolvedValueOnce(ready).mockResolvedValueOnce({ status: 'applied', detail: 'Verified', mutation: 'written', warning: 'Notification failed' })
    const wrapper = mount(SettingsTerminalPath, { props: { target: 'ciao' } })
    await flushPromises()
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Notification failed')
    wrapper.unmount()
  })

  it('discards a pending revision synchronously and ignores stale target responses', async () => {
    let finish!: (value: unknown) => void
    const pending = new Promise(resolve => { finish = resolve })
    vi.mocked(api.post).mockReset().mockResolvedValueOnce(ready).mockReturnValueOnce(pending as never).mockResolvedValueOnce({ ...ready, revision: 'b'.repeat(64) })
    const wrapper = mount(SettingsTerminalPath, { props: { target: 'ciao' } })
    await flushPromises()
    const button = wrapper.get('button')
    await button.trigger('click')
    await button.trigger('click')
    expect(api.post).toHaveBeenCalledTimes(2)
    await wrapper.setProps({ target: 'claude' })
    await flushPromises()
    finish({ status: 'applied', detail: 'Old target', mutation: 'written', warning: '' })
    await flushPromises()
    expect(wrapper.text()).not.toContain('Old target')
    expect(wrapper.text()).toContain('Add to terminal PATH')
    wrapper.unmount()
  })

  it('never accepts a success-shaped rejected response as success', async () => {
    vi.mocked(api.post).mockReset().mockResolvedValueOnce(ready).mockRejectedValueOnce({ status: 500, payload: { status: 'applied', detail: 'Misleading', mutation: 'written', warning: '' } })
    const wrapper = mount(SettingsTerminalPath, { props: { target: 'ciao' } })
    await flushPromises()
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('may have changed')
    expect(wrapper.text()).not.toContain('Open a new terminal to use the change')
    wrapper.unmount()
  })

  it('never exposes rejected success claims or reuses approval after malformed success', async () => {
    for (const body of [
      { status: 'applied', detail: 'Successfully added to PATH', mutation: 'written', warning: '' },
      { status: 'already_configured', detail: 'Already configured successfully', mutation: 'none', warning: '' },
      { status: 'applied', detail: 'Successfully added to PATH', mutation: 'unknown', warning: '' },
      { detail: 'Successfully added to PATH' },
    ]) {
      vi.mocked(api.post).mockReset().mockResolvedValueOnce(ready).mockRejectedValueOnce({ status: 500, payload: body }).mockRejectedValueOnce(new Error('offline'))
      const wrapper = mount(SettingsTerminalPath, { props: { target: 'ciao' } })
      await flushPromises()
      await wrapper.get('button').trigger('click')
      await flushPromises()
      expect(wrapper.text()).not.toContain(body.detail)
      expect(wrapper.get('[role="alert"]').text()).toMatch(/unconfirmed|could not be confirmed/i)
      expect(wrapper.get('[role="alert"]').text()).toContain('may have changed')
      expect(wrapper.text()).not.toContain('Add to terminal PATH')
      await wrapper.get('button').trigger('click')
      await flushPromises()
      expect(api.post).toHaveBeenCalledTimes(3)
      expect(api.post).toHaveBeenLastCalledWith('/api/settings/terminal-path/preview', { target: 'ciao' })
      expect(wrapper.text()).not.toContain(body.detail)
      expect(wrapper.text()).not.toContain('Add to terminal PATH')
      wrapper.unmount()
    }
  })

  it('keeps focus elsewhere and ignores late preview and apply replies after unmount', async () => {
    let finish!: (value: unknown) => void
    const pending = new Promise(resolve => { finish = resolve })
    vi.mocked(api.post).mockReset().mockResolvedValueOnce(ready).mockReturnValueOnce(pending as never)
    const outside = document.createElement('button')
    document.body.append(outside)
    const wrapper = mount(SettingsTerminalPath, { props: { target: 'ciao' }, attachTo: document.body })
    await flushPromises()
    outside.focus()
    await wrapper.get('button').trigger('click')
    expect(document.activeElement).toBe(outside)
    wrapper.unmount()
    finish({ status: 'applied', detail: 'Late success', mutation: 'written', warning: '' })
    await flushPromises()
    expect(document.activeElement).toBe(outside)
    expect(document.body.textContent).not.toContain('Late success')
    outside.remove()
    let finishPreview!: (value: unknown) => void
    vi.mocked(api.post).mockReset().mockReturnValueOnce(new Promise(resolve => { finishPreview = resolve }) as never)
    const waiting = mount(SettingsTerminalPath, { props: { target: 'ciao' }, attachTo: document.body })
    expect(waiting.get('[role="status"]').attributes('aria-live')).toBe('polite')
    waiting.unmount()
    finishPreview(ready)
    await flushPromises()
    expect(document.body.textContent).not.toContain('Add to terminal PATH')
  })

  it('does not pull focus back when the user moves it during the approval transition', async () => {
    vi.mocked(api.post).mockReset().mockResolvedValueOnce(ready).mockReturnValueOnce(new Promise(() => {}) as never)
    const outside = document.createElement('button')
    document.body.append(outside)
    const wrapper = mount(SettingsTerminalPath, { props: { target: 'ciao' }, attachTo: document.body })
    await flushPromises()
    const button = wrapper.get('button').element as HTMLButtonElement
    button.focus()
    button.click()
    outside.focus()
    await wrapper.vm.$nextTick()
    expect(document.activeElement).toBe(outside)
    wrapper.unmount()
    outside.remove()
  })

  it('rejects invalid revisions and malformed or contradictory responses', async () => {
    for (const data of [{ ...ready, revision: '' }, { ...ready, revision: 'invalid' }, { ...ready, directory: '' }, { ...ready, status: 'invented' }, null]) {
      vi.mocked(api.post).mockReset().mockResolvedValueOnce(data)
      const wrapper = mount(SettingsTerminalPath, { props: { target: 'ciao' } })
      await flushPromises()
      expect(wrapper.find('[role="alert"]').exists()).toBe(true)
      expect(wrapper.text()).not.toContain('Add to terminal PATH')
      wrapper.unmount()
    }
    vi.mocked(api.post).mockReset().mockResolvedValueOnce(ready).mockResolvedValueOnce({ status: 'applied', detail: 'Successfully added to PATH', mutation: 'unknown', warning: '' }).mockRejectedValueOnce(new Error('offline'))
    const wrapper = mount(SettingsTerminalPath, { props: { target: 'ciao' } })
    await flushPromises()
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('may have changed')
    expect(wrapper.text()).not.toContain('Successfully added to PATH')
    expect(wrapper.text()).not.toContain('Add to terminal PATH')
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledTimes(3)
    expect(api.post).toHaveBeenLastCalledWith('/api/settings/terminal-path/preview', { target: 'ciao' })
    expect(wrapper.text()).not.toContain('Successfully added to PATH')
    expect(wrapper.text()).not.toContain('Add to terminal PATH')
    wrapper.unmount()
  })

  it('renders closed apply outcomes including refusal, already configured and warning', async () => {
    for (const item of [
      { status: 'already_configured', detail: 'Already configured', mutation: 'none', warning: '', rejected: false },
      { status: 'refused', detail: 'Markers malformed', mutation: 'none', warning: 'Inspect markers', rejected: true },
      { status: 'stale', detail: 'Stale approval', mutation: 'none', warning: '', rejected: true },
      { status: 'verification_failed', detail: 'Not verified', mutation: 'written', warning: 'Inspect PATH', rejected: true },
    ]) {
      const { rejected, ...body } = item
      vi.mocked(api.post).mockReset().mockResolvedValueOnce(ready)
      if (rejected) vi.mocked(api.post).mockRejectedValueOnce({ status: item.status === 'verification_failed' ? 500 : 409, payload: body })
      else vi.mocked(api.post).mockResolvedValueOnce(body)
      const wrapper = mount(SettingsTerminalPath, { props: { target: 'opencode' } })
      await flushPromises()
      await wrapper.get('button').trigger('click')
      await flushPromises()
      expect(wrapper.text()).toContain(item.detail)
      if (item.warning) expect(wrapper.text()).toContain(item.warning)
      expect(wrapper.find(rejected ? '[role="alert"]' : '[role="status"]').exists()).toBe(true)
      expect(wrapper.text()).not.toContain('Add to terminal PATH')
      expect(wrapper.text().includes('Check again')).toBe(rejected)
      wrapper.unmount()
    }
  })

  it('reports every rejected apply HTTP boundary without claiming success', async () => {
    for (const failure of [
      { status: 400, payload: { error: 'invalid_request' } },
      { status: 401, payload: { error: 'unauthorized' } },
      { status: 403, payload: { error: 'not_on_host' } },
      { status: 403, payload: { error: 'forbidden' } },
      { status: 500, payload: { error: 'internal_error' } },
    ]) {
      vi.mocked(api.post).mockReset().mockResolvedValueOnce(ready).mockRejectedValueOnce(failure)
      const wrapper = mount(SettingsTerminalPath, { props: { target: 'ciao' } })
      await flushPromises()
      await wrapper.get('button').trigger('click')
      await flushPromises()
      expect(wrapper.get('[role="alert"]').text()).toContain(failure.payload.error === 'not_on_host' ? 'computer running Ciaobot' : failure.payload.error)
      expect(wrapper.text()).toContain('may have changed')
      expect(wrapper.text()).not.toContain('Add to terminal PATH')
      expect(wrapper.text()).toContain('Check again')
      wrapper.unmount()
    }
  })

  it('removes the approval synchronously before the pending write can be activated twice', async () => {
    let resolve!: (value: unknown) => void
    vi.mocked(api.post).mockReset().mockResolvedValueOnce(ready).mockReturnValueOnce(new Promise(r => { resolve = r }) as never)
    const wrapper = mount(SettingsTerminalPath, { props: { target: 'ciao' } })
    await flushPromises()
    const button = wrapper.get('button').element as HTMLButtonElement
    button.click()
    button.click()
    expect(api.post).toHaveBeenCalledTimes(2)
    await wrapper.vm.$nextTick()
    expect(wrapper.get('[role="status"]').text()).toContain('Applying')
    expect(wrapper.find('button').exists()).toBe(false)
    resolve({ status: 'applied', detail: 'Verified', mutation: 'written', warning: '' })
    await flushPromises()
    wrapper.unmount()
  })

  it('ignores a late preview for the previous target', async () => {
    let resolve!: (value: unknown) => void
    vi.mocked(api.post).mockReset().mockReturnValueOnce(new Promise(r => { resolve = r }) as never).mockResolvedValueOnce({ ...ready, detail: 'New target', executable: '/bin/claude' })
    const wrapper = mount(SettingsTerminalPath, { props: { target: 'ciao' } })
    await wrapper.setProps({ target: 'claude' })
    await flushPromises()
    resolve({ ...ready, detail: 'Old target' })
    await flushPromises()
    expect(wrapper.text()).toContain('New target')
    expect(wrapper.text()).not.toContain('Old target')
    expect(wrapper.text()).toContain('/bin/claude')
    wrapper.unmount()
  })
})
