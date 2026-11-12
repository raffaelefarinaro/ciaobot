// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import SkeletonLoader from '../SkeletonLoader.vue'

describe('SkeletonLoader', () => {
  it('is one announced status whose shapes are hidden from assistive tech', () => {
    const wrapper = mount(SkeletonLoader, { props: { label: 'Loading tasks' } })
    const root = wrapper.get('.skeleton')
    expect(root.attributes('role')).toBe('status')
    expect(root.attributes('aria-label')).toBe('Loading tasks')
    expect(wrapper.findAll('.skeleton-row')).toHaveLength(4)
    for (const row of wrapper.findAll('.skeleton-row')) expect(row.attributes('aria-hidden')).toBe('true')
  })

  it('draws cards, or four columns of cards for the board', () => {
    expect(mount(SkeletonLoader, { props: { label: 'x', variant: 'cards', count: 2 } })
      .findAll('.skeleton-card')).toHaveLength(2)
    const board = mount(SkeletonLoader, { props: { label: 'x', variant: 'board', count: 3 } })
    expect(board.findAll('.skeleton-column')).toHaveLength(4)
    expect(board.findAll('.skeleton-card').length).toBeGreaterThan(4)
  })

  it('draws nodes and links for the Memory Map, hidden behind the one status', () => {
    const graph = mount(SkeletonLoader, { props: { label: 'Loading vault graph', variant: 'graph' } })
    expect(graph.get('.skeleton').attributes('aria-label')).toBe('Loading vault graph')
    const drawing = graph.get('.skeleton-graph-wrap')
    expect(drawing.attributes('aria-hidden')).toBe('true')
    expect(drawing.findAll('circle.skeleton-node').length).toBeGreaterThan(5)
    expect(drawing.findAll('line.skeleton-edge').length).toBeGreaterThan(5)
  })
})
