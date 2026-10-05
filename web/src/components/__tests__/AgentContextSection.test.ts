// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, RouterLinkStub } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import AgentContextSection from '../AgentContextSection.vue'
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
  vi.stubGlobal('fetch', vi.fn(async (url: string) => (
    url.includes(encodeURIComponent('work/AGENTS.md'))
      ? new Response('x'.repeat(4000), { status: 200 })
      : new Response('', { status: 404 })
  )))
  const wrapper = mount(AgentContextSection, {
    props: { project: PROJECT, contextPct: null, ...props },
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
    expect(rows[1].get('.agent-context-description').text()).toBe('Public beta launch planning.')
    expect(rows[1].find('pre').exists()).toBe(false)
    expect(rows[1].text()).not.toContain('project_context=')
    expect(rows[1].text()).not.toContain('Project brief, sent')
    expect(rows[1].get('.agent-context-doc').text()).toBe('launch.md')
    await rows[1].get('.agent-context-doc').trigger('click')
    expect(wrapper.emitted('open-file')?.[0]).toEqual(['work/memory-vault/projects/launch.md'])
    expect(wrapper.getComponent(RouterLinkStub).props('to')).toBe('/project/p1')
    await rows[0].trigger('click')
    expect(wrapper.emitted('open-file')?.[1]).toEqual(['work/AGENTS.md'])
  })

  it('drops the brief row when General sends no brief', async () => {
    const wrapper = await mountSection({
      project: { project_id: 'g', name: 'General', workspace: 'work' } as unknown as ProjectInfo,
    })
    expect(wrapper.find('.agent-context-brief').exists()).toBe(false)
  })

  it('shows the context used from a string percentage', async () => {
    const wrapper = await mountSection({ contextPct: 13.2 })
    expect(wrapper.get('[role="meter"]').text()).toBe('13%')
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
      props: { project: PROJECT, contextPct: null },
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
