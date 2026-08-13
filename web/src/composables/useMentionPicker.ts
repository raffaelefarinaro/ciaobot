import { computed, nextTick, ref, toValue, watch, type MaybeRefOrGetter, type Ref } from 'vue'

export type MentionKind = 'file' | 'agent' | 'chat' | 'project'

export interface MentionFile {
  path: string
  vault_path: string
}

export interface MentionAgent {
  name: string
  description?: string
}

export interface MentionChat {
  chat_id: string
  title: string
  project_id: string
  project_name?: string
  workspace?: string
  archived?: boolean
  local?: boolean
}

export interface MentionProject {
  project_id: string
  name: string
  workspace?: string
  archived?: boolean
  completed?: boolean
}

export interface MentionItem {
  kind: MentionKind
  label: string
  insertText: string
  description: string
}

export interface MentionTrigger {
  start: number
  end: number
  query: string
}

const MAX_MENTION_RESULTS = 50

/** Find an @ token immediately before the textarea caret. */
export function findMentionTrigger(text: string, cursor: number): MentionTrigger | null {
  const end = Math.max(0, Math.min(cursor, text.length))
  const beforeCaret = text.slice(0, end)
  const match = beforeCaret.match(/(?:^|\s)@([^\s@]*)$/)
  if (!match) return null

  const matchStart = end - match[0].length
  const at = beforeCaret.indexOf('@', matchStart)
  return at < 0 ? null : { start: at, end, query: match[1] || '' }
}

/** Build the two supported mention sources from existing API response shapes. */
export function buildMentionItems(
  files: MentionFile[],
  agents: MentionAgent[],
  chats: MentionChat[] = [],
  projects: MentionProject[] = [],
): MentionItem[] {
  const items: MentionItem[] = []
  const seen = new Set<string>()

  for (const agent of agents) {
    const name = agent.name.trim()
    if (!name) continue
    const key = `agent:${name.toLowerCase()}`
    if (seen.has(key)) continue
    seen.add(key)
    items.push({
      kind: 'agent',
      label: name,
      insertText: name,
      description: agent.description?.trim() || 'Named agent',
    })
  }

  for (const chat of chats) {
    const title = chat.title.trim()
    const chatId = chat.chat_id.trim()
    if (!title || !chatId || chat.archived || chat.local === false) continue
    const key = `chat:${chatId.toLowerCase()}`
    if (seen.has(key)) continue
    seen.add(key)
    const context = [chat.project_name?.trim(), chat.workspace?.trim()].filter(Boolean)
    items.push({
      kind: 'chat',
      label: title,
      insertText: `chat/${chatId}`,
      description: context.length ? `Chat · ${context.join(' · ')}` : 'Chat',
    })
  }

  for (const project of projects) {
    const name = project.name.trim()
    const projectId = project.project_id.trim()
    if (!name || !projectId || project.archived || project.completed) continue
    const key = `project:${projectId.toLowerCase()}`
    if (seen.has(key)) continue
    seen.add(key)
    const workspace = project.workspace?.trim()
    items.push({
      kind: 'project',
      label: name,
      insertText: `project/${projectId}`,
      description: workspace ? `Project · ${workspace}` : 'Project',
    })
  }

  for (const file of files) {
    const vaultPath = file.vault_path.trim()
    if (!vaultPath) continue
    const key = `file:${vaultPath.toLowerCase()}`
    if (seen.has(key)) continue
    seen.add(key)
    items.push({
      kind: 'file',
      label: vaultPath,
      insertText: vaultPath,
      description: file.path.trim() || 'Vault file',
    })
  }

  return items
}

export function filterMentionItems(items: MentionItem[], query: string): MentionItem[] {
  const needle = query.trim().toLocaleLowerCase()
  if (!needle) return items.slice(0, MAX_MENTION_RESULTS)
  return items
    .filter(item =>
      item.label.toLocaleLowerCase().includes(needle)
      || item.insertText.toLocaleLowerCase().includes(needle)
      || item.description.toLocaleLowerCase().includes(needle),
    )
    .slice(0, MAX_MENTION_RESULTS)
}

export interface UseMentionPickerOptions {
  draft: Ref<string>
  input: Ref<HTMLTextAreaElement | undefined>
  files: MaybeRefOrGetter<MentionFile[]>
  agents: MaybeRefOrGetter<MentionAgent[]>
  chats?: MaybeRefOrGetter<MentionChat[]>
  projects?: MaybeRefOrGetter<MentionProject[]>
}

/**
 * Cursor-aware mention menu state for the plain textarea composer.
 *
 * The selected value is deliberately plain text: the backend receives the
 * same vault path or named-agent token that the existing context surfaces use.
 */
export function useMentionPicker(options: UseMentionPickerOptions) {
  const trigger = ref<MentionTrigger | null>(null)
  const highlightIndex = ref(0)

  const allItems = computed(() => buildMentionItems(
    toValue(options.files),
    toValue(options.agents),
    toValue(options.chats) || [],
    toValue(options.projects) || [],
  ))
  const filteredItems = computed(() => {
    const active = trigger.value
    return active ? filterMentionItems(allItems.value, active.query) : []
  })
  const showPicker = computed(() => Boolean(trigger.value && filteredItems.value.length))

  watch(filteredItems, (items) => {
    if (highlightIndex.value >= items.length) highlightIndex.value = 0
  })

  function refresh(): void {
    const el = options.input.value
    if (!el || el.selectionStart !== el.selectionEnd) {
      trigger.value = null
      return
    }
    trigger.value = findMentionTrigger(options.draft.value, el.selectionStart)
    highlightIndex.value = 0
  }

  function dismiss(): void {
    trigger.value = null
    highlightIndex.value = 0
  }

  function select(item: MentionItem): void {
    const active = trigger.value
    const el = options.input.value
    if (!active || !el) return

    const before = options.draft.value.slice(0, active.start)
    const after = options.draft.value.slice(active.end)
    const token = `@${item.insertText}`
    // Leave the existing separator alone when inserting before whitespace;
    // otherwise keep the caret ready for the next word.
    const suffix = after && /^\s/.test(after) ? '' : ' '
    options.draft.value = before + token + suffix + after
    dismiss()

    nextTick(() => {
      const input = options.input.value
      if (!input) return
      input.value = options.draft.value
      const caret = active.start + token.length + suffix.length
      input.setSelectionRange(caret, caret)
      input.focus()
    })
  }

  /** Return true when the mention picker consumed the keyboard event. */
  function handleKeydown(event: KeyboardEvent): boolean {
    if (!showPicker.value) return false
    const items = filteredItems.value
    if (!items.length) return false

    if (event.key === 'ArrowDown') {
      event.preventDefault()
      highlightIndex.value = (highlightIndex.value + 1) % items.length
      return true
    }
    if (event.key === 'ArrowUp') {
      event.preventDefault()
      highlightIndex.value = (highlightIndex.value - 1 + items.length) % items.length
      return true
    }
    if (event.key === 'Escape') {
      event.preventDefault()
      event.stopPropagation()
      dismiss()
      return true
    }
    if (event.key === 'Tab' || (event.key === 'Enter' && !event.shiftKey && !event.metaKey && !event.ctrlKey)) {
      event.preventDefault()
      select(items[highlightIndex.value])
      return true
    }
    return false
  }

  // Programmatic draft changes (send, slash-command selection, chat switch)
  // must not leave a stale menu anchored to the old token.
  watch(options.draft, () => {
    if (!trigger.value) return
    const el = options.input.value
    const current = el && el.selectionStart === el.selectionEnd
      ? findMentionTrigger(options.draft.value, el.selectionStart)
      : null
    if (!current) dismiss()
  })

  return {
    filteredItems,
    highlightIndex,
    showPicker,
    refresh,
    dismiss,
    select,
    handleKeydown,
  }
}
