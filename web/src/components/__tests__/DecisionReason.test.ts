// @vitest-environment jsdom

import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import DecisionReason from '../DecisionReason.vue'

function mountReason(props: { modelValue?: string, disabled?: boolean, chips?: readonly string[], label?: string } = {}) {
  return mount(DecisionReason, { props: { modelValue: '', idPrefix: 'why', ...props } })
}

describe('DecisionReason', () => {
  it('renders the five quick reasons with their exact labels', () => {
    const wrapper = mountReason()
    expect(wrapper.findAll('.task-chip').map((chip) => chip.text())).toEqual([
      'not mine', 'not a task', 'already done', 'wrong person', 'good as is',
    ])
    expect(wrapper.get('label').text()).toBe('Why? (optional)')
  })

  it('names the chip group for assistive tech', () => {
    const group = mountReason().get('[role="group"]')
    expect(group.attributes('aria-label')).toBe('Quick reasons')
  })

  it('clicking a chip emits the new text and reflects it in aria-pressed', async () => {
    const wrapper = mountReason({ modelValue: 'because' })
    await wrapper.findAll('.task-chip')[0]!.trigger('click')
    expect(wrapper.emitted('update:modelValue')![0]).toEqual(['because, not mine'])
    await wrapper.setProps({ modelValue: 'because, not mine' })
    expect(wrapper.findAll('.task-chip')[0]!.attributes('aria-pressed')).toBe('true')
    expect(wrapper.findAll('.task-chip')[1]!.attributes('aria-pressed')).toBe('false')
    await wrapper.findAll('.task-chip')[0]!.trigger('click')
    expect(wrapper.emitted('update:modelValue')![1]).toEqual(['because'])
  })

  it('typing in the textarea emits and the field is capped at 1000', async () => {
    const wrapper = mountReason()
    const field = wrapper.get('textarea')
    expect(field.attributes('maxlength')).toBe('1000')
    expect(wrapper.get('label').attributes('for')).toBe(field.attributes('id'))
    await field.setValue('my words')
    expect(wrapper.emitted('update:modelValue')![0]).toEqual(['my words'])
  })

  it('disabled disables every control', () => {
    const wrapper = mountReason({ disabled: true })
    expect(wrapper.get('textarea').attributes('disabled')).toBeDefined()
    for (const chip of wrapper.findAll('.task-chip')) expect(chip.attributes('disabled')).toBeDefined()
  })

  it('offers the chips it is given and a custom label, composing them the same way', async () => {
    const wrapper = mountReason({ chips: ['one-off', 'not true'], label: 'Reason (optional)', modelValue: 'one-off' })
    expect(wrapper.findAll('.task-chip').map((chip) => chip.text())).toEqual(['one-off', 'not true'])
    expect(wrapper.get('label').text()).toBe('Reason (optional)')
    expect(wrapper.findAll('.task-chip')[0]!.attributes('aria-pressed')).toBe('true')
    await wrapper.findAll('.task-chip')[1]!.trigger('click')
    expect(wrapper.emitted('update:modelValue')![0]).toEqual(['one-off, not true'])
  })
})
