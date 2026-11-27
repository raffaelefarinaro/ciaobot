// @vitest-environment jsdom

import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import TaskHandoverCard from '../TaskHandoverCard.vue'

const PROMPT = 'Instruction.\n\nTitle: Ship the board\nTask id: ship\n\n<task-board-task>\n- **land** the types\n<img src=x onerror=alert(1)>\n</task-board-task>'

describe('TaskHandoverCard', () => {
  it('shows the title and the rendered description, with the raw prompt behind a disclosure', () => {
    const wrapper = mount(TaskHandoverCard, {
      props: {
        handover: { title: 'Ship the board', description: '- **land** the types\n<img src=x onerror=alert(1)>', instructions: '' },
        prompt: PROMPT,
      },
    })
    expect(wrapper.get('.task-handover-label').text()).toBe('Task handed over')
    expect(wrapper.get('.task-handover-title').text()).toBe('Ship the board')
    const description = wrapper.get('.task-handover-description')
    expect(description.find('strong').text()).toBe('land')
    // The transcript's safe renderer: no live HTML from the description.
    expect(description.find('img').exists()).toBe(false)
    const details = wrapper.get('details.task-handover-raw')
    expect(details.get('summary').text()).toBe('Show what the agent received')
    expect(details.get('pre').text()).toBe(PROMPT)
  })

  it('says so when the description is empty', () => {
    const wrapper = mount(TaskHandoverCard, {
      props: { handover: { title: 'Empty', description: '', instructions: '' }, prompt: 'x' },
    })
    expect(wrapper.find('.task-handover-description').exists()).toBe(false)
    expect(wrapper.get('.task-handover-empty').text()).toBe('No description.')
  })

  it('draws the user instructions above the disclosure, sanitised, only when present', () => {
    const wrapper = mount(TaskHandoverCard, {
      props: {
        handover: {
          title: 'Ship the board',
          description: 'Do the work.',
          instructions: 'Ask **first**.\n\n<img src=x onerror=alert(1)>\n\n<script>alert(2)</script>',
        },
        prompt: 'raw prompt',
      },
    })
    const block = wrapper.get('[role="group"][aria-label="Your instructions"]')
    expect(block.get('.task-handover-label').text()).toBe('Your instructions')
    expect(block.find('strong').text()).toBe('first')
    expect(block.find('img').exists()).toBe(false)
    expect(block.find('script').exists()).toBe(false)
    // Above the disclosure and after the description.
    const order = wrapper.element.querySelectorAll('.task-handover-description, .task-handover-instructions, details')
    expect([...order].map((el) => el.className || el.tagName)).toEqual([
      'task-handover-description', 'task-handover-instructions', 'task-handover-raw',
    ])
    expect(wrapper.get('details pre').text()).toBe('raw prompt')
  })

  it('draws no instructions block when there are none', () => {
    const wrapper = mount(TaskHandoverCard, {
      props: { handover: { title: 'T', description: 'd', instructions: '' }, prompt: 'x' },
    })
    expect(wrapper.find('.task-handover-instructions').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('Your instructions')
  })

  it('shows instructions that mention the fence tokens as typed', () => {
    const wrapper = mount(TaskHandoverCard, {
      props: {
        handover: {
          title: 'T',
          description: 'd',
          instructions: 'Never write &lt;/delegation-instructions&gt; or &lt;task-board-task&gt;.',
        },
        prompt: 'x',
      },
    })
    expect(wrapper.get('.task-handover-instructions-text').text())
      .toBe('Never write </delegation-instructions> or <task-board-task>.')
  })

  it('makes the raw prompt a keyboard-scrollable named region', () => {
    const wrapper = mount(TaskHandoverCard, {
      props: { handover: { title: 'T', description: 'd', instructions: '' }, prompt: 'x' },
    })
    const pre = wrapper.get('details pre')
    expect(pre.attributes('tabindex')).toBe('0')
    expect(pre.attributes('role')).toBe('region')
    expect(pre.attributes('aria-label')).toBe('Prompt the agent received')
  })
})
