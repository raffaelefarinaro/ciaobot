// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import type { ProjectInfo } from '../../lib/types'
import { useTaskStore } from '../../stores/tasks'
import { useProjectStore } from '../../stores/projects'
import NewScheduleForm from '../NewScheduleForm.vue'

const Stub = defineComponent({ render: () => h('div') })

const mounted: VueWrapper[] = []

async function mountForm(): Promise<VueWrapper> {
  const tasks = useTaskStore()
  tasks.fetchModels = vi.fn(async () => {}) as typeof tasks.fetchModels
  tasks.createSchedule = vi.fn(async () => {}) as unknown as typeof tasks.createSchedule
  const projects = useProjectStore()
  projects.activeWorkspace = 'work'
  projects.projects = [
    { project_id: 'proj-1', name: 'General', workspace: 'work', is_auto: true } as ProjectInfo,
  ]
  const wrapper = mount(NewScheduleForm, {
    attachTo: document.body,
    global: { stubs: { ModelSelector: Stub } },
  })
  mounted.push(wrapper)
  await flushPromises()
  return wrapper
}

function railText(wrapper: VueWrapper) {
  return wrapper.find('.page-rail').text()
}

describe('NewScheduleForm', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    while (mounted.length) mounted.pop()?.unmount()
  })

  it('uses the detail page sections, prompt first, with no uppercase field labels', async () => {
    const wrapper = await mountForm()
    const headings = wrapper.findAll('.new-form h2').map(h => h.text())
    expect(headings).toEqual(['Prompt', 'When', 'Where it runs', 'Results'])
    expect(wrapper.find('.form-group').exists()).toBe(false)
  })

  it('previews every-day runs when a weekly automation has no days', async () => {
    const wrapper = await mountForm()
    await wrapper.find('#nsf-time').setValue('08:00')
    expect(wrapper.text()).toContain('No days picked, so it runs every day.')
    expect(railText(wrapper)).toContain('Next run')
    expect(railText(wrapper)).toContain('Each run opens a new chat in General.')
  })

  it('toggles weekdays as pressed buttons and submits them in week order', async () => {
    const wrapper = await mountForm()
    const tasks = useTaskStore()
    const day = (label: string) => wrapper.find(`.nsf-day[aria-label="${label}"]`)
    await day('Friday').trigger('click')
    await day('Monday').trigger('click')
    expect(day('Monday').attributes('aria-pressed')).toBe('true')
    await day('Friday').trigger('click')
    await day('Friday').trigger('click')
    await wrapper.find('#nsf-time').setValue('08:00')
    await wrapper.find('#nsf-prompt').setValue('Brief me')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(tasks.createSchedule).toHaveBeenCalledWith(
      expect.objectContaining({ daysOfWeek: ['mon', 'fri'], webProjectId: 'proj-1', archivePolicy: 'manual' }),
    )
    expect(wrapper.emitted('created')).toBeTruthy()
  })

  it('picks the archive policy from the Afterwards control', async () => {
    const wrapper = await mountForm()
    await wrapper.find('input[value="auto"]').setValue(true)
    expect(wrapper.find('.nsf-seg label.on').text()).toBe('Archive if nothing to judge')
  })

  it('drops the time of day for manual automations', async () => {
    const wrapper = await mountForm()
    await wrapper.find('#nsf-frequency').setValue('manual')
    expect(wrapper.find('#nsf-time').exists()).toBe(false)
    expect(railText(wrapper)).toContain('When you click Run')
  })

  it('emits cancel from the Cancel button', async () => {
    const wrapper = await mountForm()
    await wrapper.find('.nsf-cancel').trigger('click')
    expect(wrapper.emitted('cancel')).toBeTruthy()
  })
})
