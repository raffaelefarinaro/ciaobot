// @vitest-environment jsdom

import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import HtmlArtifactViewer from '../HtmlArtifactViewer.vue'

function mountViewer(props: Partial<InstanceType<typeof HtmlArtifactViewer>['$props']> = {}) {
  return mount(HtmlArtifactViewer, {
    props: {
      filePath: 'Workspace/dashboard.html',
      reloadToken: 42,
      view: 'preview',
      source: '<h1>hi</h1>',
      ...props,
    },
  })
}

describe('HtmlArtifactViewer', () => {
  it('renders the artifact in a frame by default', () => {
    const frame = mountViewer().get('iframe')
    expect(frame.attributes('src')).toBe(
      '/api/workspace-html?path=Workspace%2Fdashboard.html&t=42',
    )
  })

  it('sandboxes the frame without same-origin access', () => {
    // The one attribute that must never appear: with allow-same-origin the
    // artifact's script would run with the user's session and full access to
    // the embedding app. Keep this assertion even if the list grows.
    const sandbox = mountViewer().get('iframe').attributes('sandbox')
    expect(sandbox).toBe('allow-scripts')
    expect(sandbox).not.toContain('allow-same-origin')
  })

  it('unmounts the frame in Code view so artifact script stops running', () => {
    const wrapper = mountViewer({ view: 'code' })
    expect(wrapper.find('iframe').exists()).toBe(false)
    expect(wrapper.get('.hav-code').text()).toContain('<h1>hi</h1>')
  })

  it('emits the requested view when a tab is clicked', async () => {
    const wrapper = mountViewer()
    await wrapper.findAll('.hav-tab')[1].trigger('click')
    expect(wrapper.emitted('update:view')).toEqual([['code']])
  })

  it('shows a source error without hiding the toggle', () => {
    const wrapper = mountViewer({ view: 'code', source: '', sourceError: 'Source is too large to show (>2 MB).' })
    expect(wrapper.get('.hav-note-error').text()).toContain('too large')
    expect(wrapper.findAll('.hav-tab')).toHaveLength(2)
  })
})
