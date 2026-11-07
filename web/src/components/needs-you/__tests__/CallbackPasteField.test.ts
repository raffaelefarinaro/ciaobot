// @vitest-environment jsdom

/**
 * `components/needs-you/CallbackPasteField.vue`: the shared field where a
 * sign-in that ends on an unreachable loopback page is finished by pasting its
 * address. The field clears after every try, is never stored, and shows the
 * engine's refusal in plain words.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import CallbackPasteField from '../CallbackPasteField.vue'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())

vi.mock('../../../lib/api', () => ({
  api: { get: apiGet, post: apiPost, patch: vi.fn(), del: vi.fn(), postForm: vi.fn() },
}))

const CHAT_ID = 'chat-1'
const URL_OK = 'http://localhost:62471/callback?code=a&state=s'

let wrapper: VueWrapper | null = null

function mountField() {
  const pinia = createPinia()
  setActivePinia(pinia)
  wrapper = mount(CallbackPasteField, {
    props: { chatId: CHAT_ID, actionId: 'a1', hint: 'Paste it here' },
    attachTo: document.body,
    global: { plugins: [pinia] },
  })
  return wrapper
}

function input(w: VueWrapper): HTMLInputElement {
  return w.get('input').element as HTMLInputElement
}

async function paste(w: VueWrapper, value: string) {
  w.get('input').setValue(value)
  w.get('form').trigger('submit')
  await flushPromises()
}

beforeEach(() => {
  apiGet.mockReset()
  apiPost.mockReset()
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  vi.restoreAllMocks()
})

describe('CallbackPasteField', () => {
  it('labels the field with the hint', () => {
    const w = mountField()
    const label = w.get('label')
    expect(label.text()).toBe('Paste it here')
    expect(label.attributes('for')).toBe(w.get('input').attributes('id'))
  })

  it('submits, clears and emits sent', async () => {
    apiPost.mockResolvedValueOnce({
      action: {
        id: 'a1', kind: 'mcp_auth', origin: 'engine', state: 'waiting',
        params: { server: 's', provider: 'claude', url: 'https://x', callback_used: '1' },
        error: '', created_at: 1,
      },
    })
    const w = mountField()
    await paste(w, URL_OK)
    expect(apiPost).toHaveBeenCalledWith('/api/chats/chat-1/actions/a1/callback', { url: URL_OK })
    expect(input(w).value).toBe('')
    expect(w.emitted('sent')).toHaveLength(1)
  })

  it('shows the mapped refusal, clears the input and does not emit sent', async () => {
    apiPost.mockRejectedValueOnce(Object.assign(new Error('state_mismatch'), {
      status: 400,
      payload: { error: 'state_mismatch', action: null },
    }))
    const w = mountField()
    await paste(w, URL_OK)
    expect(w.get('[role=alert]').text()).toContain('different sign-in')
    expect(input(w).value).toBe('')
    expect(w.emitted('sent')).toBeUndefined()
  })

  it('says a failed send can be retried', async () => {
    apiPost.mockRejectedValueOnce(new Error('offline'))
    const w = mountField()
    await paste(w, URL_OK)
    expect(w.get('[role=alert]').text()).toContain('could not be sent')
  })

  it('keeps the button disabled until the text looks like a loopback address', async () => {
    const w = mountField()
    w.get('input').setValue('https://x.example/')
    await flushPromises()
    expect(w.get('button[type=submit]').attributes('disabled')).toBeDefined()
    w.get('form').trigger('submit')
    await flushPromises()
    expect(apiPost).not.toHaveBeenCalled()

    w.get('input').setValue(URL_OK)
    await flushPromises()
    expect(w.get('button[type=submit]').attributes('disabled')).toBeUndefined()
  })

  it('never stores the pasted address', async () => {
    apiPost.mockResolvedValueOnce({ action: null })
    const setItem = vi.spyOn(Storage.prototype, 'setItem')
    const w = mountField()
    await paste(w, URL_OK)
    for (const call of setItem.mock.calls) {
      expect(call.join(' ')).not.toContain('code=a')
    }
  })
})
