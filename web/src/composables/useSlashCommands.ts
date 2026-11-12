import { computed, nextTick, ref, watch, type ComputedRef, type Ref } from 'vue'
import { api } from '../lib/api'
import type { CommandsResponse, SlashCommand } from '../lib/types'

export interface SlashCommandTrigger {
  start: number
  end: number
  query: string
}

export interface UseSlashCommandsOptions {
  draft: Ref<string>
  input: Ref<HTMLTextAreaElement | undefined>
}

/** Find a slash token immediately before the textarea caret. */
export function findSlashCommandTrigger(text: string, cursor: number): SlashCommandTrigger | null {
  const end = Math.max(0, Math.min(cursor, text.length))
  const beforeCaret = text.slice(0, end)
  const match = beforeCaret.match(/(?:^|\s)\/([^\s/]*)$/)
  if (!match) return null

  const start = end - match[0].length + match[0].lastIndexOf('/')
  return { start, end, query: match[1] || '' }
}

/**
 * Cursor-aware slash-command menu state for a plain textarea composer.
 *
 * Both composers share it: the chat's draft is persisted by useChatComposer,
 * Home's is local. The selected value is plain text — the provider CLI parses
 * `/name` from the first message.
 */
export function useSlashCommands(options: UseSlashCommandsOptions): {
  commands: Ref<SlashCommand[]>
  trigger: Ref<SlashCommandTrigger | null>
  highlight: Ref<number>
  matches: ComputedRef<SlashCommand[]>
  visible: ComputedRef<boolean>
  load(provider: string, workspace: string): Promise<void>
  refresh(): void
  dismiss(): void
  move(delta: number): void
  current(): SlashCommand | null
  insert(cmd: SlashCommand, after?: () => void): void
} {
  const commands = ref<SlashCommand[]>([])
  const trigger = ref<SlashCommandTrigger | null>(null)
  const highlight = ref(0)

  const matches = computed<SlashCommand[]>(() => {
    const active = trigger.value
    if (!active) return []
    const needle = active.query.toLowerCase()
    return commands.value.filter(command =>
      command.name.toLowerCase().startsWith(needle),
    )
  })
  const visible = computed(() => matches.value.length > 0)

  watch(matches, (list) => {
    if (highlight.value >= list.length) highlight.value = 0
  })

  async function load(provider: string, workspace: string): Promise<void> {
    try {
      const response = await api.get<CommandsResponse>(
        `/api/commands?provider=${encodeURIComponent(provider)}&workspace=${encodeURIComponent(workspace)}`,
      )
      commands.value = [
        ...(response.commands || []),
        ...(response.skills || []),
      ]
    } catch {
      // Asset discovery is offline: leave the picker empty rather than stale.
      commands.value = []
    }
  }

  function dismiss(): void {
    trigger.value = null
    highlight.value = 0
  }

  function refresh(): void {
    const el = options.input.value
    if (!el || el.selectionStart !== el.selectionEnd) {
      dismiss()
      return
    }
    trigger.value = findSlashCommandTrigger(options.draft.value, el.selectionStart)
    highlight.value = 0
  }

  function move(delta: number): void {
    const n = matches.value.length
    if (!n) return
    highlight.value = (highlight.value + delta + n) % n
  }

  function current(): SlashCommand | null {
    return matches.value[highlight.value] ?? null
  }

  function insert(cmd: SlashCommand, after?: () => void): void {
    const active = trigger.value
    if (!active) return

    const before = options.draft.value.slice(0, active.start)
    const rest = options.draft.value.slice(active.end)
    const token = `/${cmd.name}`
    // Keep the existing separator when this replaces a token in the middle of
    // a draft; otherwise leave room for command arguments.
    const suffix = cmd.argument_hint && !(rest && /^\s/.test(rest)) ? ' ' : ''
    options.draft.value = before + token + suffix + rest
    const cursor = before.length + token.length + suffix.length
    dismiss()

    nextTick(() => {
      const input = options.input.value
      if (!input) return
      input.setSelectionRange(cursor, cursor)
      input.focus()
      after?.()
    })
  }

  // Programmatic draft changes (send, chat switch, shared content) must not
  // leave a stale menu anchored to the old token.
  watch(options.draft, () => {
    if (!trigger.value) return
    const el = options.input.value
    const found = el && el.selectionStart === el.selectionEnd
      ? findSlashCommandTrigger(options.draft.value, el.selectionStart)
      : null
    if (found) trigger.value = found
    else dismiss()
  })

  return {
    commands,
    trigger,
    highlight,
    matches,
    visible,
    load,
    refresh,
    dismiss,
    move,
    current,
    insert,
  }
}
