// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest'
import { nextTick, ref, watch } from 'vue'
import { api } from '../../lib/api'
import type { SlashCommand } from '../../lib/types'
import { useSlashCommands } from '../useSlashCommands'

function command(name: string, argument_hint = ''): SlashCommand {
  return { name, description: `run ${name}`, argument_hint, source: 'user', path: `${name}.md` }
}

function setup(initial = '') {
  const input = document.createElement('textarea')
  document.body.appendChild(input)
  input.value = initial
  const draft = ref(initial)
  // Stand in for the composers' v-model: the textarea's value tracks the draft
  // before insert() positions the caret in its nextTick callback.
  watch(draft, value => { input.value = value }, { flush: 'sync' })
  const slash = useSlashCommands({ draft, input: ref<HTMLTextAreaElement | undefined>(input) })
  return { input, draft, slash }
}

function moveCaret(input: HTMLTextAreaElement, text: string, at: number): void {
  input.value = text
  input.setSelectionRange(at, at)
}

describe('useSlashCommands', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    document.body.innerHTML = ''
  })

  it('matches a slash token at the start or after whitespace', () => {
    const { input, draft, slash } = setup('/rev')
    moveCaret(input, '/rev', 4)
    slash.refresh()
    expect(slash.trigger.value).toEqual({ start: 0, end: 4, query: 'rev' })

    draft.value = 'then run /rev'
    moveCaret(input, draft.value, draft.value.length)
    slash.refresh()
    expect(slash.trigger.value).toEqual({
      start: 'then run /rev'.indexOf('/'),
      end: 'then run /rev'.length,
      query: 'rev',
    })

    // A space after the slash ends the token, so the picker closes.
    draft.value = '/run now'
    moveCaret(input, draft.value, draft.value.length)
    slash.refresh()
    expect(slash.trigger.value).toBeNull()
  })

  it('inserts the command and keeps text after the token', async () => {
    const get = vi.spyOn(api, 'get').mockResolvedValue({
      commands: [command('review', 'file')],
      skills: [],
    } as never)
    const { input, draft, slash } = setup('/rev')
    await slash.load('claude', 'personal')
    expect(get).toHaveBeenCalledWith('/api/commands?provider=claude&workspace=personal')

    moveCaret(input, '/rev', 4)
    slash.refresh()
    slash.insert(command('review', 'file'))
    await nextTick()
    // No text after the token, and the command takes an argument: add one space.
    expect(draft.value).toBe('/review ')
    expect(input.selectionStart).toBe(8)

    // Existing whitespace after the token is the separator already.
    draft.value = 'run /rev tail'
    moveCaret(input, 'run /rev tail', 8)
    slash.refresh()
    slash.insert(command('review', 'file'))
    await nextTick()
    expect(draft.value).toBe('run /review tail')

    // A command that takes no argument gets no trailing space.
    draft.value = '/rev'
    moveCaret(input, '/rev', 4)
    slash.refresh()
    slash.insert(command('review'))
    await nextTick()
    expect(draft.value).toBe('/review')
  })

  it('resets the highlight when the match list shrinks', async () => {
    const get = vi.spyOn(api, 'get')
    get.mockResolvedValueOnce({
      commands: [command('review'), command('releases'), command('run')],
      skills: [],
    } as never)
    const { input, slash } = setup('/')
    moveCaret(input, '/', 1)
    await slash.load('claude', 'personal')
    slash.refresh()
    expect(slash.matches.value).toHaveLength(3)

    slash.move(2)
    expect(slash.highlight.value).toBe(2)

    get.mockResolvedValueOnce({ commands: [command('review')], skills: [] } as never)
    await slash.load('claude', 'personal')
    await nextTick()
    expect(slash.matches.value).toHaveLength(1)
    expect(slash.highlight.value).toBe(0)
  })
})
