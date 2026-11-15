// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it } from 'vitest'
import DialogStoryHost from '../../stories/DialogStoryHost.vue'
import { pendingConfirm } from '../../lib/confirm'
import { pendingPrompt } from '../../lib/prompt'
import confirmMeta from '../../stories/ConfirmDialog.stories'
import promptMeta, { Disabled, Empty, Narrow, Populated, Prefilled, Whitespace } from '../../stories/PromptDialog.stories'

const wrappers: ReturnType<typeof mount>[] = []
function host(props: Partial<InstanceType<typeof DialogStoryHost>['$props']> = {}) {
  const wrapper = mount(DialogStoryHost, {
    attachTo: document.body,
    props: { kind: 'confirm', title: 'Archive', message: 'Archive this?', confirmLabel: 'Archive', ...props },
  })
  wrappers.push(wrapper)
  return wrapper
}
afterEach(() => {
  for (const wrapper of wrappers.splice(0).reverse()) wrapper.unmount()
  document.body.innerHTML = ''
})

describe('dialog story singleton lifecycle', () => {
  it('does not publish inline Docs hosts', () => {
    for (const meta of [confirmMeta, promptMeta]) {
      expect(meta.tags).toContain('!autodocs')
      expect(meta.parameters.docs.disable).toBe(true)
    }
  })

  it('opens after the real host mounts and reports production Enter behavior', async () => {
    const wrapper = host()
    await flushPromises()
    const cancel = document.querySelector<HTMLButtonElement>('.confirm-action--cancel')!
    expect(document.activeElement).toBe(cancel)
    cancel.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }))
    await flushPromises()
    expect(wrapper.get('[role="status"]').text()).toBe('Result: true')
    expect(pendingConfirm.value).toBeNull()
  })

  it.each([Empty, Whitespace, Prefilled, Populated, Disabled, Narrow])('seeds prompt values only after mounting its watcher', async (story) => {
    host({ ...promptMeta.args, ...story.args })
    await flushPromises()
    const value = story.args?.value ?? promptMeta.args.value
    const input = document.querySelector<HTMLInputElement>('.prompt-input')!
    expect(input.value).toBe(value)
    expect(document.activeElement).toBe(input)
    expect(input.selectionEnd).toBe(value.length)
    expect(document.querySelector<HTMLButtonElement>('.prompt-action--primary')!.disabled).toBe(!value.trim())
  })

  it.each(['confirm', 'prompt'] as const)('cancels pending %s requests on teardown and allows a fresh host', async (kind) => {
    const first = host({ kind })
    await flushPromises()
    const request = kind === 'confirm' ? pendingConfirm.value : pendingPrompt.value
    expect(request).not.toBeNull()
    first.unmount()
    wrappers.splice(wrappers.indexOf(first), 1)
    expect(pendingConfirm.value).toBeNull()
    expect(pendingPrompt.value).toBeNull()
    host({ kind })
    await flushPromises()
    expect(document.querySelectorAll('[role="dialog"]')).toHaveLength(1)
  })

  it('rejects a simultaneous host without cancelling the first request', async () => {
    host()
    await flushPromises()
    const first = pendingConfirm.value
    // Vue catches lifecycle errors with the configured handler.
    const errors: unknown[] = []
    const second = mount(DialogStoryHost, {
      props: { kind: 'prompt', title: 'Name', message: 'Name', confirmLabel: 'Create' },
      global: { config: { errorHandler: (error) => { errors.push(error) } } },
    })
    await flushPromises()
    second.unmount()
    expect(errors[0]).toEqual(expect.objectContaining({ message: expect.stringContaining('one host per canvas') }))
    expect(pendingConfirm.value).toBe(first)
    expect(pendingPrompt.value).toBeNull()
    expect(document.querySelectorAll('[role="dialog"]')).toHaveLength(1)
  })
})
