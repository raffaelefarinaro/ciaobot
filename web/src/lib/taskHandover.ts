/**
 * The first message of a delegated chat, read back as what was handed over.
 *
 * `ciao/task_attempts.py::build_prompt` writes that message for the agent: an
 * instruction, the record's fields (`Title:`, `Status:`, `Task id:` …), then the
 * task's description fenced between `<task-board-task>` tags. Shown verbatim it
 * reads as a wall of machine framing, so the transcript draws it as a card —
 * the title and the description — and keeps the raw prompt one disclosure away.
 *
 * The instructions the user typed into the delegation sheet ride in the same
 * message, quoted between their own tags after the description. They are what the
 * user is looking for in the card, so they are read out too. The engine escapes
 * both fence tokens inside user text (`_neutralize`), so the first instructions
 * open tag after the description's close is the real one and the first close tag
 * after it ends it.
 *
 * Parsed defensively: a message without both task fences is not one the engine
 * built, and the caller renders it as an ordinary message. A missing or broken
 * instructions fence only means no instructions are drawn; the raw prompt still
 * carries whatever the message held.
 */

export const TASK_FENCE_OPEN = '<task-board-task>'
export const TASK_FENCE_CLOSE = '</task-board-task>'
export const INSTRUCTIONS_FENCE_OPEN = '<delegation-instructions>'
export const INSTRUCTIONS_FENCE_CLOSE = '</delegation-instructions>'

export interface TaskHandover {
  /** The `Title:` line, or `''` when the header has none. */
  title: string
  /** The Markdown between the fences, trimmed. */
  description: string
  /** The user's hand-over instructions between their fence, trimmed; `''` when there are none. */
  instructions: string
}

/**
 * The instructions fence after `from`, which only counts when each tag stands on
 * its own line, as the engine writes it. A tag in the middle of a line is prose.
 */
function readInstructions(content: string, from: number): string {
  const open = content.indexOf(`\n${INSTRUCTIONS_FENCE_OPEN}\n`, from)
  if (open < 0) return ''
  const start = open + INSTRUCTIONS_FENCE_OPEN.length + 2
  const closeAt = content.indexOf(`\n${INSTRUCTIONS_FENCE_CLOSE}`, start - 1)
  if (closeAt < 0) return ''
  const after = content.charAt(closeAt + INSTRUCTIONS_FENCE_CLOSE.length + 1)
  if (after !== '' && after !== '\n') return ''
  return content.slice(start, closeAt + 1).trim()
}

export function parseTaskHandover(content: string | null | undefined): TaskHandover | null {
  if (!content) return null
  const open = content.indexOf(TASK_FENCE_OPEN)
  if (open < 0) return null
  const close = content.indexOf(TASK_FENCE_CLOSE, open + TASK_FENCE_OPEN.length)
  if (close < 0) return null
  const description = content.slice(open + TASK_FENCE_OPEN.length, close).trim()
  // Only the header above the fence names the record; a `Title:` line inside
  // the description is the user's prose.
  const titleLine = content
    .slice(0, open)
    .split('\n')
    .find((line) => line.startsWith('Title: '))
  const title = titleLine ? titleLine.slice('Title: '.length).trim() : ''
  return {
    title,
    description,
    instructions: readInstructions(content, close + TASK_FENCE_CLOSE.length),
  }
}
