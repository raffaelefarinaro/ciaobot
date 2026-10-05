// @vitest-environment jsdom

import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import TaskHandoverCard from '../TaskHandoverCard.vue'

const PROMPT = 'Instruction.\n\nTitle: Ship the board\nTask id: ship\n\n<task-board-task>\n- **land** the types\n<img src=x onerror=alert(1)>\n</task-board-task>'

describe('TaskHandoverCard', () => {
  it('shows the title and the rendered description, with the raw prompt behind a disclosure', () => {
    const wrapper = mount(TaskHandoverCard, {
      props: {
        handover: { title: 'Ship the board', description: '- **land** the types\n<img src=x onerror=alert(1)>' },
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
      props: { handover: { title: 'Empty', description: '' }, prompt: 'x' },
    })
    expect(wrapper.find('.task-handover-description').exists()).toBe(false)
    expect(wrapper.get('.task-handover-empty').text()).toBe('No description.')
  })
})
