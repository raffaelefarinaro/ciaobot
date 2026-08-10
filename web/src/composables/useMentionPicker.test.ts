// @vitest-environment jsdom

import { describe, expect, it } from 'vitest'
import { nextTick, ref } from 'vue'
import {
  buildMentionItems,
  filterMentionItems,
  findMentionTrigger,
  useMentionPicker,
} from './useMentionPicker'

function makeTextarea(value: string, cursor = value.length): HTMLTextAreaElement {
  const textarea = document.createElement('textarea')
  textarea.value = value
  textarea.selectionStart = cursor
  textarea.selectionEnd = cursor
  return textarea
}

describe('useMentionPicker', () => {
  it('finds a token at the caret without treating email addresses as mentions', () => {
    const pathDraft = 'read @memory-vault/personal/README.md'
    expect(findMentionTrigger(pathDraft, pathDraft.length)).toEqual({
      start: 5,
      end: pathDraft.length,
      query: 'memory-vault/personal/README.md',
    })
    const emailDraft = 'mail me@example.com'
    expect(findMentionTrigger(emailDraft, emailDraft.length)).toBeNull()
    expect(findMentionTrigger('before@agent', 12)).toBeNull()
  })

  it('filters agents and vault files together and deduplicates sources', () => {
    const items = buildMentionItems(
      [
        { path: 'README.md', vault_path: 'memory-vault/personal/README.md' },
        { path: 'README.md', vault_path: 'memory-vault/personal/README.md' },
      ],
      [
        { name: 'researcher', description: 'Find evidence' },
        { name: 'Researcher', description: 'Duplicate name' },
      ],
    )

    expect(filterMentionItems(items, 'read')).toEqual([
      expect.objectContaining({ kind: 'file', insertText: 'memory-vault/personal/README.md' }),
    ])
    expect(filterMentionItems(items, 'RESEARCH')).toEqual([
      expect.objectContaining({ kind: 'agent', insertText: 'researcher' }),
    ])
    expect(items).toHaveLength(2)
  })

  it('inserts plain mention text and restores the caret after the token', async () => {
    const draft = ref('Review @read')
    const input = ref<HTMLTextAreaElement | undefined>(makeTextarea(draft.value))
    const picker = useMentionPicker({
      draft,
      input,
      files: [{ path: 'README.md', vault_path: 'memory-vault/personal/README.md' }],
      agents: [{ name: 'researcher', description: 'Find evidence' }],
    })

    picker.refresh()
    expect(picker.filteredItems.value[0]?.insertText).toBe('memory-vault/personal/README.md')
    picker.select(picker.filteredItems.value[0])
    await nextTick()

    expect(draft.value).toBe('Review @memory-vault/personal/README.md ')
    expect(input.value?.selectionStart).toBe(draft.value.length)
    expect(input.value?.selectionEnd).toBe(draft.value.length)
    expect(picker.showPicker.value).toBe(false)
  })

  it('keeps the menu session local to the active token and handles Escape', () => {
    const draft = ref('@res')
    const input = ref<HTMLTextAreaElement | undefined>(makeTextarea(draft.value))
    const picker = useMentionPicker({ draft, input, files: [], agents: [{ name: 'researcher' }] })
    picker.refresh()
    expect(picker.showPicker.value).toBe(true)

    const event = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })
    picker.handleKeydown(event)
    expect(picker.showPicker.value).toBe(false)
    expect(draft.value).toBe('@res')
  })
})
