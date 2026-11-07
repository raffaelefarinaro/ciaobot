// @vitest-environment jsdom

/**
 * `components/needs-you/CommandBody.vue` — the body of a `command` card.
 *
 * The command is shown as literal text. Typing goes through the terminal
 * composable in paste mode for this chat, and the engine only hears the
 * `typed` and `ran` steps. Nothing on the body runs a command.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { ref } from 'vue'
import CommandBody from '../CommandBody.vue'
import type { ChatAction } from '../../../lib/types'

const run = vi.hoisted(() => vi.fn())
const busy = vi.hoisted(() => ({ value: false }))
const writeClipboard = vi.hoisted(() => vi.fn())
const startChatAction = vi.hoisted(() => vi.fn())

vi.mock('../../../composables/useRunInTerminal', () => ({
  useRunInTerminal: () => ({ run, busy: ref(busy.value) }),
}))
vi.mock('../../../stores/projects', () => ({
  useProjectStore: () => ({ startChatAction }),
}))
vi.mock('../../../lib/codeCopy', () => ({ writeClipboard }))

function action(over: Partial<ChatAction> = {}): ChatAction {
  return {
    id: 'a1',
    kind: 'command',
    origin: 'agent',
    state: 'open',
    params: { command: 'echo needs-you' },
    error: '',
    created_at: 1,
    ...over,
  }
}

function button(wrapper: ReturnType<typeof mount>, label: string) {
  const match = wrapper.findAll('button').find(b => b.text() === label)
  if (!match) throw new Error(`no button named ${label}`)
  return match
}

beforeEach(() => {
  run.mockReset().mockResolvedValue('typed')
  busy.value = false
  writeClipboard.mockReset().mockResolvedValue(true)
  startChatAction.mockReset().mockResolvedValue(null)
})

describe('CommandBody', () => {
  it('shows the command as literal text', () => {
    const wrapper = mount(CommandBody, {
      props: { chatId: 'c1', action: action({ params: { command: '<b>x</b> [l](https://e.invalid)' } }) },
    })
    expect(wrapper.find('pre code').text()).toBe('<b>x</b> [l](https://e.invalid)')
    expect(wrapper.find('b').exists()).toBe(false)
    expect(wrapper.find('a').exists()).toBe(false)
  })

  it('typing calls run in paste mode for this chat, then records typed', async () => {
    const wrapper = mount(CommandBody, { props: { chatId: 'c1', action: action() } })
    await button(wrapper, 'Type it in a terminal').trigger('click')
    await flushPromises()
    expect(run).toHaveBeenCalledWith('echo needs-you', { chatId: 'c1', paste: true })
    expect(startChatAction).toHaveBeenCalledWith('c1', 'a1', { step: 'typed' })
  })

  it('a multi-line refusal explains itself and records nothing', async () => {
    run.mockResolvedValue('copied_multiline')
    const wrapper = mount(CommandBody, { props: { chatId: 'c1', action: action() } })
    await button(wrapper, 'Type it in a terminal').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('copied instead of typed')
    expect(startChatAction).not.toHaveBeenCalled()
    expect(button(wrapper, 'I ran it').exists()).toBe(true)
  })

  it('working offers I ran it', async () => {
    const wrapper = mount(CommandBody, { props: { chatId: 'c1', action: action({ state: 'working' }) } })
    await button(wrapper, 'I ran it').trigger('click')
    await flushPromises()
    expect(startChatAction).toHaveBeenCalledWith('c1', 'a1', { step: 'ran' })
  })

  it('done renders no body buttons', () => {
    const wrapper = mount(CommandBody, { props: { chatId: 'c1', action: action({ state: 'done' }) } })
    expect(wrapper.find('button').exists()).toBe(false)
  })

  it('a late answer for a replaced action changes nothing', async () => {
    const pending = { resolve: (_value: string) => {} }
    run.mockReturnValue(new Promise<string>((resolve) => { pending.resolve = resolve }))
    const wrapper = mount(CommandBody, { props: { chatId: 'c1', action: action() } })
    await button(wrapper, 'Type it in a terminal').trigger('click')
    await wrapper.setProps({ action: action({ id: 'a2' }) })
    pending.resolve('typed')
    await flushPromises()
    expect(startChatAction).not.toHaveBeenCalled()
  })
})
