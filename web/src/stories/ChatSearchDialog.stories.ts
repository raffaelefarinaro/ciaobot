import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, screen, userEvent, waitFor } from 'storybook/test'
import { defineComponent, h, ref } from 'vue'
import ChatSearchDialog from '../components/ChatSearchDialog.vue'
import { json, route } from './settingsAccessFixture'

function renderSearchDialog() {
  return defineComponent({
    setup() {
      const open = ref(false)
      return () => h('div', [
        h('button', { type: 'button', onClick: () => { open.value = true } }, 'Search chats'),
        h(ChatSearchDialog, {
          open: open.value,
          workspace: 'storybook-alpha',
          onClose: () => { open.value = false },
          onSelect: () => { open.value = false },
        }),
      ])
    },
  })
}

const meta = {
  id: 'chat-search-dialog',
  title: 'Chat/Search chats dialog',
  tags: ['!autodocs'],
  parameters: { docs: { disable: true }, controls: { disable: true } },
} satisfies Meta
export default meta
type Story = StoryObj<typeof meta>

export const KeyboardDismissal: Story = {
  render: () => renderSearchDialog(),
  play: async () => {
    const opener = screen.getByRole('button', { name: 'Search chats' })
    await userEvent.click(opener)
    await waitFor(() => expect(screen.getByRole('dialog', { name: 'Search chats' })).toBeTruthy())
    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(document.activeElement).toBe(opener)
  },
}

const LONG_SNIPPET = '**Decision:** we shipped the [rollout plan](https://example.invalid/plan) after the review. '.repeat(6)
  + '1. Check the dashboards, then confirm the migration finished before the next window opens.'

const searchRoute = (items: Array<Record<string, unknown>>) => route('POST', '/api/chats/search', () => json({ items, next_cursor: null, skipped_large_transcripts: false }))

async function openAndSearch(query: string) {
  await userEvent.click(screen.getByRole('button', { name: 'Search chats' }))
  const field = await waitFor(() => screen.getByRole('combobox', { name: 'Title or conversation text' }))
  await userEvent.type(field, query)
  await waitFor(() => expect(screen.getAllByRole('option').length).toBeGreaterThan(0), { timeout: 3000 })
  return field
}

export const ArchivedWithProject: Story = {
  parameters: { fixture: { routes: [searchRoute([
    { chat_id: 'live', title: 'Launch checklist', project_title: 'Upwordo', archived: false, snippet: "**haven't** merged [PR #254](https://example.invalid/254)" },
    { chat_id: 'old', title: 'Launch retro', project_title: 'Upwordo', archived: true, snippet: 'launch notes' },
  ])] } },
  render: () => renderSearchDialog(),
  play: async () => {
    const field = await openAndSearch('launch')
    const options = screen.getAllByRole('option')
    expect(options[0]!.textContent).toContain('Upwordo')
    expect(options[0]!.textContent).not.toContain('Archived')
    expect(options[1]!.textContent).toContain('Upwordo')
    expect(options[1]!.textContent).toContain('Archived')
    expect(options[0]!.querySelector('.chat-search-snippet')?.textContent).toBe("haven't merged PR #254")
    await userEvent.keyboard('{ArrowDown}')
    expect(field.getAttribute('aria-activedescendant')).toBe(options[0]!.id)
  },
}

export const LongSnippet: Story = {
  parameters: { fixture: { routes: [searchRoute([
    { chat_id: 'long', title: 'Rollout plan', project_title: 'Upwordo', archived: true, snippet: LONG_SNIPPET },
  ])] } },
  render: () => renderSearchDialog(),
  play: async () => {
    await openAndSearch('rollout')
    const snippet = screen.getByRole('option').querySelector('.chat-search-snippet') as HTMLElement
    expect(snippet.textContent).not.toContain('**')
    expect(snippet.textContent).not.toContain('https://')
    expect(snippet.scrollHeight).toBeGreaterThan(snippet.clientHeight)
    expect(snippet.clientHeight).toBeLessThan(3 * parseFloat(getComputedStyle(snippet).lineHeight))
  },
}
