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
  context: 'Public beta launch planning. Hub doc: launch.md',
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
    props: { project: PROJECT, contextPct: null, compacting: false, compactDisabled: false, ...props },
    global: { stubs: { RouterLink: RouterLinkStub } },
  })
  await flushPromises()
  return wrapper
}

describe('AgentContextSection', () => {
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

  it('heads the file list with Always loaded', async () => {
    const wrapper = await mountSection()
    const label = wrapper.get('.rail-label')
    expect(label.text()).toBe('Always loaded')
    expect(label.element.nextElementSibling?.classList.contains('rail-list')).toBe(true)
  })

  it('gives context usage and always loaded their own headings', async () => {
    const wrapper = await mountSection({ contextPct: 42 })
    const sections = wrapper.findAll('section.rail-section')
    expect(sections).toHaveLength(2)
    const usage = sections[0]
    const always = sections[1]
    const usageLabel = usage.get('.rail-label')
    const alwaysLabel = always.get('.rail-label')
    expect(usageLabel.text()).toBe('Context usage')
    expect(alwaysLabel.text()).toBe('Always loaded')
    // Each group is named by its own heading.
    expect(usage.attributes('aria-labelledby')).toBe(usageLabel.attributes('id'))
    expect(always.attributes('aria-labelledby')).toBe(alwaysLabel.attributes('id'))
    expect(alwaysLabel.attributes('id')).toBe('agent-context-label')
    // The file list belongs to the always-loaded group, not to the usage one.
    expect(usage.find('.rail-list').exists()).toBe(false)
    expect(always.find('.rail-list').exists()).toBe(true)
  })

  it('describes reported context usage in words', async () => {
    const wrapper = await mountSection({ contextPct: 56 })
    expect(wrapper.get('.agent-context-usage-note').text()).toBe('56% of context window used')
  })

  it('keeps usage unknown rather than inventing a percentage', async () => {
    const wrapper = await mountSection({ contextPct: null })
    expect(wrapper.find('.agent-context-usage-note').exists()).toBe(false)
    expect(wrapper.find('[role="meter"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('of context window used')
    // The always-loaded group stands on its own without a reported figure.
    expect(wrapper.get('.rail-label').text()).toBe('Always loaded')
  })

  it('lists the guide and the project brief with their sizes', async () => {
    const wrapper = await mountSection()
    const rows = wrapper.findAll('.agent-context-row')
    expect(rows[0].text()).toContain('AGENTS.md')
    expect(rows[0].text()).toContain('1k tokens')
    expect(rows[1].text()).toContain('Launch')
    expect(rows[1].get('.agent-context-description').text()).toBe(PROJECT.context)
    expect(rows[1].find('pre').exists()).toBe(false)
    expect(rows[1].text()).not.toContain('project_context=')
    expect(rows[1].text()).not.toContain('Project brief, sent')
    expect(rows[1].find('.agent-context-doc').exists()).toBe(false)
    expect(rows[1].get('.agent-context-description a').text()).toBe('launch.md')
    await rows[1].get('.agent-context-description a').trigger('click')
    expect(wrapper.emitted('open-file')?.[0]).toEqual(['work/memory-vault/projects/launch.md'])
    expect(wrapper.getComponent(RouterLinkStub).props('to')).toBe('/project/p1')
    await rows[0].trigger('click')
    expect(wrapper.emitted('open-file')?.[1]).toEqual(['work/AGENTS.md'])
  })

  it('links a full canonical path in place and escapes HTML in the context', async () => {
    const context = `Read ${PROJECT.vault_doc_path}. <img src=x onerror=alert(1)>`
    const wrapper = await mountSection({ project: { ...PROJECT, context } })
    const description = wrapper.get('.agent-context-description')
    expect(description.text()).toBe(context)
    expect(description.find('img').exists()).toBe(false)
    await description.get('a').trigger('click')
    expect(wrapper.emitted('open-file')?.[0]).toEqual([PROJECT.vault_doc_path])
  })

  it('does not add a separate document link when context does not mention it', async () => {
    const wrapper = await mountSection({ project: { ...PROJECT, context: 'Public beta planning.' } })
    expect(wrapper.get('.agent-context-description').text()).toBe('Public beta planning.')
    expect(wrapper.find('.agent-context-brief button').exists()).toBe(false)
    expect(wrapper.find('.agent-context-description a').exists()).toBe(false)
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
      props: { project: PROJECT, contextPct: null, compacting: false, compactDisabled: false },
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

  it('puts a Compact button beside the context meter and emits compact', async () => {
    const wrapper = await mountSection({ contextPct: 42 })
    const button = wrapper.get('.agent-context-meter-row button')
    expect(button.text()).toBe('Compact')
    expect(button.attributes('disabled')).toBeUndefined()
    expect(wrapper.get('[role="meter"]').element.parentElement).toBe(button.element.parentElement)
    await button.trigger('click')
    expect(wrapper.emitted('compact')).toHaveLength(1)
  })

  it('shows no Compact button until the provider has reported a context figure', async () => {
    const wrapper = await mountSection({ contextPct: null })
    expect(wrapper.find('.agent-context-compact').exists()).toBe(false)
  })

  it('disables Compact while the chat streams, and says so while a request is in flight', async () => {
    const busy = await mountSection({ contextPct: 42, compactDisabled: true })
    const disabled = busy.get('.agent-context-compact')
    expect(disabled.attributes('disabled')).toBeDefined()
    await disabled.trigger('click')
    expect(busy.emitted('compact')).toBeUndefined()

    const pending = await mountSection({ contextPct: 42, compacting: true, compactDisabled: true })
    const label = pending.get('.agent-context-compact')
    expect(label.text()).toBe('Compacting…')
    expect(label.attributes('aria-busy')).toBe('true')
  })

  const repoProject = (instructions: { name: 'AGENTS.md' | 'CLAUDE.md'; chars: number } | null) => ({
    ...PROJECT,
    repo: { path: '/r/upwordo', branch: 'main', dirty: false, missing: false, instructions },
  }) as unknown as ProjectInfo

  it('lists the repository instructions in code mode with their size', async () => {
    const wrapper = await mountSection({ project: repoProject({ name: 'AGENTS.md', chars: 4000 }), codeMode: true })
    const row = wrapper.get('.agent-context-repo')
    expect(row.text()).toContain('upwordo/AGENTS.md')
    expect(row.text()).toContain('1k tokens')
    expect(row.text()).toContain('Repository instructions')
    await row.trigger('click')
    expect(wrapper.emitted('open-file')?.[0]).toEqual(['/r/upwordo/AGENTS.md'])
  })

  it('says not found when the repository has no instructions', async () => {
    const wrapper = await mountSection({ project: repoProject(null), codeMode: true })
    const row = wrapper.get('.agent-context-repo')
    expect(row.text()).toContain('Repository instructions')
    expect(row.text()).toContain('not found')
  })

  it('has no repository row while code mode is off', async () => {
    const wrapper = await mountSection({ project: repoProject({ name: 'AGENTS.md', chars: 4000 }), codeMode: false })
    expect(wrapper.find('.agent-context-repo').exists()).toBe(false)
  })
})
