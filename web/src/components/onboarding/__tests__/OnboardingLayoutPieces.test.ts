// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent, ref } from 'vue'
import OnboardingStack from '../OnboardingStack.vue'
import OnboardingField from '../OnboardingField.vue'
import OnboardingChoiceGroup from '../OnboardingChoiceGroup.vue'
import OnboardingChoice from '../OnboardingChoice.vue'
import OnboardingStepFooter from '../OnboardingStepFooter.vue'
import OnboardingButton from '../OnboardingButton.vue'

describe('OnboardingStack', () => {
  it('maps its gap to a spacing step and renders the requested element', () => {
    const wrapper = mount(OnboardingStack, { props: { gap: 'xl', as: 'section' }, slots: { default: '<p>a</p><p>b</p>' } })
    expect(wrapper.element.tagName).toBe('SECTION')
    expect(wrapper.attributes('data-gap')).toBe('xl')
  })
})

describe('OnboardingField', () => {
  it('orders label, control, chips, help and error, and wires the described-by ids', () => {
    const wrapper = mount(OnboardingField, {
      props: { id: 'f', label: 'Name', help: 'Plain help', error: 'Bad' },
      slots: {
        default: '<template #default="{ describedby }"><input id="f" :aria-describedby="describedby"></template>',
        below: '<button type="button">Work</button>',
      },
    })
    const order = Array.from(wrapper.element.children as HTMLCollection).map((node: Element) => node.tagName + (node.className ? `.${node.className}` : ''))
    expect(order).toEqual(['LABEL', 'INPUT', 'DIV.ob-field-below', 'DIV.ob-field-help', 'P.ob-field-error'])
    expect(wrapper.get('input').attributes('aria-describedby')).toBe('f-help f-error')
    expect(wrapper.get('#f-help').text()).toBe('Plain help')
    expect(wrapper.get('[role="alert"]').text()).toBe('Bad')
  })

  it('does not reference an error that is not shown', () => {
    const wrapper = mount(OnboardingField, {
      props: { id: 'f', label: 'Name' },
      slots: { default: '<template #default="{ describedby }"><input id="f" :aria-describedby="describedby"></template>' },
    })
    expect(wrapper.get('input').attributes('aria-describedby')).toBe('f-help')
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  })
})

describe('OnboardingChoiceGroup', () => {
  const Host = defineComponent({
    components: { OnboardingChoiceGroup, OnboardingChoice },
    props: { disabled: Boolean },
    setup() { return { value: ref('a'), heard: ref(0) } },
    template: `<OnboardingChoiceGroup v-model="value" @update:model-value="heard++" label="Pick" name="pick" layout="cards" :disabled="disabled">
      <OnboardingChoice value="a">A<template #description>About A</template></OnboardingChoice>
      <OnboardingChoice value="b">B</OnboardingChoice>
    </OnboardingChoiceGroup><output>{{ value }}</output>`,
  })

  it('is a labelled radiogroup whose choices are native radios sharing one name', async () => {
    const wrapper = mount(Host)
    const group = wrapper.get('[role="radiogroup"]')
    expect(wrapper.get(`#${group.attributes('aria-labelledby')}`).text()).toBe('Pick')
    const radios = wrapper.findAll('input[type="radio"]')
    expect(radios.map(radio => radio.attributes('name'))).toEqual(['pick', 'pick'])
    expect((radios[0].element as HTMLInputElement).checked).toBe(true)
    expect(wrapper.text()).toContain('About A')
    await radios[1].setValue()
    expect(wrapper.get('output').text()).toBe('b')
    expect(wrapper.findAll('label')[1].attributes('data-checked')).toBe('true')
  })

  it('hears a click on the already checked choice, which fires no change event', async () => {
    const wrapper = mount(Host)
    await wrapper.findAll('input')[0].trigger('click')
    expect((wrapper.vm as unknown as { heard: number }).heard).toBe(1)
  })

  it('disables every choice with the group', () => {
    const wrapper = mount(Host, { props: { disabled: true } })
    expect(wrapper.findAll('input').every(input => (input.element as HTMLInputElement).disabled)).toBe(true)
  })

  it('refuses a choice outside a group', () => {
    expect(() => mount(OnboardingChoice, { props: { value: 'x' } })).toThrow(/OnboardingChoiceGroup/)
  })
})

describe('OnboardingStepFooter and OnboardingButton', () => {
  it('renders one footer with a start and an end row', () => {
    const wrapper = mount(OnboardingStepFooter, { slots: { start: '<b class="s">Back</b>', default: '<b class="e">Go</b>' } })
    expect(wrapper.findAll('footer')).toHaveLength(1)
    const rows = wrapper.findAll('.ob-footer-row')
    expect(rows[0].find('.s').exists()).toBe(true)
    expect(rows[1].find('.e').exists()).toBe(true)
  })

  it('is a non-submitting button unless told otherwise, and passes attributes through', () => {
    const plain = mount(OnboardingButton, { attrs: { disabled: true, class: 'x' }, slots: { default: 'Go' } })
    expect(plain.attributes('type')).toBe('button')
    expect(plain.attributes('disabled')).toBeDefined()
    expect(plain.classes()).toContain('x')
    expect(mount(OnboardingButton, { attrs: { type: 'submit' } }).attributes('type')).toBe('submit')
  })
})
