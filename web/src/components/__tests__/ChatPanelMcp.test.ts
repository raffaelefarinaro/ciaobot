// @vitest-environment jsdom

/**
 * Chat details MCP section: one button that runs the chat's own agent CLI MCP
 * command in the in-app terminal. The command comes from the provider row.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount, RouterLinkStub, type VueWrapper } from '@vue/test-utils'
import { ref } from 'vue'
import { useProjectStore } from '../../stores/projects'
import type { ChatInfo } from '../../lib/types'

// The store's onMounted fetches (models, commands, loops, schedules) all
// swallow errors, so a rejecting api keeps the mount path quiet while the
// card is driven purely from store state.
const apiGet = vi.hoisted(() => vi.fn(() => Promise.reject(new Error('no server'))))
const apiPost = vi.hoisted(() => vi.fn(() => Promise.resolve({})))
const apiPatch = vi.hoisted(() => vi.fn(() => Promise.resolve({})))
const apiDel = vi.hoisted(() => vi.fn(() => Promise.resolve({})))
const writeClipboard = vi.hoisted(() => vi.fn(async (_text: string) => true))

const runMock = vi.hoisted(() => vi.fn())
vi.mock('../../composables/useRunInTerminal', () => ({
  useRunInTerminal: () => ({ run: runMock, busy: ref(false) }),
}))

vi.mock('../../lib/api', () => ({
  api: { get: apiGet, post: apiPost, patch: apiPatch, del: apiDel, postForm: vi.fn() },
}))

vi.mock('../../lib/codeCopy', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../lib/codeCopy')>()
  return { ...actual, writeClipboard }
})

vi.mock('../../router', () => ({
  router: {
    push: vi.fn(),
    currentRoute: { value: { params: {} } },
  },
}))

// Heavy or irrelevant children are stubbed at the module level: Vue SFCs
// import siblings directly through ESM, which bypasses `config.global.stubs`.
const NoopStub = vi.hoisted(() => ({ name: 'NoopStub', render: () => null }))
const CommentPopoverStub = vi.hoisted(() => ({
  name: 'CommentPopoverStub',
  render: () => null,
  methods: {
    close: () => {},
    clearPendingClose: () => {},
    onTargetOver: () => {},
    onTargetOut: () => {},
    pinFromEvent: () => null,
    show: () => {},
  },
}))
vi.mock('../PaneHeader.vue', () => ({ default: { template: '<header><slot name="title" /><slot name="actions" /></header>' } }))
vi.mock('../SubagentPanel.vue', () => ({ default: NoopStub }))
vi.mock('../ChatCommentPopover.vue', () => ({ default: CommentPopoverStub }))
vi.mock('../CommentComposePopover.vue', () => ({ default: NoopStub }))

import ChatPanel from '../ChatPanel.vue'

const CHAT_ID = 'chat-1'

function makeChat(overrides: Partial<ChatInfo> = {}): ChatInfo {
  return {
    chat_id: CHAT_ID,
    project_id: 'proj-1',
    title: 'a chat',
    model: 'synthetic-model',
    provider: 'claude',
    mode: 'normal',
    session_id: 'sess-1',
    created_at: '2026-08-01T00:00:00Z',
    archived: false,
    ...overrides,
  } as ChatInfo
}

let wrapper: VueWrapper | null = null

async function mountPanel(chatId = CHAT_ID, existing?: ReturnType<typeof createPinia>) {
  // One store for a remount: production keys the panel by chat id, so the
  // store outlives the panel and the new one sees the old one's state.
  const pinia = existing ?? createPinia()
  setActivePinia(pinia)
  const store = useProjectStore()
  store.chats = [makeChat({ chat_id: chatId })]
  store.projects = [{ project_id: 'proj-1', name: 'Test', workspace: 'personal' }] as never
  store.activeChatId = chatId
  store.messages = { [chatId]: [] }
  store.bootstrapped = true
  // Attached to the document: the card hands the focus to the composer when
  // it goes, and focus only exists in a document.
  wrapper = mount(ChatPanel, {
      props: { chatId: chatId },
    attachTo: document.body,
    global: { plugins: [pinia], stubs: { RouterLink: RouterLinkStub } },
  })
  await flushPromises()
  return { wrapper, store, pinia }
}

const PROVIDERS = {
  connections: {
    claude: { name: 'claude', label: 'Claude Code', ok: true, auth: 'ok', command: 'claude auth login', mcp_command: 'claude mcp' },
    opencode: { name: 'opencode', label: 'OpenCode', ok: true, auth: 'ok', command: 'opencode auth login', mcp_command: 'opencode mcp' },
  },
}

/** Every other read rejects, like the sibling panel tests: only the provider list is answered. */
function answerProviders(reply: () => Promise<unknown>) {
  apiGet.mockImplementation(((path: string) => path === '/api/settings/providers'
    ? reply()
    : Promise.reject(new Error('no server'))) as never)
}

beforeEach(() => {
  vi.clearAllMocks()
  runMock.mockReset().mockResolvedValue('typed')
  answerProviders(() => Promise.resolve(PROVIDERS))
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
})

async function openDetails(panel: VueWrapper) {
  await panel.get('[title="Show chat details"]').trigger('click')
  await flushPromises()
}

describe('ChatPanel Chat details MCP', () => {
  it.each([
    ['claude', 'Open MCP settings in Claude Code', 'claude mcp'],
    ['opencode', 'Open MCP settings in OpenCode', 'opencode mcp'],
  ] as const)('a %s chat gets its own agent button and runs its own command', async (provider, label, command) => {
    const { wrapper: panel, store } = await mountPanel()
    store.chats = [makeChat({ provider })]
    await flushPromises()
    await openDetails(panel)
    const button = panel.findAll('.chat-rail-mcp button').find(b => b.text() === label)
    expect(button).toBeDefined()
    await button!.trigger('click')
    await flushPromises()
    expect(runMock).toHaveBeenCalledWith(command, { chatId: CHAT_ID })
  })

  it('reads the provider list once, only after Chat details opens', async () => {
    const { wrapper: panel } = await mountPanel()
    expect(apiGet).not.toHaveBeenCalledWith('/api/settings/providers')
    await openDetails(panel)
    await panel.get('[aria-label="Hide chat details"], .chat-rail-head button').trigger('click')
    await openDetails(panel)
    expect(apiGet.mock.calls.filter(c => (c as unknown[])[0] === '/api/settings/providers')).toHaveLength(1)
  })

  it('says why when the provider list cannot be read', async () => {
    answerProviders(() => Promise.reject(new Error('offline')))
    const { wrapper: panel } = await mountPanel()
    await openDetails(panel)
    expect(panel.get('.chat-rail-mcp [role="alert"]').text()).toContain('offline')
    expect(panel.find('.chat-rail-mcp button').exists()).toBe(false)
  })

  it('shows a reason, not a button, when the agent has no MCP command', async () => {
    answerProviders(() => Promise.resolve({ connections: { claude: { ...PROVIDERS.connections.claude, mcp_command: undefined } } }))
    const { wrapper: panel } = await mountPanel()
    await openDetails(panel)
    expect(panel.find('.chat-rail-mcp button').exists()).toBe(false)
    expect(panel.get('.chat-rail-mcp').text()).toContain("No MCP command is available for this chat's agent.")
  })
})
