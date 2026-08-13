// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import CommentComposePopover from '../CommentComposePopover.vue'
import { useProjectStore } from '../../stores/projects'

const FULL_HEIGHT = 932
const KEYBOARD_HEIGHT = 596

type FakeViewport = EventTarget & { height: number; width: number }

/** One shared fake so listeners installed by an earlier mount keep working. */
const vv = new EventTarget() as FakeViewport
vv.height = FULL_HEIGHT
vv.width = 430
Object.defineProperty(window, 'visualViewport', { value: vv, configurable: true })

function setViewportHeight(height: number): void {
  vv.height = height
  vv.dispatchEvent(new Event('resize'))
}

// These mount into document.body via Teleport, so a test that throws before its
// unmount would leave a second .compose behind and fail the next one for the
// wrong reason.
enableAutoUnmount(afterEach)

function composeTop(): number {
  const el = document.body.querySelector('.compose') as HTMLElement
  return parseFloat(el.style.top)
}

// CommentComposePopover now includes a VoiceRecorder that touches the project
// store for transcription. Stub the recorder so the tests stay focused on the
// compose popover, and set up a store so setup() doesn't crash.
vi.mock('../VoiceRecorder.vue', () => ({
  default: {
    name: 'VoiceRecorderStub',
    setup() {
      return {}
    },
    render: () => null,
  },
}))

function mountCompose(props: { anchor?: { top: number; left: number } | null; modelValue?: string; images?: string[] } = {}) {
  setActivePinia(createPinia())
  const store = useProjectStore()
  store.activeChatId = 'chat-1'
  return mount(CommentComposePopover, {
    props: {
      anchor: props.anchor === undefined ? { top: 40, left: 80 } : props.anchor,
      modelValue: props.modelValue ?? '',
      images: props.images ?? [],
    },
    attachTo: document.body,
  })
}

describe('CommentComposePopover', () => {
  beforeEach(() => {
    setViewportHeight(FULL_HEIGHT)
    vi.unstubAllGlobals()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders without a selection quote', async () => {
    const wrapper = mountCompose({ modelValue: 'I did, close' })
    await nextTick()

    expect(document.body.textContent).toContain('Add comment')
    expect(document.body.textContent).toContain('Cancel')
    expect(document.body.textContent).not.toMatch(/^"/)
    expect(document.body.querySelector('.compose-input')).not.toBeNull()
    wrapper.unmount()
  })

  it('emits save on Cmd+Enter and cancel on Escape', async () => {
    const wrapper = mountCompose({ anchor: { top: 10, left: 10 }, modelValue: 'note' })
    await nextTick()

    const input = document.body.querySelector('.compose-input') as HTMLTextAreaElement
    await input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', metaKey: true, bubbles: true }))
    expect(wrapper.emitted('save')).toBeTruthy()

    await input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    expect(wrapper.emitted('cancel')).toBeTruthy()
    wrapper.unmount()
  })

  // The popover focuses its textarea on open, so on a phone the keyboard comes
  // up right after it is placed. It has to move up out of the way: it is
  // position: fixed, so anything the keyboard covers is unreachable.
  it('re-clamps above the keyboard when the viewport shrinks after opening', async () => {
    const wrapper = mountCompose({ anchor: { top: 700, left: 20 }, modelValue: 'note' })
    await nextTick()
    expect(composeTop()).toBe(700)

    setViewportHeight(KEYBOARD_HEIGHT)
    await nextTick()
    expect(composeTop()).toBeLessThanOrEqual(KEYBOARD_HEIGHT - 8)

    // And drops back when the keyboard goes away.
    setViewportHeight(FULL_HEIGHT)
    await nextTick()
    expect(composeTop()).toBe(700)
    wrapper.unmount()
  })

  it('leaves a popover that the keyboard does not reach where it is', async () => {
    const wrapper = mountCompose({ anchor: { top: 60, left: 20 }, modelValue: 'note' })
    await nextTick()

    setViewportHeight(KEYBOARD_HEIGHT)
    await nextTick()
    expect(composeTop()).toBe(60)
    wrapper.unmount()
  })

  it('does not render when anchor is null', async () => {
    const wrapper = mountCompose({ anchor: null, modelValue: '' })
    await nextTick()
    expect(document.body.querySelector('.compose')).toBeNull()
    wrapper.unmount()
  })

  it('shows a voice recorder button in the action row', async () => {
    const wrapper = mountCompose({ modelValue: '' })
    await nextTick()
    expect(document.body.querySelector('.compose-voice')).not.toBeNull()
    wrapper.unmount()
  })

  it('inserts transcribed voice text at the cursor', async () => {
    const wrapper = mountCompose({ modelValue: 'before ' })
    await nextTick()

    const store = useProjectStore()
    store.transcribeVoice = vi.fn(async () => 'after')

    const input = document.body.querySelector('.compose-input') as HTMLTextAreaElement
    input.focus()
    input.setSelectionRange(7, 7)

    // Simulate a recorded blob by invoking the internal handler through the
    // exposed ref (the VoiceRecorder stub never emits). We emit the event
    // directly on the wrapper's vm because the handler is internal to setup.
    // Instead, use the component's exposed toggleDictation + recorded handler
    // is private; call it via the recorded event on a recreated stub path.
    // Simpler: invoke the recorded handler through the template-bound listener.
    await wrapper.findComponent({ name: 'VoiceRecorderStub' }).vm.$emit('recorded', new Blob(['x'], { type: 'audio/webm' }))
    await flushPromises()
    await nextTick()

    expect(store.transcribeVoice).toHaveBeenCalledWith('chat-1', expect.any(Blob))
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual(['before after'])
    wrapper.unmount()
  })

  it('shows a spinner while transcribing and hides the recorder', async () => {
    const wrapper = mountCompose({ modelValue: '' })
    await nextTick()

    const store = useProjectStore()
    let resolveTranscribe: (text: string) => void = () => {}
    store.transcribeVoice = vi.fn(() => new Promise<string>((resolve) => { resolveTranscribe = resolve }))

    await wrapper.findComponent({ name: 'VoiceRecorderStub' }).vm.$emit('recorded', new Blob(['x'], { type: 'audio/webm' }))
    await nextTick()

    expect(document.body.querySelector('.voice-transcribing')).not.toBeNull()
    expect(document.body.querySelector('.compose-voice')).not.toBeNull()

    resolveTranscribe('done')
    await flushPromises()
    await nextTick()

    expect(document.body.querySelector('.voice-transcribing')).toBeNull()
    wrapper.unmount()
  })

  it('surfaces transcription errors as a store toast', async () => {
    const wrapper = mountCompose({ modelValue: '' })
    await nextTick()

    const store = useProjectStore()
    store.transcribeVoice = vi.fn(async () => { throw new Error('mic broken') })
    const pushError = vi.spyOn(store, 'pushErrorToast')

    await wrapper.findComponent({ name: 'VoiceRecorderStub' }).vm.$emit('recorded', new Blob(['x'], { type: 'audio/webm' }))
    await flushPromises()
    await nextTick()

    expect(pushError).toHaveBeenCalledWith('Voice transcription failed', expect.stringContaining('mic broken'))
    wrapper.unmount()
  })

  it('surfaces voice recorder errors as a store toast', async () => {
    const wrapper = mountCompose({ modelValue: '' })
    await nextTick()

    const store = useProjectStore()
    const pushError = vi.spyOn(store, 'pushErrorToast')

    await wrapper.findComponent({ name: 'VoiceRecorderStub' }).vm.$emit('error', 'permission denied')
    await nextTick()

    expect(pushError).toHaveBeenCalledWith('Voice dictation unavailable', 'permission denied')
    wrapper.unmount()
  })
})
