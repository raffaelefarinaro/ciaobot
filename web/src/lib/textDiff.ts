/** Line changes between two versions of a small text body.
 *
 * Used by the proposal review card to show the exact replacement an accept
 * would make. Deliberately not a general diff: the bodies here are a bounded
 * memory region, a person-note stub or a Learnings file, and every change the
 * promotion path makes is a contiguous edit — an appended entry, one line
 * whose recurrence count went up, a replaced entry. Trimming the shared prefix
 * and suffix names exactly that edit, in a few lines, with no dependency.
 *
 * Context lines are not returned. What the card has to answer is *what
 * changes*; reprinting an unchanged region around it buries the one line that
 * does, which is the failure mode of showing the raw bodies side by side.
 */
export interface LineChange {
  op: 'added' | 'removed'
  text: string
}

/**
 * `separator` is what joins the destination's units, which the server names on
 * the preview. A bounded region's entries are joined by `\n§\n`, not by
 * newlines: diffed as lines, appending one entry showed a second added row
 * reading "§" and a multi-line entry was torn into unrelated rows.
 */
export function lineChanges(before: string, after: string, separator = '\n'): LineChange[] {
  if (before === after) return []
  // One trailing newline is a file convention, not a change. Without this the
  // serialized region's terminating newline split into a final empty line and
  // the card showed a blank "+" row under every real addition.
  const oldLines = splitBody(before, separator)
  const newLines = splitBody(after, separator)

  let start = 0
  while (start < oldLines.length && start < newLines.length && oldLines[start] === newLines[start]) {
    start += 1
  }
  let endOld = oldLines.length
  let endNew = newLines.length
  while (endOld > start && endNew > start && oldLines[endOld - 1] === newLines[endNew - 1]) {
    endOld -= 1
    endNew -= 1
  }

  const changes: LineChange[] = []
  for (const text of oldLines.slice(start, endOld)) changes.push({ op: 'removed', text })
  for (const text of newLines.slice(start, endNew)) changes.push({ op: 'added', text })
  return changes
}

function splitBody(text: string, separator: string): string[] {
  if (!text) return []
  return text.replace(/\n$/, '').split(separator)
}

/** One line of a compact diff: a change, or unchanged context around it. */
export interface DiffLine {
  op: 'added' | 'removed' | 'context'
  text: string
  /** 1-based position: in the new body for added and context lines, in the
   * old body for removed ones. Counts units, so for a bounded region it is an
   * entry number rather than a file line. */
  line: number
}

/**
 * The same contiguous edit `lineChanges` finds, with up to `context` unchanged
 * units on either side of it and a position on every line.
 *
 * The review row shows this without being opened, so it needs the one thing
 * `lineChanges` deliberately leaves out: enough of the surrounding file to say
 * where the change lands ("after line 41, under ## Decisions") without
 * reprinting the file.
 */
export function diffWithContext(before: string, after: string, separator = '\n', context = 1): DiffLine[] {
  if (before === after) return []
  const oldLines = splitBody(before, separator)
  const newLines = splitBody(after, separator)

  let start = 0
  while (start < oldLines.length && start < newLines.length && oldLines[start] === newLines[start]) {
    start += 1
  }
  let endOld = oldLines.length
  let endNew = newLines.length
  while (endOld > start && endNew > start && oldLines[endOld - 1] === newLines[endNew - 1]) {
    endOld -= 1
    endNew -= 1
  }

  const out: DiffLine[] = []
  for (let i = Math.max(0, start - context); i < start; i += 1) {
    out.push({ op: 'context', text: newLines[i], line: i + 1 })
  }
  for (let i = start; i < endOld; i += 1) out.push({ op: 'removed', text: oldLines[i], line: i + 1 })
  for (let i = start; i < endNew; i += 1) out.push({ op: 'added', text: newLines[i], line: i + 1 })
  for (let i = endNew; i < Math.min(newLines.length, endNew + context); i += 1) {
    out.push({ op: 'context', text: newLines[i], line: i + 1 })
  }
  return out
}

/** The nearest Markdown heading at or above a 1-based line of `body`, or ''.
 *
 * Scanned top-down so fenced code is skipped: a `# comment` inside a shell
 * block is not a heading, and naming it as the section would misplace the
 * change. */
export function headingAbove(body: string, line: number): string {
  const lines = body.split('\n')
  let heading = ''
  let fence = ''
  for (let i = 0; i < Math.min(line, lines.length); i += 1) {
    const text = lines[i]
    const opener = /^\s{0,3}(`{3,}|~{3,})/.exec(text)
    if (opener) {
      if (!fence) fence = opener[1][0]
      else if (opener[1][0] === fence) fence = ''
      continue
    }
    if (fence) continue
    const match = /^(#{1,6})\s+(.+?)\s*#*\s*$/.exec(text)
    if (match) heading = `${match[1]} ${match[2]}`
  }
  return heading
}
