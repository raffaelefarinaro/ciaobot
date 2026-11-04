// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import ComposerUploadProgress from '../ComposerUploadProgress.vue'
import type { ComposerUpload } from '../../composables/useChatComposer'

describe('ComposerUploadProgress', () => {
  it('renders nothing with no uploads', () => {
    const w = mount(ComposerUploadProgress, { props: { uploads: [] } })
    expect(w.find('.composer-uploads').exists()).toBe(false)
  })

  it('shows the file, percent and bar value while loading', () => {
    const uploads: ComposerUpload[] = [{ id: 1, label: 'brief.pdf', loaded: 42, total: 100 }]
    const w = mount(ComposerUploadProgress, { props: { uploads } })
    expect(w.text()).toContain('Loading brief.pdf…')
    expect(w.text()).toContain('42%')
    const progress = w.find('progress').element as HTMLProgressElement
    expect(progress.value).toBe(42)
  })

  it('switches to Processing once every byte is sent', () => {
    const done = mount(ComposerUploadProgress, {
      props: { uploads: [{ id: 1, label: 'brief.pdf', loaded: 100, total: 100 }] },
    })
    expect(done.text()).toContain('Processing brief.pdf…')
    expect(done.text()).not.toContain('Loading')

    const noBytes = mount(ComposerUploadProgress, {
      props: { uploads: [{ id: 2, label: 'brief.pdf', loaded: 0, total: 0 }] },
    })
    expect(noBytes.text()).toContain('Processing brief.pdf…')
    expect(noBytes.text()).not.toContain('Loading')
  })

  it('announces the phase through a status region', () => {
    const w = mount(ComposerUploadProgress, {
      props: { uploads: [{ id: 1, label: 'brief.pdf', loaded: 10, total: 100 }] },
    })
    expect(w.find('[role="status"]').exists()).toBe(true)
  })
})
