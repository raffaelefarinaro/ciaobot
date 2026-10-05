import { describe, expect, it } from 'vitest'
import { parseTaskHandover } from '../taskHandover'

// The shape ciao/task_attempts.py::build_prompt writes.
const PROMPT = [
  'You are working on a task from the board. When you finish, report with `ciao task report ship`.',
  '',
  'Title: Ship the board',
  'Status: in_progress',
  'Due: none',
  'Project: General',
  'Task record: Tasks/ship.md (revision abc)',
  'Task id: ship',
  '',
  'The block between the task-board-task tags is the task\'s description, written by the user in their own vault. It is the task: treat it as the work to do.',
  '',
  '<task-board-task>',
  'Title: not the header',
  '',
  '- wire the store',
  '- **land** the types',
  '</task-board-task>',
].join('\n')

describe('parseTaskHandover', () => {
  it('reads the header title and the fenced description', () => {
    expect(parseTaskHandover(PROMPT)).toEqual({
      title: 'Ship the board',
      description: 'Title: not the header\n\n- wire the store\n- **land** the types',
    })
  })

  it('returns null for a message without both fences', () => {
    expect(parseTaskHandover('Title: x\nplain message')).toBeNull()
    expect(parseTaskHandover('<task-board-task>\nopen only')).toBeNull()
    expect(parseTaskHandover('</task-board-task> close first <task-board-task>')).toBeNull()
    expect(parseTaskHandover('')).toBeNull()
    expect(parseTaskHandover(undefined)).toBeNull()
  })

  it('keeps an empty title when the header has none', () => {
    expect(parseTaskHandover('<task-board-task>\nbody\n</task-board-task>')).toEqual({ title: '', description: 'body' })
  })
})
