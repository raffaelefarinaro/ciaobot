// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, RouterLinkStub } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import AgentContextSection from '../AgentContextSection.vue'
import { writeClipboard } from '../../lib/codeCopy'
import type { ProjectInfo } from '../../lib/types'

vi.mock('../../lib/codeCopy', () => ({ writeClipboard: vi.fn() }))

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

  const SYSTEM_PROMPT = 'You are Ciao.\n'.repeat(400)

  async function mountWithSystemPrompt(promptResponse: () => Response | Promise<Response>) {
    setActivePinia(createPinia())
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url === '/api/system-prompt') return promptResponse()
      return url.includes(encodeURIComponent('work/AGENTS.md'))
        ? new Response('x'.repeat(4000), { status: 200 })
        : new Response('', { status: 404 })
    }))
    const wrapper = mount(AgentContextSection, {
      props: { project: PROJECT, contextPct: null, compacting: false, compactDisabled: false },
      global: { stubs: { RouterLink: RouterLinkStub } },
    })
    await flushPromises()
    return wrapper
  }
  const promptOk = () => new Response(JSON.stringify({ text: SYSTEM_PROMPT, chars: SYSTEM_PROMPT.length }), { status: 200 })

  it('lists the Ciaobot system prompt first, with its tokens in the total', async () => {
    const wrapper = await mountWithSystemPrompt(promptOk)
    const row = wrapper.get('.agent-context-system')
    expect(wrapper.get('.rail-list').element.firstElementChild).toBe(row.element)
    expect(row.text()).toContain('Ciaobot system prompt')
    expect(row.text()).toContain('1.4k tokens')
    expect(row.text()).toContain("Added to the provider's own system prompt")
    // 1.4k prompt + 1k guide + a small brief; without the prompt the total would read 1k.
    expect(wrapper.get('.agent-context-summary .agent-context-meta').text()).toBe('2.4k tokens')
  })

  it('shows the system prompt text only once its toggle is opened', async () => {
    const wrapper = await mountWithSystemPrompt(promptOk)
    expect(wrapper.find('.agent-context-system-text').exists()).toBe(false)
    const toggle = wrapper.get('.agent-context-system-toggle')
    expect(toggle.attributes('aria-expanded')).toBe('false')
    await toggle.trigger('click')
    expect(toggle.attributes('aria-expanded')).toBe('true')
    expect(wrapper.get('pre.agent-context-system-text').text()).toContain('You are Ciao.')
    expect(toggle.attributes('aria-controls')).toBe(wrapper.get('.agent-context-system-body').attributes('id'))
  })

  it('hides the system prompt row when the fetch fails', async () => {
    const wrapper = await mountWithSystemPrompt(() => new Response('boom', { status: 500 }))
    expect(wrapper.find('.agent-context-system').exists()).toBe(false)
    expect(wrapper.get('.agent-context-summary .agent-context-meta').text()).toBe('1k tokens')
  })

  it('hides the system prompt row when the text is empty', async () => {
    const wrapper = await mountWithSystemPrompt(() => new Response(JSON.stringify({ text: '', chars: 0 }), { status: 200 }))
    expect(wrapper.find('.agent-context-system').exists()).toBe(false)
    expect(wrapper.get('.agent-context-summary .agent-context-meta').text()).toBe('1k tokens')
  })

  describe('copying the system prompt', () => {
    afterEach(() => { vi.useRealTimers() })

    async function openedSection() {
      const wrapper = await mountWithSystemPrompt(promptOk)
      await wrapper.get('.agent-context-system-toggle').trigger('click')
      return wrapper
    }

    it('says Copied, writes the text, then goes back to Copy after 1.5 seconds', async () => {
      vi.useFakeTimers()
      vi.mocked(writeClipboard).mockResolvedValue(true)
      const wrapper = await openedSection()
      const copy = wrapper.get('.agent-context-system-body button')
      expect(copy.text()).toBe('Copy')
      await copy.trigger('click')
      await flushPromises()
      expect(writeClipboard).toHaveBeenCalledWith(SYSTEM_PROMPT)
      expect(copy.text()).toBe('Copied')
      vi.advanceTimersByTime(1500)
      await flushPromises()
      expect(copy.text()).toBe('Copy')
    })

    it('keeps Copy when the clipboard write fails', async () => {
      vi.useFakeTimers()
      vi.mocked(writeClipboard).mockResolvedValue(false)
      const wrapper = await openedSection()
      const copy = wrapper.get('.agent-context-system-body button')
      await copy.trigger('click')
      await flushPromises()
      expect(copy.text()).toBe('Copy')
    })

    it('clears the Copied timer on unmount', async () => {
      vi.useFakeTimers()
      vi.mocked(writeClipboard).mockResolvedValue(true)
      const wrapper = await openedSection()
      await wrapper.get('.agent-context-system-body button').trigger('click')
      await flushPromises()
      wrapper.unmount()
      expect(() => vi.advanceTimersByTime(1500)).not.toThrow()
      expect(vi.getTimerCount()).toBe(0)
    })
  })

  it('keeps Always loaded closed behind a summary that names its cost', async () => {
    localStorage.removeItem('ciao.chatDetails.alwaysLoadedOpen')
    const wrapper = await mountSection()
    const details = wrapper.get('details.agent-context-loaded')
    expect(details.attributes('open')).toBeUndefined()
    const summary = details.get('summary')
    expect(details.element.firstElementChild).toBe(summary.element)
    expect(summary.get('.agent-context-summary-label').text()).toBe('Always loaded')
    // The guide (1k) and the brief together, so the closed row still says what it costs.
    expect(summary.get('.agent-context-meta').text()).toMatch(/tokens$/)
    expect(details.find('.rail-list').exists()).toBe(true)
  })

  it('remembers an opened Always loaded and opens when pointed at', async () => {
    localStorage.removeItem('ciao.chatDetails.alwaysLoadedOpen')
    const wrapper = await mountSection()
    const details = wrapper.get('details.agent-context-loaded')
    ;(details.element as HTMLDetailsElement).open = true
    await details.trigger('toggle')
    expect(localStorage.getItem('ciao.chatDetails.alwaysLoadedOpen')).toBe('1')
    localStorage.setItem('ciao.chatDetails.alwaysLoadedOpen', '0')
    const closed = await mountSection()
    expect(closed.get('details.agent-context-loaded').attributes('open')).toBeUndefined()
    await closed.setProps({ revealKey: 1 })
    expect(closed.get('details.agent-context-loaded').attributes('open')).toBeDefined()
  })

  it('gives context usage its own heading, apart from Always loaded', async () => {
    const wrapper = await mountSection({ contextPct: 42 })
    const usage = wrapper.get('section.rail-section')
    const usageLabel = usage.get('.rail-label')
    expect(usageLabel.text()).toBe('Context usage')
    expect(usage.attributes('aria-labelledby')).toBe(usageLabel.attributes('id'))
    // The file list belongs to the always-loaded group, not to the usage one.
    expect(usage.find('.rail-list').exists()).toBe(false)
    expect(wrapper.get('details.agent-context-loaded').find('.rail-list').exists()).toBe(true)
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
    expect(wrapper.get('.agent-context-summary-label').text()).toBe('Always loaded')
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
