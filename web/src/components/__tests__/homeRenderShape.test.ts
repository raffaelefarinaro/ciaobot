// @vitest-environment jsdom

/**
 * Renders the home lanes and asserts the *shape* of what a user sees, as a
 * readable snapshot of the reviewed feedback:
 *
 *   - older chats listed inline with quiet, no separate section or disclosure
 *   - the needs-you tier absent entirely when nothing needs the user
 *   - tier labels lowercase
 *
 * The per-behaviour assertions live in HomeRecentChats.test.ts; this exists so a
 * regression shows up as a diff of the visible text rather than a failed
 * selector, which is much faster to read.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { useProjectStore } from '../../stores/projects'

function timestamp(secondsAgo: number): string {
  return new Date(Date.now() - secondsAgo * 1000).toISOString()
}

function seed(withNeedsYou: boolean) {
  const store = useProjectStore()
  store.workspaces = [
    { name: 'personal', vault_root: '', default_provider: 'claude', default_model: '', gws_profile: '', color: 'emerald' },
  ] as unknown as typeof store.workspaces
  store.projects = [
    { project_id: 'p1', name: 'Wedding', workspace: 'personal' },
    { project_id: 'general', name: 'General', workspace: 'personal' },
  ] as unknown as typeof store.projects
  store.chats = [
    ...(withNeedsYou
      ? [{
          chat_id: 'needs', project_id: 'p1', title: 'Needs an answer',
          pending_question: JSON.stringify({ questions: [{ question: 'Which date?' }] }),
          created_at: timestamp(60), last_activity_at: timestamp(60), last_read_at: timestamp(60), archived: false, local: true,
        }]
      : []),
    {
      chat_id: 'quiet', project_id: 'p1', title: 'A quiet chat',
      created_at: timestamp(2 * 86400), last_activity_at: timestamp(2 * 86400), last_read_at: timestamp(2 * 86400), archived: false, local: true,
    },
    {
      chat_id: 'old', project_id: 'p1', title: 'A three week old chat',
      created_at: timestamp(21 * 86400), last_activity_at: timestamp(21 * 86400), last_read_at: timestamp(21 * 86400), archived: false, local: true,
    },
  ] as unknown as typeof store.chats
  store.activeWorkspace = 'personal'
  store.bootstrapped = true
  store.projectStreaming = {}
  store.backgroundAgents = {}
  return store
}

async function render(withNeedsYou: boolean): Promise<string[]> {
  seed(withNeedsYou)
  const { default: HomeRecentChats } = await import('../HomeRecentChats.vue')
  const wrapper = mount(HomeRecentChats)
  await nextTick()
  const lines = [
    ...wrapper.findAll('.home-tier').map(tier => {
      const label = tier.find('.home-tier-label').text()
      const rows = tier.findAll('.home-chat-title').map(t => t.text())
      return `${label}: ${rows.join(' | ')}`
    }),
  ]
  wrapper.unmount()
  return lines
}

describe('home lane rendered shape', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.restoreAllMocks()
    if (!Element.prototype.scrollIntoView) Element.prototype.scrollIntoView = () => {}
  })

  it('lists a three-week-old chat in quiet, with no older section', async () => {
    expect(await render(true)).toEqual([
      'needs you: Needs an answer',
      'quiet: A quiet chat | A three week old chat',
    ])
  })

  it('omits the needs-you tier when nothing needs the user', async () => {
    expect(await render(false)).toEqual([
      'quiet: A quiet chat | A three week old chat',
    ])
  })

  // Reported from live use: a chat with an unread message was being listed
  // under "quiet" while the sidebar showed an unread badge for it and the bell
  // showed a count. The heading contradicted the rest of the screen.
  it('does not call an unread chat quiet', async () => {
    const store = seed(false)
    // Same shape the server leaves behind: activity newer than the last read.
    store.chats[0].last_read_at = new Date(Date.parse(store.chats[0].last_activity_at!) - 1000).toISOString()
    const { default: HomeRecentChats } = await import('../HomeRecentChats.vue')
    const wrapper = mount(HomeRecentChats)
    await nextTick()

    const tiers = wrapper.findAll('.home-tier').map(tier => ({
      label: tier.find('.home-tier-label').text(),
      rows: tier.findAll('.home-chat-title').map(t => t.text()),
    }))
    const unread = tiers.find(t => t.label === 'unread')
    const quiet = tiers.find(t => t.label === 'quiet')

    expect(unread?.rows).toContain('A quiet chat')
    expect(quiet?.rows ?? []).not.toContain('A quiet chat')
    wrapper.unmount()
  })
})
