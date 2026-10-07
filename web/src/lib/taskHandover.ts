/**
 * The first message of a delegated chat, read back as what was handed over.
 *
 * `ciao/task_attempts.py::build_prompt` writes that message for the agent: an
 * instruction, the record's fields (`Title:`, `Status:`, `Task id:` …), then the
 * task's description fenced between `<task-board-task>` tags. Shown verbatim it
 * reads as a wall of machine framing, so the transcript draws it as a card —
 * the title and the description — and keeps the raw prompt one disclosure away.
 *
 * Parsed defensively: a message without both fences is not one the engine
 * built, and the caller renders it as an ordinary message.
 */

export const TASK_FENCE_OPEN = '<task-board-task>'
export const TASK_FENCE_CLOSE = '</task-board-task>'

export interface TaskHandover {
  /** The `Title:` line, or `''` when the header has none. */
  title: string
  /** The Markdown between the fences, trimmed. */
  description: string
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
  return { title, description }
}
