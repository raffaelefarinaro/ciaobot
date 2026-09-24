// @vitest-environment jsdom

import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { computed } from 'vue'
import HostStatusPill from '../HostStatusPill.vue'
import { CONNECTION_ROLE_KEY, type ConnectionRole } from '../../lib/connectionRole'

function mountWith(role: ConnectionRole) {
  return mount(HostStatusPill, {
    global: { provide: { [CONNECTION_ROLE_KEY as symbol]: computed(() => role) } },
  })
}

describe('HostStatusPill', () => {
  it('restates the connection role in the banner wording', () => {
    expect(mountWith({ kind: 'host' }).text()).toBe('Host')
    expect(mountWith({ kind: 'client', hostLabel: 'studio.local:8443', reachable: true }).text())
      .toBe('Client · studio.local:8443')
    expect(mountWith({ kind: 'client', hostLabel: 'studio.local:8443', reachable: false }).text())
      .toBe('Reconnecting…')
    expect(mountWith({ kind: 'unknown' }).text()).toBe('Role unavailable')
  })

  it('renders nothing without a provider', () => {
    expect(mount(HostStatusPill).find('.host-status-pill').exists()).toBe(false)
  })
})
