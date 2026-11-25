// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import ProjectDecisionEntry from '../ProjectDecisionEntry.vue'

const { get, post } = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))
vi.mock('../../lib/api', () => ({ api: { get, post } }))

describe('project decisions', () => {
  beforeEach(() => { get.mockReset(); post.mockReset() })

  it('shows only this project’s active standing permissions and records the authored decision', async () => {
    get.mockResolvedValue({ disclosure_tokens: { p1: 'scope' }, grants: [
      { grant_id: 'one', project_id: 'p1', type: 'standing', status: 'active', brain: { name: 'Team' }, audience: 'team', field_mask: ['decision'], producers: ['memory_extraction'] },
      { grant_id: 'two', project_id: 'p2', type: 'standing', status: 'active', brain: { name: 'Foreign' }, audience: 'foreign', field_mask: ['reason'], producers: ['nightly_curation'] },
    ] })
    post.mockResolvedValue({})
    const wrapper = mount(ProjectDecisionEntry, { props: { workspace: 'work', projectId: 'p1' } })
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Team (team): decision for memory_extraction')
    expect(wrapper.text()).not.toContain('Foreign')
    expect(wrapper.text()).toContain('Automatic sharing is not active yet')
    const fields = wrapper.findAll('input')
    await fields[0].setValue('Choose blue')
    await fields[1].setValue('Readable')
    await fields[2].setValue('2026-11-25')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(post).toHaveBeenCalledWith('/api/workspaces/work/share-evidence/decisions', { project_id: 'p1', decision: 'Choose blue', reason: 'Readable', effective_date: '2026-11-25', disclosure_token: 'scope' })
    expect(wrapper.text()).toContain('Decision recorded.')
  })

  it('does not offer a save when permissions cannot be read', async () => {
    get.mockRejectedValue(new Error('Unavailable'))
    const wrapper = mount(ProjectDecisionEntry, { props: { workspace: 'work', projectId: 'p1' } })
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.find('[role="alert"]').exists()).toBe(true)
    expect(wrapper.find('form').exists()).toBe(false)
    expect(post).not.toHaveBeenCalled()
  })

  it('refreshes changed permissions after a refused save and keeps the draft', async () => {
    get.mockResolvedValueOnce({ disclosure_tokens: { p1: 'old' }, grants: [] })
    get.mockResolvedValueOnce({ disclosure_tokens: { p1: 'new' }, grants: [
      { grant_id: 'new', project_id: 'p1', type: 'standing', status: 'active', brain: { name: 'Team' }, audience: 'team', field_mask: ['decision'], producers: ['memory_extraction'] },
    ] })
    post.mockRejectedValueOnce(Object.assign(new Error('Sharing permissions changed. Review them before saving.'), { status: 409 }))
    const wrapper = mount(ProjectDecisionEntry, { props: { workspace: 'work', projectId: 'p1' } })
    await wrapper.get('button').trigger('click')
    await flushPromises()
    await wrapper.findAll('input')[0].setValue('Keep this draft')
    await wrapper.findAll('input')[2].setValue('2026-11-25')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('Team (team): decision for memory_extraction')
    expect(wrapper.text()).toContain('Sharing permissions changed')
    expect((wrapper.findAll('input')[0].element as HTMLInputElement).value).toBe('Keep this draft')
    expect(post).toHaveBeenCalledTimes(1)
  })
})
