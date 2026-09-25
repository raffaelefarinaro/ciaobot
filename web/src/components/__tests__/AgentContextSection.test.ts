// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, RouterLinkStub } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import AgentContextSection from '../AgentContextSection.vue'
import { useFileViewerStore } from '../../stores/fileViewer'
import type { ProjectInfo } from '../../lib/types'

const PROJECT = {
  project_id: 'p1',
  name: 'Launch',
  workspace: 'work',
  context: 'Public beta launch planning.',
  vault_doc_path: 'work/memory-vault/projects/launch.md',
} as unknown as ProjectInfo

async function mountSection(props: Partial<InstanceType<typeof AgentContextSection>['$props']> = {}) {
  setActivePinia(createPinia())
  const viewer = useFileViewerStore()
  vi.spyOn(viewer, 'loadMarkdownPaths').mockResolvedValue(['work/memory-vault/People/Mo.md'])
  vi.stubGlobal('fetch', vi.fn(async (url: string) => (
    url.includes(encodeURIComponent('work/AGENTS.md'))
      ? new Response('x'.repeat(4000), { status: 200 })
      : new Response('', { status: 404 })
  )))
  const wrapper = mount(AgentContextSection, {
    props: { project: PROJECT, entities: undefined, contextPct: null, ...props },
    global: { stubs: { RouterLink: RouterLinkStub } },
  })
  await flushPromises()
  return wrapper
}

describe('AgentContextSection', () => {
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

  it('lists the guide and the project brief with their sizes', async () => {
    const wrapper = await mountSection()
    const rows = wrapper.findAll('.agent-context-row')
    expect(rows[0].text()).toContain('AGENTS.md')
    expect(rows[0].text()).toContain('1k tokens')
    expect(rows[1].text()).toContain('Launch')
    expect(rows[1].text()).toContain('launch.md')
    expect(rows[1].text()).toContain('Public beta launch planning.')
    expect(wrapper.getComponent(RouterLinkStub).props('to')).toBe('/project/p1')
    await rows[0].trigger('click')
    expect(wrapper.emitted('open-file')?.[0]).toEqual(['work/AGENTS.md'])
  })

  it('shows the context used from a string percentage', async () => {
    const wrapper = await mountSection({ contextPct: 13.2 })
    expect(wrapper.get('[role="meter"]').text()).toBe('13%')
  })

  it('lists matched notes one per line and opens the resolved file', async () => {
    const wrapper = await mountSection({
      entities: [{ name: 'Mo', path: 'work/People/Mo.md', category: 'person' }],
    })
    const notes = wrapper.get('[aria-labelledby="agent-context-notes-label"]').findAll('.agent-context-row')
    expect(notes).toHaveLength(1)
    expect(notes[0].text()).toBe('Moperson')
    await notes[0].trigger('click')
    expect(wrapper.emitted('open-file')?.[0]).toEqual(['work/memory-vault/People/Mo.md'])
  })

  it('says None when the last message matched nothing', async () => {
    const wrapper = await mountSection({ entities: [] })
    expect(wrapper.text()).toContain('None.')
  })

  it('says the guide could not be read, with a retry, instead of dropping the row', async () => {
    setActivePinia(createPinia())
    let failing = true
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (failing) throw new Error('network down')
      return url.includes(encodeURIComponent('work/AGENTS.md'))
        ? new Response('guide', { status: 200 })
        : new Response('', { status: 404 })
    }))
    const wrapper = mount(AgentContextSection, {
      props: { project: PROJECT, entities: undefined, contextPct: null },
      global: { stubs: { RouterLink: RouterLinkStub } },
    })
    await flushPromises()
    const error = wrapper.get('.agent-context-error')
    expect(error.text()).toContain('network down')

    failing = false
    await error.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.find('.agent-context-error').exists()).toBe(false)
    expect(wrapper.text()).toContain('AGENTS.md')
  })
})
