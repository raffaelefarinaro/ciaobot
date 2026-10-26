// @vitest-environment jsdom

import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import WorkspaceTile from '../WorkspaceTile.vue'
import { useProjectStore } from '../../stores/projects'
import type { WorkspaceInfo } from '../../lib/types'

function info(name: string, color?: string): WorkspaceInfo {
  return {
    name,
    vault_root: `${name}/memory-vault`,
    default_provider: 'claude',
    gws_profile: '',
    color,
  } as WorkspaceInfo
}

describe('WorkspaceTile', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('shows the 1-9 key in the workspace colour', () => {
    const store = useProjectStore()
    store.workspaces = [info('personal'), info('work', 'violet')]
    store.activeWorkspace = 'personal'
    const tile = mount(WorkspaceTile, { props: { name: 'work' } }).get('.workspace-tile')
    expect(tile.text()).toBe('2')
    expect(tile.attributes('data-workspace-color')).toBe('violet')
    expect(tile.attributes('aria-hidden')).toBe('true')
  })

  it('shows the initial for a workspace past the ninth', () => {
    const store = useProjectStore()
    store.workspaces = Array.from({ length: 10 }, (_, i) => info(i === 9 ? 'tenth-one' : `ws${i}`))
    const tile = mount(WorkspaceTile, { props: { name: 'tenth-one' } }).get('.workspace-tile')
    expect(tile.text()).toBe('T')
  })
})
